"""The `run` command's orchestration (plan §11), kept separate from argument parsing.

run_pipeline(cfg, dry_run=False, stages=None) -> RunSummary:
1. discover + new_files (ledger).  If none: summary.nothing_to_do, write summary, return.
2. Load Categories, payment methods, SupplierBook(client), OutputWorkbook.
3. Receipt stage: for each new receipt file: extract_receipt -> suppliers.match(supplier_name,
   context=line_summary) -> to_purchase_row -> collect. Per-file exceptions are caught: ledger
   Status="Error", summary.errors, continue.
4. Bank stage: parse_statement per file (flagged -> ledger "Flagged"); collect txns.
5. reconcile(workbook.rows() + new receipt rows, txns, ...).
6. If cfg.processing.block_on_flags and any Review row/flagged file: write summary, ledger nothing,
   return with summary.errors += ["blocked: flags present"].
7. Unless dry_run: workbook.append(receipt rows + reconcile.new_rows); workbook.update(reconcile.updated_rows);
   workbook.save(); suppliers.save(); ledger.append per file (Processed/Flagged, rows_added).
8. write_summary; return summary.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

from kosaccounts.bank.registry import parse_statement
from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeClient
from kosaccounts.config import Config
from kosaccounts.discovery import discover, new_files
from kosaccounts.ledger import Ledger
from kosaccounts.models import LedgerEntry, PurchaseRow, ReconcileResult, RunSummary, Stage
from kosaccounts.receipts.extract import cfg_payment_methods, extract_receipt, to_purchase_row
from kosaccounts.reconcile import reconcile
from kosaccounts.suppliers import SupplierBook
from kosaccounts.summary import write_summary
from kosaccounts.workbook import OutputWorkbook

logger = logging.getLogger("kosaccounts")


def run_pipeline(cfg: Config, dry_run: bool = False, stages: Optional[Sequence[Stage]] = None) -> RunSummary:
    run_stages: list[Stage] = list(stages) if stages is not None else list(cfg.processing.stages)  # type: ignore[arg-type]
    run_time = datetime.now()
    summary = RunSummary(started=run_time, dry_run=dry_run)

    ledger = Ledger(cfg.paths.ledger)
    discovered = discover(cfg.paths.imports, run_stages)
    new_list = new_files(discovered, ledger)

    for stage in run_stages:
        summary.files_found[stage] = sum(1 for f in discovered if f.stage == stage)
        summary.files_new[stage] = sum(1 for f in new_list if f.stage == stage)
        summary.files_flagged[stage] = 0
        summary.files_errored[stage] = 0

    if summary.nothing_to_do:
        summary.finished = datetime.now()
        write_summary(summary, cfg.paths.logs)
        return summary

    # --- Step 2: shared resources, built once ---
    categories = Categories(cfg.paths.categories)
    payment_methods = cfg_payment_methods(cfg)
    client = ClaudeClient(cfg.claude, cwd=cfg.paths.root)
    suppliers = SupplierBook(cfg, categories, client)
    workbook = OutputWorkbook(cfg.paths.output_workbook)

    receipt_rows: list[PurchaseRow] = []
    pending_ledger_entries: list[LedgerEntry] = []
    new_supplier_names: set[str] = set()
    bank_flagged_any = False

    new_receipt_files = [f for f in new_list if f.stage == "receipt"]
    new_bank_files = [f for f in new_list if f.stage == "bank"]

    # --- Step 3: receipt stage ---
    for file in new_receipt_files:
        try:
            extract = extract_receipt(file, client, cfg, payment_methods)
            match = suppliers.match(
                extract.supplier_name or "",
                context=extract.line_summary or "",
                source_file=file.relative_path,
            )
            row = to_purchase_row(extract, match, categories, cfg, run_time)
        except Exception as exc:  # per-file isolation: any exception here must not abort the run
            notes = f"{type(exc).__name__}: {exc}"[:200]
            summary.errors.append(f"{file.relative_path}: {exc}")
            summary.files_errored["receipt"] += 1
            pending_ledger_entries.append(
                LedgerEntry(
                    filename=file.filename,
                    relative_path=file.relative_path,
                    sha256=file.sha256,
                    size=file.size,
                    date_processed=run_time,
                    stage="receipt",
                    status="Error",
                    rows_added=0,
                    notes=notes,
                )
            )
            logger.exception("receipt: %s -> Error", file.relative_path)
            continue

        receipt_rows.append(row)
        if match.is_new:
            new_supplier_names.add(row.company)

        file_status = "Flagged" if row.status == "Review" else "Processed"
        if file_status == "Flagged":
            summary.files_flagged["receipt"] += 1
        pending_ledger_entries.append(
            LedgerEntry(
                filename=file.filename,
                relative_path=file.relative_path,
                sha256=file.sha256,
                size=file.size,
                date_processed=run_time,
                stage="receipt",
                status=file_status,
                rows_added=1,
                notes="",
            )
        )
        logger.info("receipt: %s -> %s %s %s", file.relative_path, row.company, row.total, row.status)

    # --- Step 4: bank stage ---
    all_txns = []
    for file in new_bank_files:
        try:
            txns, flagged = parse_statement(file.path, file.relative_path, cfg, client)
        except Exception as exc:
            notes = f"{type(exc).__name__}: {exc}"[:200]
            summary.errors.append(f"{file.relative_path}: {exc}")
            summary.files_errored["bank"] += 1
            pending_ledger_entries.append(
                LedgerEntry(
                    filename=file.filename,
                    relative_path=file.relative_path,
                    sha256=file.sha256,
                    size=file.size,
                    date_processed=run_time,
                    stage="bank",
                    status="Error",
                    rows_added=0,
                    notes=notes,
                )
            )
            logger.exception("bank: %s -> Error", file.relative_path)
            continue

        all_txns.extend(txns)
        logger.info("bank: %s -> %d txns", file.relative_path, len(txns))
        if flagged:
            summary.files_flagged["bank"] += 1
            bank_flagged_any = True
        # rows_added is filled in below, once reconcile() has run.
        pending_ledger_entries.append(
            LedgerEntry(
                filename=file.filename,
                relative_path=file.relative_path,
                sha256=file.sha256,
                size=file.size,
                date_processed=run_time,
                stage="bank",
                status="Flagged" if flagged else "Processed",
                rows_added=0,
                notes="",
            )
        )

    # --- Step 5: reconcile ---
    if all_txns:
        existing_rows = workbook.rows() + receipt_rows
        reconcile_result = reconcile(existing_rows, all_txns, suppliers, categories, cfg, run_time)
    else:
        reconcile_result = ReconcileResult()

    for entry in pending_ledger_entries:
        if entry.stage == "bank" and entry.status != "Error":
            entry.rows_added = sum(
                1 for r in reconcile_result.new_rows if r.source_file == entry.relative_path
            )

    for new_row in reconcile_result.new_rows:
        if new_row.category == "":
            new_supplier_names.add(new_row.company)

    summary.new_suppliers = sorted(new_supplier_names)
    summary.rows_review = sum(1 for r in receipt_rows if r.status == "Review") + sum(
        1 for r in reconcile_result.new_rows if r.status == "Review"
    )
    summary.unmatched_bank_txns = len(reconcile_result.unmatched_txns)
    summary.unmatched_receipts = len(reconcile_result.unmatched_receipts)
    if "receipt" in run_stages:
        summary.rows_added["receipt"] = len(receipt_rows)
    if "bank" in run_stages:
        summary.rows_added["bank"] = len(reconcile_result.new_rows)

    # --- Step 6: block_on_flags ---
    review_present = any(r.status == "Review" for r in receipt_rows) or any(
        r.status == "Review" for r in reconcile_result.new_rows
    )
    blocked = cfg.processing.block_on_flags and (review_present or bank_flagged_any)

    if blocked:
        summary.errors.append("blocked: flags present (processing.block_on_flags=true)")
        summary.finished = datetime.now()
        write_summary(summary, cfg.paths.logs)
        return summary

    # --- Step 7: write (unless dry_run) ---
    if not dry_run:
        try:
            workbook.append(receipt_rows + reconcile_result.new_rows)
            workbook.update(reconcile_result.updated_rows)
            workbook.save()
            suppliers.save()
            for entry in pending_ledger_entries:
                ledger.append(entry)
        except Exception as exc:
            summary.errors.append(f"write failed: {type(exc).__name__}: {exc}")
            summary.finished = datetime.now()
            write_summary(summary, cfg.paths.logs)
            raise

    # --- Step 8: summary ---
    summary.finished = datetime.now()
    write_summary(summary, cfg.paths.logs)
    return summary
