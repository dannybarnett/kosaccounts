"""Stage 3: import watcher (specs/README_server_intake.md).

Watches `kos_root/imports/<folder>` for each configured folder (non-recursive, `IN_CLOSE_WRITE |
IN_MOVED_TO`). Waits until a folder has been quiet for `inotify_quiet_seconds`, then runs the
folder's configured processor command once, holding the same `logs/pipeline.lock` that
`scripts/run_pipeline.sh` flocks (via `intake.lock.hold_lock`), so a manual run and the watcher
never overlap. Events that ignore names starting with "." or ending ".partial" (unfinished
downloads / editor temp files) never mark a folder dirty.

`process_once` passes `KOSACCOUNTS_LOCK_HELD=1` in the child's environment so
`scripts/run_pipeline.sh` knows this process already holds `pipeline.lock` and skips its own
`flock` (flock is per open-file-description, so the child re-locking the same path would always
conflict with the lock we're already holding and report "already running" without ever
processing anything).

On startup every configured folder is marked dirty so the first pass sweeps whatever is already
present (events lost while the watcher was down are recovered this way).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

import inotify_simple

from kosaccounts.config import Config
from kosaccounts.intake.lock import LockHeld, hold_lock

logger = logging.getLogger("kosaccounts.intake.watcher")

_WATCH_MASK = inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO


def _ignored_name(name: str) -> bool:
    """True for unfinished downloads (".partial") and hidden/editor files (leading ".")."""
    return not name or name.startswith(".") or name.endswith(".partial")


class Watcher:
    def __init__(
        self,
        cfg: Config,
        run_command=subprocess.run,
        sleep=time.sleep,
        clock=time.monotonic,
        inotify_factory=inotify_simple.INotify,
        read_timeout_ms: int = 1000,
    ):
        self.cfg = cfg
        self.run_command = run_command
        self.sleep = sleep
        self.clock = clock
        self.inotify_factory = inotify_factory
        self.read_timeout_ms = read_timeout_ms

        self._quiet = cfg.intake.inotify_quiet_seconds
        self._root = cfg.paths.root
        self._pipeline_lock = cfg.paths.logs / "pipeline.lock"
        self._folders = list(cfg.intake.folders)

    def command_for(self, folder: str) -> list[str] | None:
        """argv for `folder`'s configured process command, with the import folder path appended
        as the last argument. None if no command is configured for this folder."""
        raw = self.cfg.intake.process_commands.get(folder)
        if not raw:
            return None
        argv = shlex.split(raw)
        if argv:
            candidate = argv[0]
            if "/" in candidate or (self._root / candidate).exists():
                argv[0] = str((self._root / candidate).resolve())
        argv.append(str(self.cfg.intake.import_folder(folder)))
        return argv

    def process_once(self, folder: str) -> None:
        """Run `folder`'s processor command once, holding the pipeline lock. Raises LockHeld if
        another run (manual `scripts/run_pipeline.sh` or another cycle) already holds it. Used by
        `run()`'s quiet-period trigger and by a CLI "sweep now" command."""
        argv = self.command_for(folder)
        if argv is None:
            logger.info("no process command configured for folder=%s; watching but skipping", folder)
            return
        with hold_lock(self._pipeline_lock):
            logger.info("processing folder=%s cmd=%s", folder, argv)
            # KOSACCOUNTS_LOCK_HELD=1 tells scripts/run_pipeline.sh that this Watcher already
            # holds logs/pipeline.lock (via hold_lock, above) and to skip taking it again: a
            # child process's `flock` on the same path opens a new file description, which
            # conflicts with the lock we're already holding and would otherwise always report
            # "already running" and exit without processing anything -- a deadlock.
            env = {**os.environ, "KOSACCOUNTS_LOCK_HELD": "1"}
            result = self.run_command(argv, cwd=str(self._root), check=False, env=env)
            logger.info(
                "finished folder=%s exit_code=%s", folder, getattr(result, "returncode", None)
            )

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        for folder in self._folders:
            self.cfg.intake.import_folder(folder).mkdir(parents=True, exist_ok=True)

        inotify = self.inotify_factory()
        wd_to_folder: dict[int, str] = {}
        try:
            for folder in self._folders:
                wd = inotify.add_watch(str(self.cfg.intake.import_folder(folder)), _WATCH_MASK)
                wd_to_folder[wd] = folder

            # Startup sweep: mark every folder dirty as if it had just gone quiet.
            start = self.clock()
            dirty = {folder: True for folder in self._folders}
            last_event = {folder: start - self._quiet for folder in self._folders}

            while not stop():
                for event in inotify.read(timeout=self.read_timeout_ms):
                    folder = wd_to_folder.get(event.wd)
                    if folder is None or _ignored_name(event.name):
                        continue
                    dirty[folder] = True
                    last_event[folder] = self.clock()
                    logger.debug(
                        "event folder=%s name=%s mask=%s", folder, event.name, event.mask
                    )

                now = self.clock()
                for folder in self._folders:
                    if not dirty[folder]:
                        continue
                    if now - last_event[folder] < self._quiet:
                        continue
                    # Clear dirty before running: events arriving during the run re-dirty it,
                    # which causes a rerun after another quiet period.
                    dirty[folder] = False
                    try:
                        self.process_once(folder)
                    except LockHeld:
                        logger.info("processor busy (manual run?), retrying")
                        dirty[folder] = True
                        last_event[folder] = self.clock() - self._quiet + (self._quiet / 4)
                    except Exception:
                        logger.exception("processing folder=%s failed", folder)
        finally:
            inotify.close()
