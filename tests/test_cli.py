"""Tests for kosaccounts.cli. Never calls the real `claude` binary: the only `run` scenario
exercised here has zero new files, so run_pipeline() returns via the nothing_to_do fast path before
ever constructing a ClaudeClient.
"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from kosaccounts.cli import build_parser, main
from kosaccounts.config import Config, load_config
from kosaccounts.ledger import Ledger
from kosaccounts.models import LedgerEntry

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_DATA = REPO_ROOT / "data"
DATA_FILES = ["suppliers.csv", "supplier_aliases.csv", "categories.csv", "payment_methods.csv"]


@pytest.fixture
def cfg(tmp_repo: Path) -> Config:
    """Overrides tests/conftest.py's `cfg`: same tmp_repo, but seeded with the repo's real
    data/*.csv so category validation / supplier lookups behave like production."""
    for name in DATA_FILES:
        shutil.copy(REPO_DATA / name, tmp_repo / "data" / name)
    return load_config(tmp_repo / "config.toml")


def _config_path(cfg: Config) -> str:
    return str(cfg.paths.root / "config.toml")


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_prog_name(self) -> None:
        parser = build_parser()
        assert parser.prog == "kosaccounts"

    def test_run_subcommand_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run", "--dry-run", "--stage", "expense", "--stage", "bank"])
        assert args.dry_run is True
        assert args.stage == ["expense", "bank"]
        assert args.func.__name__ == "cmd_run"

    def test_seed_subcommand_default_func(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["seed", "--master", "somewhere.xlsx"])
        assert args.master == "somewhere.xlsx"
        assert args.func.__name__ == "cmd_seed"

    def test_review_approve_repeatable(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["review", "--approve", "A=B", "--approve", "C=D"])
        assert args.approve == ["A=B", "C=D"]


# ---------------------------------------------------------------------------
# main: no subcommand
# ---------------------------------------------------------------------------


class TestMainNoSubcommand:
    def test_returns_2_and_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main([])
        assert result == 2
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


class TestScan:
    def test_lists_new_files(self, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
        expense_path = cfg.paths.imports / "expenses" / "expense1.pdf"
        expense_path.parent.mkdir(parents=True, exist_ok=True)
        expense_path.write_bytes(b"%PDF-1.4 fake receipt")

        result = main(["--config", _config_path(cfg), "scan"])
        assert result == 0

        captured = capsys.readouterr()
        size = expense_path.stat().st_size
        assert f"expense\texpenses/expense1.pdf\t{size}" in captured.out
        assert "1 new files (1 total)" in captured.out

    def test_no_files_reports_zero(self, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--config", _config_path(cfg), "scan"])
        assert result == 0
        captured = capsys.readouterr()
        assert "0 new files (0 total)" in captured.out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRun:
    def test_dry_run_nothing_to_do(self, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--config", _config_path(cfg), "run", "--dry-run"])
        assert result == 0

        captured = capsys.readouterr()
        assert "nothing to do" in captured.out

    def test_folder_argument_matching_stage_is_accepted(
        self, cfg: Config, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The watcher invokes `run --stage expense <imports_folder>`; the trailing folder
        argument must not be rejected by argparse, and must agree with an explicit --stage."""
        folder = str(cfg.paths.imports / "expenses")
        result = main(
            ["--config", _config_path(cfg), "run", "--dry-run", "--stage", "expense", folder]
        )
        assert result == 0
        captured = capsys.readouterr()
        assert "nothing to do" in captured.out

    def test_folder_argument_alone_derives_stage(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no --stage given, the folder argument's basename picks the stage."""
        import kosaccounts.cli as cli_module

        captured_stages: dict = {}
        original_run_pipeline = cli_module.run_pipeline

        def spy_run_pipeline(cfg_, dry_run, stages):
            captured_stages["stages"] = stages
            return original_run_pipeline(cfg_, dry_run=dry_run, stages=stages)

        monkeypatch.setattr(cli_module, "run_pipeline", spy_run_pipeline)

        folder = str(cfg.paths.imports / "bank")
        result = main(["--config", _config_path(cfg), "run", "--dry-run", folder])
        assert result == 0
        assert captured_stages["stages"] == ["bank"]

    def test_folder_argument_contradicting_stage_returns_2(
        self, cfg: Config, capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = str(cfg.paths.imports / "expenses")
        result = main(
            ["--config", _config_path(cfg), "run", "--dry-run", "--stage", "bank", folder]
        )
        assert result == 2
        captured = capsys.readouterr()
        assert "contradicts" in captured.err

    def test_unrecognised_folder_argument_returns_2(
        self, cfg: Config, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = main(["--config", _config_path(cfg), "run", "--dry-run", "/tmp/not-a-folder"])
        assert result == 2
        captured = capsys.readouterr()
        assert "Unrecognised imports folder" in captured.err

    def test_calls_commit_outputs_when_summary_did_work(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import kosaccounts.cli as cli_module
        from kosaccounts.models import RunSummary

        summary = RunSummary(started=datetime(2026, 9, 21, 20, 41), files_new={"expense": 1})
        monkeypatch.setattr(cli_module, "run_pipeline", lambda cfg_, dry_run, stages: summary)

        calls: list[dict] = []

        def fake_commit_outputs(root, message, push):
            calls.append({"root": root, "message": message, "push": push})
            return "committed"

        monkeypatch.setattr(cli_module.gitsync, "commit_outputs", fake_commit_outputs)

        result = main(["--config", _config_path(cfg), "run"])
        assert result == 0

        assert len(calls) == 1
        assert calls[0]["root"] == cfg.paths.root
        assert calls[0]["push"] == cfg.processing.push_outputs
        captured = capsys.readouterr()
        assert "git: committed" in captured.out

    def test_skips_commit_outputs_on_dry_run(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import kosaccounts.cli as cli_module
        from kosaccounts.models import RunSummary

        summary = RunSummary(started=datetime(2026, 9, 21, 20, 41), files_new={"expense": 1})
        monkeypatch.setattr(cli_module, "run_pipeline", lambda cfg_, dry_run, stages: summary)

        calls: list[dict] = []
        monkeypatch.setattr(
            cli_module.gitsync, "commit_outputs", lambda root, message, push: calls.append(1) or "committed"
        )

        result = main(["--config", _config_path(cfg), "run", "--dry-run"])
        assert result == 0
        assert calls == []
        captured = capsys.readouterr()
        assert "git:" not in captured.out

    def test_skips_commit_outputs_when_nothing_to_do(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No new files -> summary.nothing_to_do is True even on a non-dry-run."""
        import kosaccounts.cli as cli_module

        calls: list[dict] = []
        monkeypatch.setattr(
            cli_module.gitsync, "commit_outputs", lambda root, message, push: calls.append(1) or "committed"
        )

        result = main(["--config", _config_path(cfg), "run"])
        assert result == 0
        assert calls == []
        captured = capsys.readouterr()
        assert "git:" not in captured.out


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


class TestReview:
    def test_approve_moves_supplier_and_rejects_invalid_category(
        self, cfg: Config, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pending_path = cfg.paths.pending_suppliers
        with pending_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["Supplier", "Suggested category", "First seen", "Source file", "Context", "Occurrences"]
            )
            writer.writerow(["New Co", "", "2026-09-01T10:00:00", "expenses/x.pdf", "some context", "1"])

        result = main(["--config", _config_path(cfg), "review", "--approve", "New Co=Fabric"])
        assert result == 0
        captured = capsys.readouterr()
        assert "Approved: New Co -> Fabric" in captured.out

        with cfg.paths.suppliers.open() as fh:
            supplier_rows = list(csv.DictReader(fh))
        assert any(
            row["Supplier"] == "New Co" and row["Default category"] == "Fabric" for row in supplier_rows
        )

        with pending_path.open() as fh:
            pending_rows = list(csv.DictReader(fh))
        assert not any(row["Supplier"] == "New Co" for row in pending_rows)

        # An invalid category is rejected and nothing further is written.
        supplier_count_before = len(supplier_rows)
        result2 = main(
            ["--config", _config_path(cfg), "review", "--approve", "Another Co=Not A Real Category"]
        )
        assert result2 == 1

        with cfg.paths.suppliers.open() as fh:
            supplier_rows_after = list(csv.DictReader(fh))
        assert len(supplier_rows_after) == supplier_count_before
        assert not any(row["Supplier"] == "Another Co" for row in supplier_rows_after)

    def test_review_with_no_pending_or_review_rows(
        self, cfg: Config, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = main(["--config", _config_path(cfg), "review"])
        assert result == 0
        captured = capsys.readouterr()
        assert "(none)" in captured.out


# ---------------------------------------------------------------------------
# reconcile-ledger
# ---------------------------------------------------------------------------


class TestReconcileLedger:
    def test_lists_missing_source(self, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
        ledger = Ledger(cfg.paths.ledger)
        ledger.append(
            LedgerEntry(
                filename="gone.pdf",
                relative_path="expenses/gone.pdf",
                sha256="deadbeef",
                size=100,
                date_processed=datetime(2026, 9, 1, 10, 0, 0),
                stage="expense",
                status="Processed",
                rows_added=1,
                notes="",
            )
        )

        result = main(["--config", _config_path(cfg), "reconcile-ledger"])
        assert result == 0

        captured = capsys.readouterr()
        assert "expenses/gone.pdf (processed 2026-09-01)" in captured.out
        assert "1 missing source file(s)" in captured.out


def test_duplicates_command_lists_pairs_and_writes_csv(tmp_repo, capsys):
    import csv as _csv
    from kosaccounts.cli import main

    data = tmp_repo / "data"
    with (data / "suppliers.csv").open("w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["Supplier", "Default category", "Other categories", "Count", "Last date"])
        w.writerow(["Amazon", "Office supplies", "", 2, "2018-11-16"])
        w.writerow(["Amazon.com", "Office supplies", "", 70, "2026-06-10"])
        w.writerow(["Mood", "Fabric", "", 27, "2026-05-01"])
    rc = main(["--config", str(tmp_repo / "config.toml"), "duplicates", "--out"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Amazon (2)  <->  Amazon.com (70)  => Amazon.com" in out
    assert "1 suggested duplicate pair(s)" in out
    assert (data / "supplier_duplicates_suggested.csv").exists()
