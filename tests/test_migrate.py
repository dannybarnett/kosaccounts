"""Tests for scripts/migrate_receipts_to_expenses.py (not a package module, so it is loaded by
file path via importlib). Uses the shared `cfg`/`tmp_repo` fixtures from conftest.py so paths
(ledger, output workbook) come from a real Config, same as the script itself uses."""

from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kosaccounts.config import Config
from kosaccounts.ledger import Ledger
from kosaccounts.models import BankRow, LedgerEntry, PurchaseRow
from kosaccounts.workbook import OutputWorkbook

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_receipts_to_expenses.py"

_spec = importlib.util.spec_from_file_location("migrate_receipts_to_expenses", SCRIPT_PATH)
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrate
_spec.loader.exec_module(migrate)  # type: ignore[union-attr]


def _make_purchase_row(source_file: str) -> PurchaseRow:
    return PurchaseRow(
        date=date(2026, 9, 1),
        company="Mood Fabrics",
        category="Fabric",
        net=Decimal("100.00"),
        sales_tax=Decimal("8.88"),
        total=Decimal("108.88"),
        tax_rate=Decimal("8.875"),
        payment_method="Visa",
        notes="",
        year=2026,
        source_file=source_file,
        processed_on=datetime(2026, 9, 1, 10, 0, 0),
        status="OK",
    )


def _make_bank_row(matched_source: str) -> BankRow:
    return BankRow(
        date=date(2026, 9, 1),
        description="MOOD FABRICS NYC",
        amount=Decimal("108.88"),
        direction="debit",
        status="Matched",
        matched_source=matched_source,
        ref="ref-1",
        statement_file="bank/statement_sep.csv",
        statement_start=date(2026, 9, 1),
        statement_end=date(2026, 9, 30),
        processed_on=datetime(2026, 9, 1, 10, 0, 0),
    )


@pytest.fixture
def seeded_ledger(cfg: Config) -> Ledger:
    ledger = Ledger(cfg.paths.ledger)
    ledger.append(
        LedgerEntry(
            filename="X.pdf",
            relative_path="receipts/X.pdf",
            sha256="abc123",
            size=10,
            date_processed=datetime(2026, 9, 1, 10, 0, 0),
            stage="receipt",  # type: ignore[arg-type]
            status="Processed",
            rows_added=1,
            notes="",
        )
    )
    # A bank-stage row must be left alone: no "receipts/" prefix, no "receipt" stage.
    ledger.append(
        LedgerEntry(
            filename="statement.csv",
            relative_path="bank/statement.csv",
            sha256="def456",
            size=20,
            date_processed=datetime(2026, 9, 1, 10, 0, 0),
            stage="bank",  # type: ignore[arg-type]
            status="Processed",
            rows_added=1,
            notes="",
        )
    )
    return ledger


@pytest.fixture
def seeded_workbook(cfg: Config) -> OutputWorkbook:
    wb = OutputWorkbook(cfg.paths.output_workbook)
    wb.append([_make_purchase_row("receipts/X.pdf")])
    wb.append_bank([_make_bank_row("receipts/X.pdf")])
    wb.save()
    return wb


