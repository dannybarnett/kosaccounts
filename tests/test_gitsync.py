"""Tests for kosaccounts.gitsync. Builds a real temporary git repo (and a bare "origin") under
tmp_path so push actually exercises `git push`, without touching the real repo or any network."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from kosaccounts import gitsync
from kosaccounts.models import RunSummary


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A local repo with the three OUTPUT_PATHS committed, plus an unrelated README.md, and a
    bare "origin" it can push to without network access."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "output").mkdir()
    (root / "data").mkdir()

    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")

    (root / "output" / "kosibah_import.xlsx").write_text("workbook v1")
    (root / "data" / "suppliers.csv").write_text("Supplier\n")
    (root / "data" / "supplier_aliases.csv").write_text("Alias\n")
    (root / "README.md").write_text("unrelated file\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True, text=True)
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "-u", "origin", "HEAD")

    return root, bare


class TestChangedOutputPaths:
    def test_no_changes(self, repo: tuple[Path, Path]) -> None:
        root, _bare = repo
        assert gitsync.changed_output_paths(root) == []

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "not_a_repo"
        root.mkdir()
        assert gitsync.changed_output_paths(root) == []


class TestCommitOutputs:
    def test_no_changes_returns_nothing_and_does_not_commit(self, repo: tuple[Path, Path]) -> None:
        root, _bare = repo
        before = _git(root, "rev-parse", "HEAD")

        result = gitsync.commit_outputs(root, "Workbook update", push=False)

        assert result == "nothing"
        assert _git(root, "rev-parse", "HEAD") == before

    def test_modified_workbook_is_committed_without_unrelated_dirty_file(
        self, repo: tuple[Path, Path]
    ) -> None:
        root, _bare = repo
        before = _git(root, "rev-parse", "HEAD")
        (root / "output" / "kosibah_import.xlsx").write_text("workbook v2")
        (root / "README.md").write_text("dirty, unrelated change\n")

        result = gitsync.commit_outputs(root, "Workbook update", push=False)

        assert result == "committed"
        after = _git(root, "rev-parse", "HEAD")
        assert after != before

        changed_files = _git(root, "show", "--name-only", "--pretty=format:", "HEAD").split()
        assert changed_files == ["output/kosibah_import.xlsx"]

        # README.md's change was never staged/committed; it's still dirty.
        status = _git(root, "status", "--porcelain", "--", "README.md")
        assert "README.md" in status

    def test_push_true_advances_bare_origin(self, repo: tuple[Path, Path]) -> None:
        root, bare = repo
        (root / "data" / "suppliers.csv").write_text("Supplier\nAcme\n")

        result = gitsync.commit_outputs(root, "Workbook update", push=True)

        assert result == "pushed"
        local_head = _git(root, "rev-parse", "HEAD").strip()
        bare_head = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        assert local_head == bare_head

    def test_push_failure_keeps_local_commit(self, repo: tuple[Path, Path], tmp_path: Path) -> None:
        root, _bare = repo
        _git(root, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
        (root / "output" / "kosibah_import.xlsx").write_text("workbook v3")

        result = gitsync.commit_outputs(root, "Workbook update", push=True)

        assert result == "push-failed"
        log = _git(root, "log", "-1", "--pretty=%s")
        assert log.strip() == "Workbook update"

    def test_not_a_git_repo_returns_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "not_a_repo"
        root.mkdir(parents=True)
        (root / "output").mkdir()
        (root / "output" / "kosibah_import.xlsx").write_text("workbook")

        result = gitsync.commit_outputs(root, "Workbook update", push=False)

        assert result == "nothing"


class TestRunMessage:
    def test_format(self) -> None:
        summary = RunSummary(
            started=datetime(2026, 9, 21, 20, 41),
            rows_added={"expense": 3, "bank": 0},
            rows_review=0,
            rows_superseded=0,
        )
        assert gitsync.run_message(summary) == (
            "Workbook update 2026-09-21 20:41: expense rows +3, bank rows +0, review 0, superseded 0"
        )

    def test_missing_stage_defaults_to_zero(self) -> None:
        summary = RunSummary(
            started=datetime(2026, 1, 5, 9, 0),
            rows_added={"expense": 2},
            rows_review=1,
            rows_superseded=4,
        )
        assert gitsync.run_message(summary) == (
            "Workbook update 2026-01-05 09:00: expense rows +2, bank rows +0, review 1, superseded 4"
        )
