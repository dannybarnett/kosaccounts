#!/usr/bin/env python3
"""One-off data migration: rewrite "receipts/" path prefixes to "expenses/" (and the ledger's
Stage column "receipt" -> "expense") after the kosaccounts.receipts -> kosaccounts.expenses
rename landed in code and config.

Touches two files (paths come from Config, so --config works the same as for `kosaccounts run`):
  - data/processing_log.csv: "Relative path" column prefix, "Stage" column value.
  - output/kosibah_import.xlsx: Purchases "Source file" column, Bank "Matched receipt" column
    (both hold a relative_path-shaped string), via OutputWorkbook so formatting/formulas/tables
    are re-applied the normal way and the save is atomic.

Each file is backed up (a "<name>.bak-YYYYMMDD-HHMMSS" sibling) before being rewritten. Nothing is
backed up or written when there is nothing to change, so re-running this script after it has
already migrated everything (or against files that never used "receipts/") is a no-op: idempotent.

Usage:
    .venv/bin/python scripts/migrate_receipts_to_expenses.py [--config PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kosaccounts.config import load_config  # noqa: E402
from kosaccounts.workbook import OutputWorkbook  # noqa: E402

OLD_PREFIX = "receipts/"
NEW_PREFIX = "expenses/"
OLD_STAGE = "receipt"
NEW_STAGE = "expense"


def _rewrite_path(value: str) -> str:
    if value.startswith(OLD_PREFIX):
        return NEW_PREFIX + value[len(OLD_PREFIX):]
    return value


def _backup(path: Path) -> Path:
    """Copy `path` to a "<name>.bak-YYYYMMDD-HHMMSS" sibling and return that path."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def migrate_ledger(path: Path, dry_run: bool) -> int:
    """Rewrite data/processing_log.csv in place. Returns the number of rows changed."""
    if not path.exists():
        print(f"ledger:   {path} does not exist, skipping")
        return 0

    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    changed = 0
    for row in rows:
        relative_path = row.get("Relative path", "")
        new_relative_path = _rewrite_path(relative_path)
        stage = row.get("Stage", "")
        new_stage = NEW_STAGE if stage == OLD_STAGE else stage
        if new_relative_path != relative_path or new_stage != stage:
            row["Relative path"] = new_relative_path
            row["Stage"] = new_stage
            changed += 1

    if changed == 0:
        print("ledger:   nothing to change")
        return 0

    if dry_run:
        print(f"ledger:   {changed} row(s) would be rewritten (dry run)")
        return changed

    backup_path = _backup(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"ledger:   {changed} row(s) rewritten (backup: {backup_path.name})")
    return changed


def migrate_workbook(path: Path, dry_run: bool) -> tuple[int, int]:
    """Rewrite output/kosibah_import.xlsx's Purchases source_file and Bank matched_source columns
    in place. Returns (purchase rows changed, bank rows changed)."""
    if not path.exists():
        print(f"workbook: {path} does not exist, skipping")
        return 0, 0

    wb = OutputWorkbook(path)

    purchase_rows_to_update = []
    for row in wb.rows():
        new_source_file = _rewrite_path(row.source_file)
        if new_source_file != row.source_file:
            row.source_file = new_source_file
            purchase_rows_to_update.append(row)

    bank_rows_to_update = []
    for row in wb.bank_rows():
        new_matched_source = _rewrite_path(row.matched_source)
        if new_matched_source != row.matched_source:
            row.matched_source = new_matched_source
            bank_rows_to_update.append(row)

    if not purchase_rows_to_update and not bank_rows_to_update:
        print("workbook: nothing to change")
        return 0, 0

    if dry_run:
        print(
            f"workbook: {len(purchase_rows_to_update)} purchase row(s), "
            f"{len(bank_rows_to_update)} bank row(s) would be rewritten (dry run)"
        )
        return len(purchase_rows_to_update), len(bank_rows_to_update)

    backup_path = _backup(path)
    if purchase_rows_to_update:
        wb.update(purchase_rows_to_update)
    if bank_rows_to_update:
        wb.update_bank(bank_rows_to_update)
    wb.save()
    print(
        f"workbook: {len(purchase_rows_to_update)} purchase row(s), "
        f"{len(bank_rows_to_update)} bank row(s) rewritten (backup: {backup_path.name})"
    )
    return len(purchase_rows_to_update), len(bank_rows_to_update)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=str, default=None, help="Path to config.toml")
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Report what would change; write nothing"
    )
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)

    print(f"ledger:   {cfg.paths.ledger}")
    print(f"workbook: {cfg.paths.output_workbook}")
    if args.dry_run:
        print("(dry run: no files will be written)")
    print()

    ledger_changed = migrate_ledger(cfg.paths.ledger, args.dry_run)
    purchase_changed, bank_changed = migrate_workbook(cfg.paths.output_workbook, args.dry_run)

    print()
    print(
        f"Summary: ledger rows changed={ledger_changed}, "
        f"purchase rows changed={purchase_changed}, bank rows changed={bank_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