class TestMigrateLedger:
    def test_renames_prefix_and_stage(self, cfg: Config, seeded_ledger: Ledger) -> None:
        changed = migrate.migrate_ledger(cfg.paths.ledger, dry_run=False)
        assert changed == 1

        with cfg.paths.ledger.open("r", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        by_filename = {r["Filename"]: r for r in rows}
        assert by_filename["X.pdf"]["Relative path"] == "expenses/X.pdf"
        assert by_filename["X.pdf"]["Stage"] == "expense"
        # The bank-stage row is untouched.
        assert by_filename["statement.csv"]["Relative path"] == "bank/statement.csv"
        assert by_filename["statement.csv"]["Stage"] == "bank"

        backups = list(cfg.paths.ledger.parent.glob("processing_log.csv.bak-*"))
        assert len(backups) == 1

    def test_idempotent_second_run_is_a_no_op(self, cfg: Config, seeded_ledger: Ledger) -> None:
        migrate.migrate_ledger(cfg.paths.ledger, dry_run=False)
        before = cfg.paths.ledger.read_text()

        changed = migrate.migrate_ledger(cfg.paths.ledger, dry_run=False)
        assert changed == 0
        assert cfg.paths.ledger.read_text() == before
        # No second backup: nothing changed, so nothing was backed up again.
        backups = list(cfg.paths.ledger.parent.glob("processing_log.csv.bak-*"))
        assert len(backups) == 1

    def test_dry_run_writes_nothing(self, cfg: Config, seeded_ledger: Ledger) -> None:
        before = cfg.paths.ledger.read_text()
        changed = migrate.migrate_ledger(cfg.paths.ledger, dry_run=True)
        assert changed == 1
        assert cfg.paths.ledger.read_text() == before
        assert list(cfg.paths.ledger.parent.glob("processing_log.csv.bak-*")) == []

    def test_missing_file_returns_zero(self, cfg: Config) -> None:
        assert migrate.migrate_ledger(cfg.paths.ledger, dry_run=False) == 0


class TestMigrateWorkbook:
    def test_renames_source_file_and_matched_source(
        self, cfg: Config, seeded_workbook: OutputWorkbook
    ) -> None:
        purchase_changed, bank_changed = migrate.migrate_workbook(cfg.paths.output_workbook, dry_run=False)
        assert purchase_changed == 1
        assert bank_changed == 1

        wb = OutputWorkbook(cfg.paths.output_workbook)
        rows = wb.rows()
        assert len(rows) == 1
        assert rows[0].source_file == "expenses/X.pdf"

        bank_rows = wb.bank_rows()
        assert len(bank_rows) == 1
        assert bank_rows[0].matched_source == "expenses/X.pdf"

        backups = list(cfg.paths.output_workbook.parent.glob("kosibah_import.xlsx.bak-*"))
        assert len(backups) == 1

    def test_idempotent_second_run_is_a_no_op(
        self, cfg: Config, seeded_workbook: OutputWorkbook
    ) -> None:
        migrate.migrate_workbook(cfg.paths.output_workbook, dry_run=False)

        purchase_changed, bank_changed = migrate.migrate_workbook(cfg.paths.output_workbook, dry_run=False)
        assert (purchase_changed, bank_changed) == (0, 0)
        backups = list(cfg.paths.output_workbook.parent.glob("kosibah_import.xlsx.bak-*"))
        assert len(backups) == 1

    def test_dry_run_writes_nothing(self, cfg: Config, seeded_workbook: OutputWorkbook) -> None:
        purchase_changed, bank_changed = migrate.migrate_workbook(cfg.paths.output_workbook, dry_run=True)
        assert (purchase_changed, bank_changed) == (1, 1)

        wb = OutputWorkbook(cfg.paths.output_workbook)
        assert wb.rows()[0].source_file == "receipts/X.pdf"
        assert wb.bank_rows()[0].matched_source == "receipts/X.pdf"
        assert list(cfg.paths.output_workbook.parent.glob("kosibah_import.xlsx.bak-*")) == []

    def test_missing_file_returns_zero(self, cfg: Config) -> None:
        assert migrate.migrate_workbook(cfg.paths.output_workbook, dry_run=False) == (0, 0)


class TestMigrateMain:
    def test_end_to_end_via_main(
        self, cfg: Config, seeded_ledger: Ledger, seeded_workbook: OutputWorkbook
    ) -> None:
        config_path = str(cfg.paths.root / "config.toml")

        result = migrate.main(["--config", config_path])
        assert result == 0

        with cfg.paths.ledger.open("r", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["Relative path"] == "expenses/X.pdf"
        assert rows[0]["Stage"] == "expense"

        wb = OutputWorkbook(cfg.paths.output_workbook)
        assert wb.rows()[0].source_file == "expenses/X.pdf"
        assert wb.bank_rows()[0].matched_source == "expenses/X.pdf"

        # Running it again changes nothing further.
        result2 = migrate.main(["--config", config_path])
        assert result2 == 0
        wb2 = OutputWorkbook(cfg.paths.output_workbook)
        assert wb2.rows()[0].source_file == "expenses/X.pdf"
