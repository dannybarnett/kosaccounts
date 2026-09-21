"""Stage 2 core (specs/README_server_intake.md): settle a Dropbox folder, download a complete
batch through staging, verify, then atomically rename into imports/<folder>/ and record state.

`listen()` is the long-running loop: take a cursor, reconcile everything, then long-poll for
changes, pulling only the folders that changed, with a periodic reconcile as a backstop and a
cursor rebuild on reset. `reconcile_all()` / `pull_folder()` are also used standalone by
`intake reconcile` and by tests.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import IntakeConfig
from .hashing import dropbox_content_hash
from .lock import LockHeld, hold_lock
from .settle import wait_for_settle
from .state import IntakeState
from .types import DropboxClient, RemoteFile

logger = logging.getLogger("kosaccounts.intake.puller")


class TransientError(Exception):
    """Network/API error the caller should retry after a backoff. The real dropbox_client.py
    raises this (or a subclass of it) for connection errors and retryable Dropbox API errors."""


def local_name_for(
    remote: RemoteFile,
    state: IntakeState,
    import_dir: Path,
    sha_of_local: Callable[[Path], str] = dropbox_content_hash,
) -> str:
    """The local file name to use for `remote`. Usually just remote.name; if a different-hash
    file with the same name is already known (recorded, or sitting in the imports folder), use
    `<stem>__<hash8><suffix>` instead so nothing is ever overwritten."""
    name = remote.name
    known = state.known_hashes_for_name(remote.folder, name)
    collides = any(h != remote.content_hash for h in known)

    if not collides:
        local_path = import_dir / name
        if local_path.exists():
            try:
                existing_hash = sha_of_local(local_path)
            except OSError:
                existing_hash = None
            if existing_hash is not None and existing_hash != remote.content_hash:
                collides = True

    if not collides:
        return name

    stem = Path(name).stem
    suffix = Path(name).suffix
    short_hash = remote.content_hash[:8]
    candidate = f"{stem}__{short_hash}{suffix}"
    logger.warning(
        "name collision for %s/%s: using %s instead", remote.folder, name, candidate
    )

    candidate_path = import_dir / candidate
    if candidate_path.exists():
        try:
            candidate_hash = sha_of_local(candidate_path)
        except OSError:
            candidate_hash = None
        if candidate_hash is not None and candidate_hash != remote.content_hash:
            candidate = f"{stem}__{remote.content_hash}{suffix}"
            logger.warning(
                "collision name also taken for %s/%s: using %s", remote.folder, name, candidate
            )

    return candidate


@dataclass
class PlannedDownload:
    remote: RemoteFile
    local_name: str
    staging_path: Path
    final_path: Path


@dataclass
class BatchResult:
    folder: str
    downloaded: list[PlannedDownload] = field(default_factory=list)
    failed: list[tuple[PlannedDownload, str]] = field(default_factory=list)
    skipped_settle: bool = False
    adopted: list[PlannedDownload] = field(default_factory=list)


def plan_batch(
    files: list[RemoteFile], state: IntakeState, cfg: IntakeConfig
) -> list[PlannedDownload]:
    planned: list[PlannedDownload] = []
    for remote in files:
        if state.is_imported(remote.folder, remote.name, remote.content_hash):
            continue
        import_dir = cfg.import_folder(remote.folder)
        local_name = local_name_for(remote, state, import_dir)
        staging_path = cfg.staging_folder(remote.folder) / (local_name + ".partial")
        final_path = import_dir / local_name
        planned.append(
            PlannedDownload(
                remote=remote,
                local_name=local_name,
                staging_path=staging_path,
                final_path=final_path,
            )
        )
    return planned


def pull_folder(
    folder: str,
    client: DropboxClient,
    state: IntakeState,
    cfg: IntakeConfig,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BatchResult:
    # Cheap first look, no settle wait: if nothing is new we can return at once instead of
    # paying the settle_poll_seconds x settle_stable_polls delay for an unchanged folder (the
    # common case for both the hourly reconcile and most long-poll wakeups).
    initial_listing = client.list_top_level_files(folder)
    if not plan_batch(initial_listing, state, cfg):
        return BatchResult(folder=folder)

    settled = wait_for_settle(
        lambda: client.list_top_level_files(folder),
        cfg.settle_poll_seconds,
        cfg.settle_stable_polls,
        cfg.settle_max_wait_minutes * 60,
        sleep=sleep,
        clock=clock,
    )
    if settled is None:
        logger.warning("folder %s did not settle within %s minutes", folder, cfg.settle_max_wait_minutes)
        return BatchResult(folder=folder, skipped_settle=True)

    planned = plan_batch(settled, state, cfg)
    if not planned:
        return BatchResult(folder=folder)

    staging_folder = cfg.staging_folder(folder)
    import_folder = cfg.import_folder(folder)
    staging_folder.mkdir(parents=True, exist_ok=True)
    import_folder.mkdir(parents=True, exist_ok=True)

    result = BatchResult(folder=folder)

    # Adopt planned items that are already sitting on disk, byte-for-byte identical to the
    # remote copy (e.g. the files already imported by the old rclone-based pipeline, which
    # predate this state store): record them as imported without downloading or touching the
    # file. plan_batch/local_name_for already guarantee local_name == remote.name (no collision
    # renaming) whenever an on-disk file at that name has the same content hash, so this check
    # only ever fires in that exact case.
    to_download: list[PlannedDownload] = []
    for item in planned:
        remote = item.remote
        if item.final_path.exists():
            try:
                same_size = item.final_path.stat().st_size == remote.size
            except OSError:
                same_size = False
            if same_size and dropbox_content_hash(item.final_path) == remote.content_hash:
                logger.info(
                    "adopted existing local file for %s/%s: %s already on disk with matching "
                    "content, not re-downloaded",
                    folder,
                    item.local_name,
                    item.final_path,
                )
                state.record(item.remote, item.local_name)
                result.adopted.append(item)
                continue
        to_download.append(item)

    verified: list[PlannedDownload] = []

    for item in to_download:
        remote = item.remote
        try:
            reused = False
            if item.staging_path.exists():
                try:
                    size = item.staging_path.stat().st_size
                except OSError:
                    size = -1
                if size == remote.size and dropbox_content_hash(item.staging_path) == remote.content_hash:
                    logger.info("reusing verified partial for %s/%s", folder, item.local_name)
                    reused = True

            if not reused:
                client.download(remote, item.staging_path)
                actual_size = item.staging_path.stat().st_size
                if actual_size != remote.size:
                    raise ValueError(
                        f"size mismatch: expected {remote.size}, got {actual_size}"
                    )
                actual_hash = dropbox_content_hash(item.staging_path)
                if actual_hash != remote.content_hash:
                    raise ValueError(
                        f"content hash mismatch: expected {remote.content_hash}, got {actual_hash}"
                    )
        except Exception as exc:  # noqa: BLE001 - any download/verify failure fails just this item
            if item.staging_path.exists():
                try:
                    item.staging_path.unlink()
                except OSError:
                    pass
            logger.error("download/verify failed for %s/%s: %s", folder, item.local_name, exc)
            result.failed.append((item, str(exc)))
            continue

        verified.append(item)

    if result.failed:
        logger.error(
            "batch for %s had %d failure(s); %d verified partial(s) left in staging, nothing moved",
            folder,
            len(result.failed),
            len(verified),
        )
        return result

    total_bytes = 0
    names = []
    for item in verified:
        os.replace(item.staging_path, item.final_path)
        state.record(item.remote, item.local_name)
        result.downloaded.append(item)
        total_bytes += item.remote.size
        names.append(item.local_name)

    logger.info(
        "folder %s: downloaded %d file(s), %d bytes: %s",
        folder,
        len(result.downloaded),
        total_bytes,
        ", ".join(names),
    )
    return result


def reconcile_all(
    client: DropboxClient,
    state: IntakeState,
    cfg: IntakeConfig,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[BatchResult]:
    try:
        with hold_lock(cfg.lock_path):
            return [
                pull_folder(folder, client, state, cfg, sleep=sleep, clock=clock)
                for folder in cfg.folders
            ]
    except LockHeld:
        logger.info("reconcile skipped: lock held")
        return []


def _pull_folder_waiting_for_lock(
    folder: str,
    client: DropboxClient,
    state: IntakeState,
    cfg: IntakeConfig,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Pull `folder`, retrying every 30s if `cfg.lock_path` is held (typically by the hourly
    reconcile) instead of skipping the pull outright -- a change-triggered pull is the fast
    path and should not silently wait a full hour for the next reconcile. Gives up and logs a
    warning after `cfg.settle_max_wait_minutes`; the next hourly reconcile will catch up."""
    deadline = clock() + cfg.settle_max_wait_minutes * 60
    while True:
        try:
            with hold_lock(cfg.lock_path):
                pull_folder(folder, client, state, cfg, sleep=sleep, clock=clock)
            return
        except LockHeld:
            if clock() >= deadline:
                logger.warning(
                    "pull for %s gave up waiting for the lock after %s minutes; hourly "
                    "reconcile will catch up",
                    folder,
                    cfg.settle_max_wait_minutes,
                )
                return
            logger.info(
                "pull for %s waiting for lock (reconcile in progress?); retrying in 30s", folder
            )
            sleep(30)


