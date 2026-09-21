"""Tests for summary.py output formatting and writing."""

import json
from datetime import datetime, timedelta

import pytest

from kosaccounts.models import RunSummary
from kosaccounts.summary import format_summary, write_summary


def test_format_summary_nothing_to_do():
    """Test format for a summary with no new files."""
    started = datetime(2026, 9, 18, 12, 0, 0)
    summary = RunSummary(started=started)

    text = format_summary(summary)
    assert "nothing to do (0 new files)" in text
    assert text.startswith(started.isoformat())


def test_format_summary_populated():
    """Test format for a summary with various results."""
    started = datetime(2026, 9, 18, 12, 0, 0)
    finished = datetime(2026, 9, 18, 12, 5, 0)

    summary = RunSummary(
        started=started,
        finished=finished,
        dry_run=False,
        files_found={"expense": 5, "bank": 3},
        files_new={"expense": 2, "bank": 1},
        files_flagged={"expense": 1, "bank": 0},
        files_errored={"expense": 0, "bank": 0},
        rows_added={"expense": 4, "bank": 2},
        rows_review=3,
        new_suppliers=["Amazon", "Uber"],
        unmatched_bank_txns=1,
        unmatched_expenses=2,
        errors=["expenses/2026-09-01 - test.pdf: validation error"],
    )

    text = format_summary(summary)

    # Check header
    assert "=== run" in text
    assert started.isoformat() in text
    assert finished.isoformat() in text

    # Check key numbers are present
    assert "Files found:" in text
    assert "expense: 5" in text
    assert "bank: 3" in text

    assert "Files new:" in text
    assert "expense: 2" in text
    assert "bank: 1" in text

    assert "Files flagged:" in text
    assert "Files errored:" in text

    assert "Rows added:" in text
    assert "expense: 4" in text
    assert "bank: 2" in text

    assert "Review rows:" in text
    assert "3" in text

    assert "New suppliers:" in text
    assert "Amazon" in text
    assert "Uber" in text

    assert "Unmatched bank:" in text
    assert "1" in text

    assert "Unmatched expense:" in text
    assert "2" in text

    assert "Errors:" in text
    assert "validation error" in text


def test_format_summary_dry_run():
    """Test format indicates dry run mode."""
    started = datetime(2026, 9, 18, 12, 0, 0)
    finished = datetime(2026, 9, 18, 12, 1, 0)

    summary = RunSummary(
        started=started,
        finished=finished,
        dry_run=True,
        files_new={"expense": 1},
    )

    text = format_summary(summary)
    assert "(dry run)" in text


def test_format_summary_unfinished():
    """Test format shows 'unfinished' when no finished time."""
    started = datetime(2026, 9, 18, 12, 0, 0)

    summary = RunSummary(
        started=started,
        finished=None,
        files_new={"expense": 1},
    )

    text = format_summary(summary)
    assert "unfinished" in text


def test_write_summary_single_run(tmp_path):
    """Test write_summary creates log and JSON files."""
    started = datetime(2026, 9, 18, 12, 0, 0)
    finished = datetime(2026, 9, 18, 12, 5, 0)

    summary = RunSummary(
        started=started,
        finished=finished,
        files_new={"expense": 2},
        rows_added={"expense": 4},
    )

    log_path = write_summary(summary, tmp_path)

    # Check log file
    assert log_path == tmp_path / "run-2026-09-18.log"
    assert log_path.exists()

    log_text = log_path.read_text()
    assert "=== run" in log_text
    assert started.isoformat() in log_text

    # Check JSON file
    json_path = tmp_path / "last_run.json"
    assert json_path.exists()

    data = json.loads(json_path.read_text())
    assert data["started"] == started.isoformat()
    assert data["finished"] == finished.isoformat()
    assert data["files_new"]["expense"] == 2
    assert data["rows_added"]["expense"] == 4


def test_write_summary_append_twice(tmp_path):
    """Test write_summary appends to the same file twice."""
    started1 = datetime(2026, 9, 18, 12, 0, 0)
    started2 = datetime(2026, 9, 18, 12, 15, 0)

    summary1 = RunSummary(started=started1, files_new={"expense": 1})
    summary2 = RunSummary(started=started2, files_new={"expense": 2})

    path1 = write_summary(summary1, tmp_path)
    path2 = write_summary(summary2, tmp_path)

    # Both should write to the same file
    assert path1 == path2

    # Both blocks should be in the file
    log_text = path1.read_text()
    assert started1.isoformat() in log_text
    assert started2.isoformat() in log_text

    # Check JSON is updated to the second run
    json_path = tmp_path / "last_run.json"
    data = json.loads(json_path.read_text())
    assert data["started"] == started2.isoformat()


def test_write_summary_creates_logs_dir(tmp_path):
    """Test write_summary creates the logs directory if missing."""
    logs_dir = tmp_path / "logs" / "nested"
    assert not logs_dir.exists()

    summary = RunSummary(started=datetime(2026, 9, 18, 12, 0, 0))
    write_summary(summary, logs_dir)

    assert logs_dir.exists()
    assert (logs_dir / "run-2026-09-18.log").exists()
