"""Tests for kosaccounts.intake.watcher.Watcher (Stage 3, specs/README_server_intake.md).

Builds its own minimal config.toml per test (rather than reusing tests/conftest.py's `tmp_repo`,
which another agent is editing concurrently) with just an [intake] section: `kos_root` under
tmp_path, arbitrary folder names, and process_commands pointing at placeholder tokens (the actual
subprocess call is always replaced by a fake `run_command`, so the command text itself is never
executed by a shell).

Uses real `inotify_simple.INotify` against tmp_path (Linux only) and a fake, manually-advanced
clock. Since `Watcher.run()`'s only hook back into the test while it is looping is the `stop()`
callable (checked once per iteration, before that iteration's `inotify.read()`), tests drive
timing by writing files / advancing the clock / releasing locks from inside `stop()` at a chosen
iteration number (see `_stop_after`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kosaccounts.config import Config, load_config
from kosaccounts.intake.lock import hold_lock
from kosaccounts.intake.watcher import Watcher

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify_simple requires Linux")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, folders: list[str], commands: dict[str, str], quiet: float = 2) -> Path:
    folders_toml = "[" + ", ".join(f'"{f}"' for f in folders) + "]"
    commands_toml = "{ " + ", ".join(f'{k} = "{v}"' for k, v in commands.items()) + " }"
    content = (
        "[intake]\n"
        'kos_root = "kos"\n'
        f"folders = {folders_toml}\n"
        f"inotify_quiet_seconds = {quiet}\n"
        f"process_commands = {commands_toml}\n"
    )
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def _config(tmp_path: Path, folders: list[str], commands: dict[str, str], quiet: float = 2) -> Config:
    return load_config(_write_config(tmp_path, folders, commands, quiet))


class FakeClock:
    """A manually-advanced stand-in for time.monotonic."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _stop_after(n: int, on_iteration=None):
    """A Watcher.run(stop=...) callable: False for the first n calls, True after.

    `on_iteration(k)` (1-based) runs before each stop check, so a test can write files, advance a
    FakeClock, or release a lock at an exact point in the loop.
    """
    count = {"n": 0}

    def stop() -> bool:
        count["n"] += 1
        if on_iteration is not None:
            on_iteration(count["n"])
        return count["n"] > n

    return stop


def _recorder():
    calls: list = []

    def fake_run(argv, cwd, check, env=None):
        calls.append(argv[-1])
        return SimpleNamespace(returncode=0)

    return calls, fake_run


# ---------------------------------------------------------------------------
# command_for
# ---------------------------------------------------------------------------


def test_command_for_resolves_relative_script_against_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "stub.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    cfg = _config(tmp_path, ["alpha"], {"alpha": "scripts/stub.sh --flag"})
    watcher = Watcher(cfg)

    argv = watcher.command_for("alpha")

    assert argv == [str(script.resolve()), "--flag", str(cfg.intake.import_folder("alpha"))]


def test_command_for_returns_none_when_no_command_configured(tmp_path):
    cfg = _config(tmp_path, ["alpha", "gamma"], {"alpha": "true"})
    watcher = Watcher(cfg)

    assert watcher.command_for("gamma") is None


def test_process_once_skips_folder_without_command(tmp_path):
    cfg = _config(tmp_path, ["alpha", "gamma"], {"alpha": "true"})
    calls, fake_run = _recorder()
    watcher = Watcher(cfg, run_command=fake_run)

    watcher.process_once("gamma")  # must not raise, must not call run_command

    assert calls == []


def test_process_once_passes_kosaccounts_lock_held_env_to_run_command(tmp_path):
    """The watcher already holds logs/pipeline.lock (via hold_lock) before invoking the
    processor command; it must tell scripts/run_pipeline.sh not to flock the same path again
    (a child-process flock on the same file is a NEW open file description and would always
    conflict with the lock the watcher already holds -- see scripts/run_pipeline.sh)."""
    cfg = _config(tmp_path, ["alpha"], {"alpha": "cmd-a"})
    captured: dict = {}

    def fake_run(argv, cwd, check, env=None):
        captured["env"] = env
        return SimpleNamespace(returncode=0)

    watcher = Watcher(cfg, run_command=fake_run)
    watcher.process_once("alpha")

    assert captured["env"] is not None
    assert captured["env"].get("KOSACCOUNTS_LOCK_HELD") == "1"
    # the rest of the process environment (e.g. PATH) is preserved, not replaced
    assert captured["env"].get("PATH") == os.environ.get("PATH")


# ---------------------------------------------------------------------------
# run(): startup sweep, dirty-tracking, in-progress re-dirty, ignored names, lock contention
# ---------------------------------------------------------------------------