def listen(
    client: DropboxClient,
    state: IntakeState,
    cfg: IntakeConfig,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    cursor = client.latest_cursor()
    reconcile_all(client, state, cfg, sleep=sleep, clock=clock)
    last_reconcile = clock()

    backoff = 10.0
    max_backoff = 600.0

    while not stop():
        try:
            result = client.longpoll(cursor, cfg.longpoll_timeout_seconds)
            if result.backoff_seconds:
                sleep(result.backoff_seconds)
            if result.changes:
                cs = client.list_changes(cursor, cfg.folders)
                if cs.reset:
                    logger.warning("cursor reset; rebuilding and reconciling")
                    cursor = client.latest_cursor()
                    reconcile_all(client, state, cfg, sleep=sleep, clock=clock)
                    last_reconcile = clock()
                else:
                    cursor = cs.new_cursor
                    for folder in sorted(cs.folders_changed):
                        _pull_folder_waiting_for_lock(folder, client, state, cfg, sleep, clock)

            if clock() - last_reconcile >= cfg.reconcile_interval_minutes * 60:
                reconcile_all(client, state, cfg, sleep=sleep, clock=clock)
                last_reconcile = clock()

            backoff = 10.0
        except Exception as exc:  # noqa: BLE001 - covers TransientError/OSError; loop must never die
            logger.error("intake listen error, retrying in %.0fs: %s", backoff, exc, exc_info=True)
            sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
