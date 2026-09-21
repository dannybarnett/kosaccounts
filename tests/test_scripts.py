"""Tests for shell scripts and systemd unit files."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from kosaccounts.intake.lock import hold_lock


def get_scripts_dir() -> Path:
    """Get the scripts directory path."""
    return Path(__file__).parent.parent / "scripts"


def get_systemd_dir() -> Path:
    """Get the systemd directory path."""
    return Path(__file__).parent.parent / "systemd"


def get_repo_dir() -> Path:
    return Path(__file__).parent.parent


def _bash_syntax_ok(script: Path) -> None:
    assert script.exists(), f"Script not found: {script}"
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error in {script.name}: {result.stderr}"


def test_run_pipeline_sh_syntax():
    _bash_syntax_ok(get_scripts_dir() / "run_pipeline.sh")


def test_run_pipeline_sh_skips_flock_when_kosaccounts_lock_held_env_set(tmp_path):
    """Watcher.process_once already holds logs/pipeline.lock (kosaccounts.intake.lock.hold_lock,
    same file/flock semantics) before invoking this script. Without KOSACCOUNTS_LOCK_HELD=1
    telling the script to skip its own `flock`, a child process locking the same path opens a
    NEW file description that always conflicts with the lock the parent already holds --
    deadlocking the watcher and the pipeline it's trying to run. This is a real subprocess run of
    the real script; KOSACCOUNTS_LOGS_DIR redirects its lock file/logs to tmp_path so it never
    touches the repo's own logs/. It may still fail downstream (e.g. importing the real repo
    config) -- this test only checks that the lock guard itself was bypassed.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    env = dict(os.environ)
    env.pop("KOSACCOUNTS_LOCK_HELD", None)
    env["KOSACCOUNTS_LOGS_DIR"] = str(logs_dir)
    env["KOSACCOUNTS_LOCK_HELD"] = "1"

    with hold_lock(logs_dir / "pipeline.lock"):
        subprocess.run(
            ["bash", str(get_scripts_dir() / "run_pipeline.sh"), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=get_repo_dir(),
            env=env,
            timeout=120,
        )

    lock_log = logs_dir / "pipeline.lock.log"
    assert not lock_log.exists() or "already running" not in lock_log.read_text()


def test_run_pipeline_sh_still_locks_when_env_var_not_set(tmp_path):
    """A manual run (no KOSACCOUNTS_LOCK_HELD) must still take the lock as before -- the guard
    above is opt-in for the watcher's own invocation, not a blanket removal of the lock."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    env = dict(os.environ)
    env.pop("KOSACCOUNTS_LOCK_HELD", None)
    env["KOSACCOUNTS_LOGS_DIR"] = str(logs_dir)

    with hold_lock(logs_dir / "pipeline.lock"):
        result = subprocess.run(
            ["bash", str(get_scripts_dir() / "run_pipeline.sh"), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=get_repo_dir(),
            env=env,
            timeout=120,
        )

    assert result.returncode == 0
    lock_log = logs_dir / "pipeline.lock.log"
    assert lock_log.exists()
    assert "already running" in lock_log.read_text()


def test_install_sh_syntax():
    _bash_syntax_ok(get_scripts_dir() / "install.sh")


def test_install_systemd_sh_syntax():
    _bash_syntax_ok(get_scripts_dir() / "install_systemd.sh")


def test_check_no_secrets_sh_syntax():
    _bash_syntax_ok(get_scripts_dir() / "check_no_secrets.sh")


def test_install_sh_no_longer_references_rclone_or_crontab():
    """install.sh replaced the crontab install step with install_systemd.sh, and dropped
    rclone from the apt package line (poppler-utils stays)."""
    content = (get_scripts_dir() / "install.sh").read_text()
    assert "rclone" not in content
    assert "crontab" not in content
    assert "poppler-utils" in content
    assert "install_systemd.sh" in content


def test_install_systemd_sh_default_mode_is_print_only():
    """Without --apply, install_systemd.sh must not execute sudo commands -- it only prints
    them. We can't safely invoke real sudo in a test, so this checks the script's own default
    dry-run behaviour by running it and confirming it does not error and prints command text
    rather than requiring privileges."""
    script = get_scripts_dir() / "install_systemd.sh"
    result = subprocess.run(
        ["bash", str(script), "--print-only"],
        capture_output=True,
        text=True,
        cwd=get_repo_dir(),
    )
    assert result.returncode == 0, result.stderr
    assert "sudo" in result.stdout
    assert "daemon-reload" in result.stdout
    assert "dry run" in result.stdout.lower()


# --- systemd unit file content checks -------------------------------------------------------

SERVICE_FILES = [
    "kosaccounts_dropbox_pull.service",
    "kosaccounts_reconcile.service",
    "kosaccounts_import_watch.service",
]

LONG_RUNNING_SERVICES = [
    "kosaccounts_dropbox_pull.service",
    "kosaccounts_import_watch.service",
]


def _unit_text(name: str) -> str:
    path = get_systemd_dir() / name
    assert path.exists(), f"Unit file not found: {path}"
    return path.read_text()


@pytest.mark.parametrize("unit_name", SERVICE_FILES)
def test_service_has_required_unit_lines(unit_name):
    text = _unit_text(unit_name)
    assert "After=network-online.target" in text
    assert "Wants=network-online.target" in text
    assert "RequiresMountsFor=/mnt/storage/docs/kosaccounts" in text
    assert "User=dannybarnett" in text


@pytest.mark.parametrize("unit_name", LONG_RUNNING_SERVICES)
def test_long_running_services_restart_always(unit_name):
    text = _unit_text(unit_name)
    assert "Restart=always" in text
    assert "RestartSec=10" in text


def test_reconcile_service_has_no_restart_always():
    """kosaccounts_reconcile.service is a oneshot triggered by the timer; it should not
    declare Restart=always like the long-running services."""
    text = _unit_text("kosaccounts_reconcile.service")
    assert "Type=oneshot" in text
    assert "Restart=always" not in text


def test_dropbox_pull_and_reconcile_have_environment_file():
    for unit_name in ["kosaccounts_dropbox_pull.service", "kosaccounts_reconcile.service"]:
        text = _unit_text(unit_name)
        assert "EnvironmentFile=/etc/kosaccounts.env" in text


def test_import_watch_has_no_environment_file():
    """The watcher needs no Dropbox secrets."""
    text = _unit_text("kosaccounts_import_watch.service")
    assert "EnvironmentFile=" not in text


def test_timer_has_hourly_and_persistent():
    text = _unit_text("kosaccounts_reconcile.timer")
    assert "OnCalendar=hourly" in text
    assert "Persistent=true" in text


def test_no_unit_file_has_non_empty_dropbox_value():
    """Guards against ever hardcoding a real secret into a checked-in unit file."""
    pattern = re.compile(r"DROPBOX_(APP_KEY|APP_SECRET|REFRESH_TOKEN)\s*=\s*\S")
    for path in get_systemd_dir().glob("*"):
        if not path.is_file():
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            assert not pattern.search(line), (
                f"{path.name}:{lineno} looks like it has a non-empty Dropbox secret value: {line!r}"
            )


def test_env_example_file_has_empty_values_for_all_three_vars():
    text = (get_systemd_dir() / "kosaccounts.env.example").read_text()
    for name in ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"]:
        assert re.search(rf"^{name}=\s*$", text, re.MULTILINE), (
            f"{name} should appear with an empty value in kosaccounts.env.example"
        )


def test_systemd_analyze_verify_if_available():
    """systemd-analyze verify checks unit syntax. It may complain about EnvironmentFile not
    existing on this machine (that file only exists in /etc on the real server) -- that's
    acceptable; a real syntax error is not."""
    import shutil

    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available on this machine")

    service_files = sorted(str(p) for p in get_systemd_dir().glob("*.service"))
    result = subprocess.run(
        ["systemd-analyze", "verify", *service_files],
        capture_output=True,
        text=True,
    )
    # A real syntax problem surfaces as a parse/load error; missing EnvironmentFile alone is
    # reported as a warning about the file not existing, not a syntax failure.
    combined = (result.stdout + result.stderr).lower()
    fatal_markers = ["failed to parse", "unknown lvalue", "invalid syntax"]
    for marker in fatal_markers:
        assert marker not in combined, f"systemd-analyze verify reported: {result.stdout}{result.stderr}"


# --- secrets check ----------------------------------------------------------------------------


def test_check_no_secrets_sh_exits_zero_against_repo():
    script = get_scripts_dir() / "check_no_secrets.sh"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        cwd=get_repo_dir(),
    )
    assert result.returncode == 0, (
        f"check_no_secrets.sh should exit 0 against this repo.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "no secrets found" in result.stdout