def test_startup_sweep_runs_each_folder_once(tmp_path):
    cfg = _config(tmp_path, ["alpha", "beta"], {"alpha": "cmd-a", "beta": "cmd-b"})
    calls, fake_run = _recorder()
    watcher = Watcher(cfg, run_command=fake_run, clock=FakeClock(0.0), read_timeout_ms=50)

    watcher.run(stop=_stop_after(1))

    assert sorted(calls) == sorted(
        [str(cfg.intake.import_folder("alpha")), str(cfg.intake.import_folder("beta"))]
    )


def test_dirty_tracking_runs_once_after_quiet_for_changed_folder_only(tmp_path):
    cfg = _config(tmp_path, ["alpha", "beta"], {"alpha": "cmd-a", "beta": "cmd-b"}, quiet=2)
    calls, fake_run = _recorder()
    clock = FakeClock(0.0)
    watcher = Watcher(cfg, run_command=fake_run, clock=clock, read_timeout_ms=100)
    alpha_dir = cfg.intake.import_folder("alpha")

    def on_iteration(n):
        if n == 2:
            calls.clear()  # drop the startup-sweep runs; this test is about the post-startup event
            for i in range(3):
                (alpha_dir / f"file{i}.pdf").write_bytes(b"data")
        elif n == 3:
            clock.advance(5)

    watcher.run(stop=_stop_after(3, on_iteration=on_iteration))

    assert calls == [str(alpha_dir)]


def test_event_during_run_causes_second_run_after_quiet(tmp_path):
    cfg = _config(tmp_path, ["alpha"], {"alpha": "cmd-a"}, quiet=2)
    calls: list = []
    clock = FakeClock(0.0)
    alpha_dir = cfg.intake.import_folder("alpha")

    def fake_run(argv, cwd, check, env=None):
        calls.append(argv[-1])
        (alpha_dir / "during-run.pdf").write_bytes(b"data")  # simulates a file landing mid-run
        return SimpleNamespace(returncode=0)

    watcher = Watcher(cfg, run_command=fake_run, clock=clock, read_timeout_ms=100)

    def on_iteration(n):
        if n == 3:
            clock.advance(5)

    watcher.run(stop=_stop_after(3, on_iteration=on_iteration))

    assert calls == [str(alpha_dir), str(alpha_dir)]  # startup run, then the re-dirtied rerun


def test_partial_and_dotfiles_are_ignored(tmp_path):
    cfg = _config(tmp_path, ["alpha"], {"alpha": "cmd-a"}, quiet=2)
    calls, fake_run = _recorder()
    clock = FakeClock(0.0)
    watcher = Watcher(cfg, run_command=fake_run, clock=clock, read_timeout_ms=100)
    alpha_dir = cfg.intake.import_folder("alpha")

    def on_iteration(n):
        if n == 2:
            calls.clear()
            (alpha_dir / ".hidden.pdf").write_bytes(b"data")
            (alpha_dir / "upload.pdf.partial").write_bytes(b"data")
        elif n == 3:
            clock.advance(10)

    watcher.run(stop=_stop_after(3, on_iteration=on_iteration))

    assert calls == []


def test_lock_held_leaves_dirty_and_retries_after_release(tmp_path):
    cfg = _config(tmp_path, ["alpha"], {"alpha": "cmd-a"}, quiet=2)
    calls, fake_run = _recorder()
    clock = FakeClock(0.0)
    watcher = Watcher(cfg, run_command=fake_run, clock=clock, read_timeout_ms=50)

    lock_path = cfg.paths.logs / "pipeline.lock"
    lock_cm = hold_lock(lock_path)
    lock_cm.__enter__()
    released = {"done": False}

    def on_iteration(n):
        if n == 2:
            assert calls == []  # iteration 1 attempted the startup run but the lock was held
            lock_cm.__exit__(None, None, None)
            released["done"] = True
            clock.advance(0.5)  # quiet/4 retry delay

    try:
        watcher.run(stop=_stop_after(2, on_iteration=on_iteration))
    finally:
        if not released["done"]:
            lock_cm.__exit__(None, None, None)

    assert calls == [str(cfg.intake.import_folder("alpha"))]


def test_folder_without_command_never_runs(tmp_path):
    cfg = _config(tmp_path, ["alpha", "gamma"], {"alpha": "cmd-a"}, quiet=2)
    calls, fake_run = _recorder()
    watcher = Watcher(cfg, run_command=fake_run, clock=FakeClock(0.0), read_timeout_ms=50)

    watcher.run(stop=_stop_after(1))

    assert calls == [str(cfg.intake.import_folder("alpha"))]
    # gamma is still watched (its import folder is created) even though nothing ever runs for it
    assert cfg.intake.import_folder("gamma").is_dir()
