"""Command line entry point: python -m kosaccounts <command>

Commands:
  seed  --master PATH        build data/*.csv from the master workbook (seed.py)
  scan                       list new (unprocessed) files without doing anything
  run   [--dry-run] [--stage receipt|bank]   the hourly pipeline (pipeline.py)
  review [--approve "Name=Category" ...]     show pending suppliers / Review rows; approve moves a
                                             pending supplier into suppliers.csv
  reconcile-ledger           report ledger entries whose source file is gone

Each command is a function `cmd_<name>(args, cfg) -> int` so tests can call them directly.
`main(argv=None) -> int` parses args, loads config (--config PATH optional) and dispatches.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from kosaccounts.categories import Categories
from kosaccounts.config import Config, load_config
from kosaccounts.discovery import discover, new_files
from kosaccounts.ledger import Ledger
from kosaccounts.pipeline import run_pipeline
from kosaccounts.seed import seed_from_master
from kosaccounts.summary import format_summary
from kosaccounts.suppliers import normalise
from kosaccounts.workbook import OutputWorkbook

logger = logging.getLogger("kosaccounts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kosaccounts")
    parser.add_argument("--config", type=str, default=None, help="Path to config.toml")

    subparsers = parser.add_subparsers(dest="command")

    seed_parser = subparsers.add_parser("seed", help="Build data/*.csv from the master workbook")
    seed_parser.add_argument("--master", type=str, default=None)
    seed_parser.set_defaults(func=cmd_seed)

    scan_parser = subparsers.add_parser("scan", help="List new (unprocessed) files")
    scan_parser.set_defaults(func=cmd_scan)

    run_parser = subparsers.add_parser("run", help="Run the pipeline")
    run_parser.add_argument("--dry-run", action="store_true", default=False)
    run_parser.add_argument(
        "--stage",
        action="append",
        choices=["receipt", "bank"],
        dest="stage",
        default=None,
        help="Restrict the run to one stage; repeatable",
    )
    run_parser.set_defaults(func=cmd_run)

    review_parser = subparsers.add_parser("review", help="Review pending suppliers / flagged rows")
    review_parser.add_argument(
        "--approve",
        action="append",
        dest="approve",
        default=[],
        metavar="Name=Category",
        help='Approve a pending supplier, e.g. --approve "New Co=Fabric"; repeatable',
    )
    review_parser.set_defaults(func=cmd_review)

    reconcile_parser = subparsers.add_parser(
        "reconcile-ledger", help="Report ledger entries whose source file is gone"
    )
    reconcile_parser.set_defaults(func=cmd_reconcile_ledger)

    dup_parser = subparsers.add_parser(
        "duplicates", help="Suggest likely duplicate suppliers in data/suppliers.csv"
    )
    dup_parser.add_argument("--min-score", type=float, default=85.0)
    dup_parser.add_argument(
        "--out", help="Also write the report as CSV (default data/supplier_duplicates_suggested.csv)",
        nargs="?", const="data/supplier_duplicates_suggested.csv", default=None,
    )
    dup_parser.set_defaults(func=cmd_duplicates)

    return parser


def cmd_seed(args: argparse.Namespace, cfg: Config) -> int:
    master = Path(args.master) if getattr(args, "master", None) else cfg.paths.master_workbook
    stats = seed_from_master(master, cfg.paths.data)
    print(stats)
    return 0


def cmd_scan(args: argparse.Namespace, cfg: Config) -> int:
    ledger = Ledger(cfg.paths.ledger)
    discovered = discover(cfg.paths.imports, ("receipt", "bank"))
    new = new_files(discovered, ledger)

    for file in new:
        print(f"{file.stage}\t{file.relative_path}\t{file.size}")

    print(f"{len(new)} new files ({len(discovered)} total)")
    return 0


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    summary = run_pipeline(cfg, dry_run=args.dry_run, stages=args.stage or None)
    print(format_summary(summary))
    return 1 if summary.errors else 0


def _read_csv_dicts(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


PENDING_FIELDNAMES = ["Supplier", "Suggested category", "First seen", "Source file", "Context", "Occurrences"]
SUPPLIER_FIELDNAMES = ["Supplier", "Default category", "Other categories", "Count", "Last date"]


def cmd_review(args: argparse.Namespace, cfg: Config) -> int:
    pending_path = cfg.paths.pending_suppliers
    pending_fieldnames, pending_rows = _read_csv_dicts(pending_path)
    if not pending_fieldnames:
        pending_fieldnames = PENDING_FIELDNAMES

    print("Pending suppliers:")
    if not pending_rows:
        print("  (none)")
    else:
        print(f"  {'Supplier':<30} {'Suggested category':<25} {'Occurrences':<11} {'First seen':<20} Context")
        for row in pending_rows:
            supplier = row.get("Supplier", "")
            suggested = row.get("Suggested category", "")
            occurrences = row.get("Occurrences", "")
            first_seen = row.get("First seen", "")
            context = (row.get("Context", "") or "")[:40]
            print(f"  {supplier:<30} {suggested:<25} {occurrences:<11} {first_seen:<20} {context}")

    print()
    print("Review rows:")
    review_rows = []
    if cfg.paths.output_workbook.exists():
        wb = OutputWorkbook(cfg.paths.output_workbook)
        review_rows = [row for row in wb.rows() if row.status == "Review"]
    if not review_rows:
        print("  (none)")
    else:
        print(f"  {'Date':<12} {'Company':<30} {'Total':<10} {'Status':<8} Notes")
        for row in review_rows:
            notes = (row.notes or "")[:60]
            print(f"  {row.date.isoformat():<12} {row.company:<30} {str(row.total):<10} {row.status:<8} {notes}")

    approvals: list[str] = getattr(args, "approve", None) or []
    if not approvals:
        return 0

    categories = Categories(cfg.paths.categories)
    parsed: list[tuple[str, str]] = []
    for item in approvals:
        if "=" not in item:
            print(f"Invalid --approve value (expected Name=Category): {item!r}")
            return 1
        name, _, category = item.partition("=")
        name = name.strip()
        category = category.strip()
        if not name or not categories.is_valid(category):
            print(f"Invalid category: {category!r} (not in {cfg.paths.categories})")
            return 1
        parsed.append((name, categories.canonical(category) or category))

    supplier_fieldnames, supplier_rows = _read_csv_dicts(cfg.paths.suppliers)
    if not supplier_fieldnames:
        supplier_fieldnames = SUPPLIER_FIELDNAMES

    for name, category in parsed:
        supplier_rows.append(
            {
                "Supplier": name,
                "Default category": category,
                "Other categories": "",
                "Count": "0",
                "Last date": "",
            }
        )
        target = normalise(name)
        pending_rows = [row for row in pending_rows if normalise(row.get("Supplier", "")) != target]

    _write_csv_atomic(cfg.paths.suppliers, supplier_fieldnames, supplier_rows)
    _write_csv_atomic(pending_path, pending_fieldnames, pending_rows)

    for name, category in parsed:
        print(f"Approved: {name} -> {category}")

    return 0


def cmd_reconcile_ledger(args: argparse.Namespace, cfg: Config) -> int:
    ledger = Ledger(cfg.paths.ledger)
    missing = ledger.missing_sources(cfg.paths.imports)
    for entry in missing:
        print(f"{entry.relative_path} (processed {entry.date_processed.date().isoformat()})")
    print(f"{len(missing)} missing source file(s)")
    return 0


def cmd_duplicates(args: argparse.Namespace, cfg: Config) -> int:
    from kosaccounts.duplicates import suggest_duplicates, write_report

    pairs = suggest_duplicates(cfg.paths.suppliers, min_score=args.min_score)
    for p in pairs:
        print(f"{p.score:5.1f}  {p.a} ({p.count_a})  <->  {p.b} ({p.count_b})  => {p.suggested_canonical}")
    print(f"{len(pairs)} suggested duplicate pair(s)")
    if args.out:
        out = write_report(pairs, (cfg.paths.root / args.out).resolve())
        print(f"written {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2

    cfg = load_config(Path(args.config) if args.config else None)
    return func(args, cfg)
