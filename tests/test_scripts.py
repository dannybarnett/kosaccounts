"""Tests for shell scripts and crontab configuration."""

import re
import subprocess
from pathlib import Path


def get_scripts_dir() -> Path:
    """Get the scripts directory path."""
    return Path(__file__).parent.parent / "scripts"


def test_rclone_import_sh_syntax():
    """Test rclone_import.sh for bash syntax errors."""
    script = get_scripts_dir() / "rclone_import.sh"
    assert script.exists(), f"Script not found: {script}"

    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error in rclone_import.sh: {result.stderr}"


def test_run_pipeline_sh_syntax():
    """Test run_pipeline.sh for bash syntax errors."""
    script = get_scripts_dir() / "run_pipeline.sh"
    assert script.exists(), f"Script not found: {script}"

    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error in run_pipeline.sh: {result.stderr}"


def test_install_sh_syntax():
    """Test install.sh for bash syntax errors."""
    script = get_scripts_dir() / "install.sh"
    assert script.exists(), f"Script not found: {script}"

    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Syntax error in install.sh: {result.stderr}"


def test_crontab_txt_format():
    """Test crontab.txt has correct cron syntax."""
    crontab_file = get_scripts_dir() / "crontab.txt"
    assert crontab_file.exists(), f"File not found: {crontab_file}"

    content = crontab_file.read_text()
    lines = content.strip().split("\n")

    # Count non-comment, non-blank lines
    cron_lines = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    assert len(cron_lines) == 2, f"Expected 2 cron lines, found {len(cron_lines)}"

    # Cron line pattern: minute hour dom month dow command
    cron_pattern = r"^(\d+|\*)\s+(\d+|\*)\s+(\d+|\*)\s+(\d+|\*)\s+(\d+|\*)\s+(.+)$"

    for i, line in enumerate(cron_lines):
        match = re.match(cron_pattern, line)
        assert match, f"Cron line {i+1} does not match expected format: {line}"

        # Extract the command (everything after the 5 time fields)
        time_fields = line.split()[:5]
        assert len(time_fields) == 5, f"Cron line {i+1} does not have 5 time fields"

        # Extract command path
        command_parts = line.split()[5:]
        script_path = command_parts[0]

        # Check that the script path is absolute
        assert script_path.startswith("/"), f"Script path must be absolute: {script_path}"

        # Check that the script exists
        assert Path(script_path).exists(), f"Script does not exist: {script_path}"


def test_crontab_txt_has_expected_scripts():
    """Test crontab.txt references the expected scripts."""
    crontab_file = get_scripts_dir() / "crontab.txt"
    content = crontab_file.read_text()

    # Should reference both scripts
    assert "rclone_import.sh" in content, "rclone_import.sh not referenced in crontab.txt"
    assert "run_pipeline.sh" in content, "run_pipeline.sh not referenced in crontab.txt"

    # rclone should run at :00 (minute 0) - only check non-comment lines
    lines = [
        line.strip()
        for line in content.split("\n")
        if "rclone_import" in line and not line.strip().startswith("#")
    ]
    assert len(lines) == 1, f"Expected 1 rclone_import.sh cron line, found {len(lines)}"
    assert lines[0].startswith("0 "), "rclone_import.sh should run at minute 0 (:00)"

    # pipeline should run at :15 (minute 15) - only check non-comment lines
    lines = [
        line.strip()
        for line in content.split("\n")
        if "run_pipeline" in line and not line.strip().startswith("#")
    ]
    assert len(lines) == 1, f"Expected 1 run_pipeline.sh cron line, found {len(lines)}"
    assert lines[0].startswith("15 "), "run_pipeline.sh should run at minute 15 (:15)"
