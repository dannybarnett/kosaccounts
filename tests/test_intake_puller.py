"""Tests for kosaccounts.intake.hashing, .settle, .puller and .lock.

FakeDropboxClient below implements the DropboxClient protocol over an in-memory
{folder: {name: bytes}} dict. `list_top_level_files` only ever iterates that flat dict, so
subfolders/nested files structurally cannot appear -- there is nothing to filter, which is the
same guarantee the real client provides by passing recursive=False.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from kosaccounts.config import IntakeConfig
from kosaccounts.intake.hashing import (
    BLOCK_SIZE,
    dropbox_content_hash,
    dropbox_content_hash_bytes,
)
from kosaccounts.intake.lock import LockHeld, hold_lock
from kosaccounts.intake.puller import (
    BatchResult,
    PlannedDownload,
    TransientError,
    local_name_for,
    listen,
    plan_batch,
    pull_folder,
    reconcile_all,
)
from kosaccounts.intake.settle import snapshot_of, wait_for_settle
from kosaccounts.intake.state import IntakeState
from kosaccounts.intake.types import ChangeSet, LongpollResult, RemoteFile


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Controls both `sleep` and `clock` so settle/backoff timing is instant in tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def monotonic(self) -> float:
        return self.t


def stop_after(n: int):
    """A `stop` callable for listen() that lets the loop body run exactly n times."""
    state = {"i": 0}

    def _stop() -> bool:
        if state["i"] >= n:
            return True
        state["i"] += 1
        return False

    return _stop


@dataclass
class FakeDropboxClient:
    files: dict[str, dict[str, bytes]] = field(default_factory=dict)
    download_log: list[tuple[str, str]] = field(default_factory=list)
    corrupt: set[tuple[str, str]] = field(default_factory=set)
    fail: set[tuple[str, str]] = field(default_factory=set)
    longpoll_results: deque = field(default_factory=deque)
    change_results: deque = field(default_factory=deque)
    list_calls: list[str] = field(default_factory=list)
    cursor_calls: int = 0

    _ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_file(self, folder: str, name: str, content: bytes) -> None:
        self.files.setdefault(folder, {})[name] = content
        self._ids.setdefault((folder, name), f"id:{len(self._ids) + 1}")

    def remove_file(self, folder: str, name: str) -> None:
        del self.files[folder][name]

    def _remote(self, folder: str, name: str) -> RemoteFile:
        content = self.files[folder][name]
        return RemoteFile(
            folder=folder,
            name=name,
            path_lower=f"/{folder}/{name.lower()}",
            path_display=f"/{folder}/{name}",
            dropbox_id=self._ids[(folder, name)],
            size=len(content),
            content_hash=dropbox_content_hash_bytes(content),
        )

    def list_top_level_files(self, folder: str) -> list[RemoteFile]:
        self.list_calls.append(folder)
        return [self._remote(folder, n) for n in sorted(self.files.get(folder, {}))]

    def latest_cursor(self) -> str:
        self.cursor_calls += 1
        return f"cursor:{self.cursor_calls}"

    def longpoll(self, cursor: str, timeout_seconds: int) -> LongpollResult:
        if self.longpoll_results:
            return self.longpoll_results.popleft()
        return LongpollResult(changes=False)

    def list_changes(self, cursor: str, watched_folders: list[str]) -> ChangeSet:
        if self.change_results:
            return self.change_results.popleft()
        return ChangeSet(folders_changed=set(), new_cursor=cursor, reset=False)

    def download(self, remote: RemoteFile, dest: Path) -> None:
        self.download_log.append((remote.folder, remote.name))
        key = (remote.folder, remote.name)
        if key in self.fail:
            self.fail.discard(key)
            raise TransientError("simulated transient failure")
        content = self.files[remote.folder][remote.name]
        if key in self.corrupt:
            self.corrupt.discard(key)
            content = content + b"CORRUPTED"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


def make_cfg(tmp_path: Path, **overrides) -> IntakeConfig:
    kwargs = dict(
        kos_root=tmp_path,
        folders=["expenses", "bank"],
        longpoll_timeout_seconds=480,
        settle_poll_seconds=1,
        settle_stable_polls=1,
        settle_max_wait_minutes=60,
        inotify_quiet_seconds=120,
        reconcile_interval_minutes=60,
    )
    kwargs.update(overrides)
    return IntakeConfig(**kwargs)


def make_remote(
    folder: str = "expenses",
    name: str = "invoice.pdf",
    content_hash: str = "a" * 64,
    dropbox_id: str = "id:1",
    size: int = 100,
) -> RemoteFile:
    return RemoteFile(
        folder=folder,
        name=name,
        path_lower=f"/{folder}/{name.lower()}",
        path_display=f"/{folder}/{name}",
        dropbox_id=dropbox_id,
        size=size,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_hash_bytes_empty_is_sha256_of_empty() -> None:
    assert dropbox_content_hash_bytes(b"") == hashlib.sha256(b"").hexdigest()


def test_hash_bytes_short_matches_double_sha256() -> None:
    content = b"hello world" * 100  # well under 4 MiB
    expected = hashlib.sha256(hashlib.sha256(content).digest()).hexdigest()
    assert dropbox_content_hash_bytes(content) == expected


def test_hash_file_short_matches_double_sha256(tmp_path: Path) -> None:
    content = b"x" * 12345
    p = tmp_path / "f.bin"
    p.write_bytes(content)
    expected = hashlib.sha256(hashlib.sha256(content).digest()).hexdigest()
    assert dropbox_content_hash(p) == expected


def test_hash_file_over_4mib_matches_hand_computation(tmp_path: Path) -> None:
    block1 = b"\x01" * BLOCK_SIZE
    block2 = b"\x02" * 10
    content = block1 + block2
    p = tmp_path / "big.bin"
    p.write_bytes(content)

    expected = hashlib.sha256(
        hashlib.sha256(block1).digest() + hashlib.sha256(block2).digest()
    ).hexdigest()
    assert dropbox_content_hash(p) == expected
    assert dropbox_content_hash_bytes(content) == expected


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------


def test_settle_stable_polls_one_returns_immediately() -> None:
    clock = FakeClock()
    files = [make_remote(name="a.pdf")]
    result = wait_for_settle(
        lambda: files, poll_seconds=1, stable_polls=1, max_wait_seconds=10,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    assert result == files
    assert clock.sleeps == []


def test_settle_after_n_identical_polls() -> None:
    clock = FakeClock()
    files = [make_remote(name="a.pdf")]
    calls = {"n": 0}

    def list_fn():
        calls["n"] += 1
        return files

    result = wait_for_settle(
        list_fn, poll_seconds=5, stable_polls=3, max_wait_seconds=1000,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    assert result == files
    assert calls["n"] == 3  # 1 initial + 2 confirming polls
    assert clock.sleeps == [5, 5]


def test_settle_resets_count_on_change() -> None:
    clock = FakeClock()
    # sequence: A, B, B  -> settles on the 3rd poll (2 consecutive B's) after resetting at poll 2
    sequence = [
        [make_remote(name="a.pdf", size=1)],
        [make_remote(name="a.pdf", size=2)],
        [make_remote(name="a.pdf", size=2)],
    ]
    calls = {"n": 0}

    def list_fn():
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[idx]

    result = wait_for_settle(
        list_fn, poll_seconds=1, stable_polls=2, max_wait_seconds=1000,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    assert result == sequence[-1]
    assert calls["n"] == 3


def test_settle_max_wait_returns_none() -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def list_fn():
        # never stable: size increments every call
        calls["n"] += 1
        return [make_remote(name="a.pdf", size=calls["n"])]

    result = wait_for_settle(
        list_fn, poll_seconds=1, stable_polls=2, max_wait_seconds=0,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    assert result is None


def test_snapshot_of_sorted_by_name() -> None:
    files = [make_remote(name="b.pdf", size=2, content_hash="b" * 64),
             make_remote(name="a.pdf", size=1, content_hash="a" * 64)]
    snap = snapshot_of(files)
    assert snap == (("a.pdf", 1, "a" * 64), ("b.pdf", 2, "b" * 64))


# ---------------------------------------------------------------------------
# local_name_for / plan_batch
# ---------------------------------------------------------------------------


def test_plan_batch_skips_already_imported(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with IntakeState(cfg.db_path) as state:
        remote = make_remote(content_hash="a" * 64)
        state.record(remote, "invoice.pdf")
        planned = plan_batch([remote], state, cfg)
        assert planned == []


def test_plan_batch_no_collision_new_file(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with IntakeState(cfg.db_path) as state:
        remote = make_remote(content_hash="a" * 64)
        planned = plan_batch([remote], state, cfg)
        assert len(planned) == 1
        item = planned[0]
        assert item.local_name == "invoice.pdf"
        assert item.staging_path == cfg.staging_folder("expenses") / "invoice.pdf.partial"
        assert item.final_path == cfg.import_folder("expenses") / "invoice.pdf"


def test_plan_batch_collision_when_state_has_different_hash(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with IntakeState(cfg.db_path) as state:
        old = make_remote(content_hash="a" * 64, dropbox_id="id:old")
        state.record(old, "invoice.pdf")

        new = make_remote(content_hash="b" * 64, dropbox_id="id:new")
        planned = plan_batch([new], state, cfg)
        assert len(planned) == 1
        assert planned[0].local_name == "invoice__" + ("b" * 8) + ".pdf"


def test_plan_batch_collision_when_local_file_differs(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    import_dir = cfg.import_folder("expenses")
    import_dir.mkdir(parents=True)
    local_content = b"some existing local content"
    (import_dir / "invoice.pdf").write_bytes(local_content)
    local_hash = dropbox_content_hash_bytes(local_content)

    remote_content = local_content + b"different"
    remote_hash = dropbox_content_hash_bytes(remote_content)
    assert remote_hash != local_hash

    with IntakeState(cfg.db_path) as state:
        remote = make_remote(content_hash=remote_hash)
        planned = plan_batch([remote], state, cfg)
        assert len(planned) == 1
        assert planned[0].local_name == f"invoice__{remote_hash[:8]}.pdf"


def test_local_name_for_no_suffix_collision(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with IntakeState(cfg.db_path) as state:
        old = make_remote(name="README", content_hash="a" * 64)
        state.record(old, "README")
        new = make_remote(name="README", content_hash="b" * 64)
        name = local_name_for(new, state, cfg.import_folder("expenses"))
        assert name == "README__" + ("b" * 8)


def test_local_name_for_pathological_collision_uses_full_hash(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    import_dir = cfg.import_folder("expenses")
    import_dir.mkdir(parents=True)

    remote_content = b"remote content"
    remote_hash = dropbox_content_hash_bytes(remote_content)

    # invoice.pdf exists locally with different content -> triggers collision naming
    (import_dir / "invoice.pdf").write_bytes(b"other content A")
    # the collision candidate name ALSO exists locally, with yet different content
    candidate_name = f"invoice__{remote_hash[:8]}.pdf"
    (import_dir / candidate_name).write_bytes(b"other content B")

    with IntakeState(cfg.db_path) as state:
        remote = make_remote(name="invoice.pdf", content_hash=remote_hash)
        name = local_name_for(remote, state, import_dir)
        assert name == f"invoice__{remote_hash}.pdf"


def test_plan_batch_same_name_same_hash_no_collision(tmp_path: Path) -> None:
    # a local file with the same content hash as the remote is not a collision at all;
    # but since it's already been imported once, state.is_imported would normally skip it.
    # Here it's on disk without a state record (e.g. copied manually) -- still not a collision
    # since the hash matches.
    cfg = make_cfg(tmp_path)
    import_dir = cfg.import_folder("expenses")
    import_dir.mkdir(parents=True)
    content = b"same content"
    content_hash = dropbox_content_hash_bytes(content)
    (import_dir / "invoice.pdf").write_bytes(content)

    with IntakeState(cfg.db_path) as state:
        remote = make_remote(content_hash=content_hash)
        name = local_name_for(remote, state, import_dir)
        assert name == "invoice.pdf"


# ---------------------------------------------------------------------------
# pull_folder
# ---------------------------------------------------------------------------


def test_pull_folder_downloads_verifies_and_records(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"content A")
    client.add_file("expenses", "b.pdf", b"content B" * 1000)

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg)

        assert result.failed == []
        assert {p.local_name for p in result.downloaded} == {"a.pdf", "b.pdf"}

        assert (cfg.import_folder("expenses") / "a.pdf").read_bytes() == b"content A"
        assert (cfg.import_folder("expenses") / "b.pdf").read_bytes() == b"content B" * 1000

        # staging is empty afterwards
        staging_files = list(cfg.staging_folder("expenses").iterdir())
        assert staging_files == []

        entries = state.entries("expenses")
        assert {e.local_name for e in entries} == {"a.pdf", "b.pdf"}


def test_pull_folder_empty_batch_when_nothing_new(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg)
        assert result == BatchResult(folder="expenses")


def test_pull_folder_settle_timeout_skips(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, settle_stable_polls=2, settle_max_wait_minutes=0, settle_poll_seconds=1)
    clock = FakeClock()

    calls = {"n": 0}

    def flaky_client_list(folder):
        calls["n"] += 1
        return [make_remote(name="a.pdf", size=calls["n"])]

    class FlakyClient:
        def list_top_level_files(self, folder):
            return flaky_client_list(folder)

    with IntakeState(cfg.db_path) as state:
        result = pull_folder(
            "expenses", FlakyClient(), state, cfg, sleep=clock.sleep, clock=clock.monotonic
        )
        assert result.skipped_settle is True
        assert result.downloaded == []
        assert result.failed == []


def test_pull_folder_corrupted_download_leaves_good_partial_nothing_recorded(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"good content")
    client.add_file("expenses", "b.pdf", b"bad content")
    client.corrupt.add(("expenses", "b.pdf"))

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg)

        assert len(result.failed) == 1
        assert result.failed[0][0].local_name == "b.pdf"
        assert result.downloaded == []

        # good file's verified partial stays in staging
        staging = cfg.staging_folder("expenses")
        assert (staging / "a.pdf.partial").exists()
        assert not (staging / "b.pdf.partial").exists()

        # nothing moved into imports
        import_dir = cfg.import_folder("expenses")
        assert not import_dir.exists() or list(import_dir.iterdir()) == []

        # nothing recorded
        assert state.entries("expenses") == []


def test_pull_folder_second_call_reuses_verified_partial(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"good content")
    client.add_file("expenses", "b.pdf", b"bad content")
    client.corrupt.add(("expenses", "b.pdf"))

    with IntakeState(cfg.db_path) as state:
        first = pull_folder("expenses", client, state, cfg)
        assert len(first.failed) == 1
        assert client.download_log.count(("expenses", "a.pdf")) == 1
        assert client.download_log.count(("expenses", "b.pdf")) == 1

        # retry: b.pdf no longer corrupts (consumed on first attempt)
        second = pull_folder("expenses", client, state, cfg)
        assert second.failed == []
        assert {p.local_name for p in second.downloaded} == {"a.pdf", "b.pdf"}

        # a.pdf was reused from the verified partial, not re-downloaded
        assert client.download_log.count(("expenses", "a.pdf")) == 1
        assert client.download_log.count(("expenses", "b.pdf")) == 2

        assert (cfg.import_folder("expenses") / "a.pdf").read_bytes() == b"good content"
        assert (cfg.import_folder("expenses") / "b.pdf").read_bytes() == b"bad content"
        assert list(cfg.staging_folder("expenses").iterdir()) == []
        assert {e.local_name for e in state.entries("expenses")} == {"a.pdf", "b.pdf"}


def test_pull_folder_moved_file_not_redownloaded(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"content A")

    with IntakeState(cfg.db_path) as state:
        first = pull_folder("expenses", client, state, cfg)
        assert len(first.downloaded) == 1

        # simulate the processor moving the finished file out of imports/
        (cfg.import_folder("expenses") / "a.pdf").unlink()

        second = pull_folder("expenses", client, state, cfg)
        assert second.downloaded == []
        assert second.failed == []
        assert client.download_log.count(("expenses", "a.pdf")) == 1  # only the first pull


def test_pull_folder_adopts_existing_identical_local_file_without_downloading(tmp_path: Path) -> None:
    """Files already on disk from the old rclone import predate the intake state store; a
    byte-identical file at the expected name/path is recorded as imported, not re-downloaded."""
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"content A")

    import_dir = cfg.import_folder("expenses")
    import_dir.mkdir(parents=True)
    (import_dir / "a.pdf").write_bytes(b"content A")
    before = (import_dir / "a.pdf").stat()

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg)

        assert result.failed == []
        assert result.downloaded == []
        assert {p.local_name for p in result.adopted} == {"a.pdf"}
        assert client.download_log == []  # never downloaded

        after = (import_dir / "a.pdf").stat()
        assert after.st_mtime_ns == before.st_mtime_ns  # file untouched
        assert (import_dir / "a.pdf").read_bytes() == b"content A"

        entries = state.entries("expenses")
        assert {e.local_name for e in entries} == {"a.pdf"}


def test_pull_folder_pre_existing_different_content_still_downloads_with_collision_name(
    tmp_path: Path,
) -> None:
    """A same-name local file with DIFFERENT content is not adopted: plan_batch already gives it
    a collision name, and that (non-existent) path is downloaded as before."""
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"new remote content")
    remote_hash = dropbox_content_hash_bytes(b"new remote content")

    import_dir = cfg.import_folder("expenses")
    import_dir.mkdir(parents=True)
    (import_dir / "a.pdf").write_bytes(b"different existing content")

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg)

        assert result.adopted == []
        assert result.failed == []
        expected_name = f"a__{remote_hash[:8]}.pdf"
        assert {p.local_name for p in result.downloaded} == {expected_name}
        assert client.download_log == [("expenses", "a.pdf")]
        assert (import_dir / expected_name).read_bytes() == b"new remote content"
        assert (import_dir / "a.pdf").read_bytes() == b"different existing content"  # untouched


def test_pull_folder_unchanged_folder_never_waits_for_settle(tmp_path: Path) -> None:
    """If the first listing shows nothing new, pull_folder returns at once: no settle polling,
    no sleeping, even though settle_stable_polls > 1 would otherwise force at least one sleep."""
    cfg = make_cfg(tmp_path, settle_stable_polls=2, settle_poll_seconds=5)
    client = FakeDropboxClient()
    clock = FakeClock()

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg, sleep=clock.sleep, clock=clock.monotonic)

    assert result == BatchResult(folder="expenses")
    assert clock.sleeps == []
    assert client.list_calls == ["expenses"]  # exactly one listing call, no settle polling


def test_pull_folder_new_file_still_settles_before_downloading(tmp_path: Path) -> None:
    """When the first listing shows something new, the settle wait still runs as before."""
    cfg = make_cfg(tmp_path, settle_stable_polls=2, settle_poll_seconds=5)
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"content A")
    clock = FakeClock()

    with IntakeState(cfg.db_path) as state:
        result = pull_folder("expenses", client, state, cfg, sleep=clock.sleep, clock=clock.monotonic)

    assert {p.local_name for p in result.downloaded} == {"a.pdf"}
    assert clock.sleeps == [5]  # one settle poll needed to reach settle_stable_polls=2


def test_list_top_level_files_only_returns_flat_dict_entries(tmp_path: Path) -> None:
    # documents FakeDropboxClient's contract: it has no concept of subfolders, so a real
    # non-recursive files_list_folder(recursive=False) call is modeled faithfully -- there is
    # simply nothing nested that could leak into the result.
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"x")
    files = client.list_top_level_files("expenses")
    assert [f.name for f in files] == ["a.pdf"]
    assert client.list_top_level_files("bank") == []


# ---------------------------------------------------------------------------
# reconcile_all / lock
# ---------------------------------------------------------------------------


def test_reconcile_all_pulls_every_configured_folder(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses", "bank"])
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"x")
    client.add_file("bank", "b.pdf", b"y")

    with IntakeState(cfg.db_path) as state:
        results = reconcile_all(client, state, cfg)
        assert {r.folder for r in results} == {"expenses", "bank"}
        assert {e.local_name for e in state.entries("expenses")} == {"a.pdf"}
        assert {e.local_name for e in state.entries("bank")} == {"b.pdf"}


def test_reconcile_all_skips_when_lock_held(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    client = FakeDropboxClient()
    with IntakeState(cfg.db_path) as state:
        with hold_lock(cfg.lock_path):
            results = reconcile_all(client, state, cfg)
        assert results == []


def test_hold_lock_raises_lockheld_when_already_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "state" / "intake.lock"
    with hold_lock(lock_path):
        with pytest.raises(LockHeld):
            with hold_lock(lock_path):
                pass  # pragma: no cover


# ---------------------------------------------------------------------------
# listen
# ---------------------------------------------------------------------------


def test_listen_startup_takes_cursor_before_reconciling(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses", "bank"])
    client = FakeDropboxClient()
    clock = FakeClock()

    with IntakeState(cfg.db_path) as state:
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=lambda: True)

    assert client.cursor_calls == 1
    # reconcile_all was called at startup: one list_top_level_files call per folder
    assert client.list_calls == ["expenses", "bank"]


def test_listen_change_triggers_pull_for_right_folder_only(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses", "bank"], reconcile_interval_minutes=1000)
    client = FakeDropboxClient()
    clock = FakeClock()
    client.longpoll_results.append(LongpollResult(changes=True))
    client.change_results.append(
        ChangeSet(folders_changed={"expenses"}, new_cursor="cursor:next", reset=False)
    )

    with IntakeState(cfg.db_path) as state:
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(1))

    # startup reconcile: one call per folder (2 calls)
    # change-triggered pull_folder: exactly one more call, for "expenses"
    assert client.list_calls == ["expenses", "bank", "expenses"]


def test_listen_reset_rebuilds_cursor_and_reconciles(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses", "bank"], reconcile_interval_minutes=1000)
    client = FakeDropboxClient()
    clock = FakeClock()
    client.longpoll_results.append(LongpollResult(changes=True))
    client.change_results.append(ChangeSet(reset=True))

    with IntakeState(cfg.db_path) as state:
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(1))

    assert client.cursor_calls == 2  # startup + after reset
    # 2 folders x 2 reconciles (startup, post-reset)
    assert client.list_calls == ["expenses", "bank", "expenses", "bank"]


def test_listen_backoff_seconds_are_slept(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses"], reconcile_interval_minutes=1000)
    client = FakeDropboxClient()
    clock = FakeClock()
    client.longpoll_results.append(LongpollResult(changes=False, backoff_seconds=42))

    with IntakeState(cfg.db_path) as state:
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(1))

    assert 42 in clock.sleeps


def test_listen_transient_error_does_not_kill_loop(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses"], reconcile_interval_minutes=1000)
    clock = FakeClock()

    calls = {"n": 0}

    class RaisingOnceClient(FakeDropboxClient):
        def longpoll(self, cursor, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientError("boom")
            return LongpollResult(changes=False)

    client = RaisingOnceClient()

    with IntakeState(cfg.db_path) as state:
        # should not raise
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(2))

    assert 10.0 in clock.sleeps  # initial backoff after the TransientError


def test_listen_change_waits_for_held_lock_and_retries_until_released(tmp_path: Path) -> None:
    """A change-triggered pull must not just give up when the hourly reconcile holds the lock:
    it retries every 30s (here, released after two retries) instead of waiting a full hour."""
    cfg = make_cfg(
        tmp_path, folders=["expenses"], reconcile_interval_minutes=1000, settle_max_wait_minutes=60
    )
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"data")
    clock = FakeClock()
    client.longpoll_results.append(LongpollResult(changes=True))
    client.change_results.append(
        ChangeSet(folders_changed={"expenses"}, new_cursor="cursor:next", reset=False)
    )

    lock_cm = hold_lock(cfg.lock_path)
    lock_cm.__enter__()
    released = {"sleeps": 0}

    def sleep_and_release(seconds: float) -> None:
        clock.sleep(seconds)
        released["sleeps"] += 1
        if released["sleeps"] == 2:
            lock_cm.__exit__(None, None, None)

    try:
        with IntakeState(cfg.db_path) as state:
            listen(
                client, state, cfg, sleep=sleep_and_release, clock=clock.monotonic,
                stop=stop_after(1),
            )
            # eventually pulled once the lock was released
            assert {e.local_name for e in state.entries("expenses")} == {"a.pdf"}
    finally:
        if released["sleeps"] < 2:
            lock_cm.__exit__(None, None, None)  # pragma: no cover - safety net

    assert clock.sleeps == [30, 30]


def test_listen_change_gives_up_waiting_for_lock_after_settle_max_wait(tmp_path: Path) -> None:
    """If the lock is never released, the retry loop gives up after settle_max_wait_minutes and
    lets the next hourly reconcile catch up, instead of blocking listen() forever."""
    cfg = make_cfg(
        tmp_path, folders=["expenses"], reconcile_interval_minutes=1000, settle_max_wait_minutes=1
    )
    client = FakeDropboxClient()
    client.add_file("expenses", "a.pdf", b"data")
    clock = FakeClock()
    client.longpoll_results.append(LongpollResult(changes=True))
    client.change_results.append(
        ChangeSet(folders_changed={"expenses"}, new_cursor="cursor:next", reset=False)
    )

    with hold_lock(cfg.lock_path):
        with IntakeState(cfg.db_path) as state:
            listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(1))
            assert state.entries("expenses") == []  # never pulled; lock held throughout

    assert clock.sleeps == [30, 30]  # two retries (60s) before the 1-minute deadline is hit


def test_listen_hourly_reconcile_fires_on_clock_advance(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, folders=["expenses", "bank"], reconcile_interval_minutes=60)
    client = FakeDropboxClient()
    clock = FakeClock()
    # backoff_seconds advances the fake clock past the hourly threshold (3600s)
    client.longpoll_results.append(LongpollResult(changes=False, backoff_seconds=3601))

    with IntakeState(cfg.db_path) as state:
        listen(client, state, cfg, sleep=clock.sleep, clock=clock.monotonic, stop=stop_after(1))

    # startup reconcile (2 calls) + hourly reconcile triggered after the backoff sleep (2 calls)
    assert client.list_calls == ["expenses", "bank", "expenses", "bank"]
