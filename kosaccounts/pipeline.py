"""The `run` command's orchestration (plan §11), kept separate from argument parsing.

run_pipeline(cfg, dry_run=False, stages=None) -> RunSummary:
1. discover + new_files (ledger).  If none: summary.nothing_to_do, write summary, return.
2. Load Categories, payment methods, SupplierBook(client), OutputWorkbook.
3. Expense stage: for each new expense file: extract_expense -> suppliers.match(supplier_name,
   context=line_summary) -> to_purchase_row -> collect. Per-file exceptions are caught: ledger
   Status="Error", summary.errors, continue.
4. Bank stage: parse_statement per file (flagged -> ledger "Flagged"); collect txns.
5. reconcile(workbook.rows() + new expense rows, txns, ...).
6. If cfg.processing.block_on_flags and any Review row/flagged file: write summary, ledger nothing,
   return with summary.errors += ["blocked: flags present"].
7. Unless dry_run: workbook.append(expense rows + reconcile.new_rows); workbook.update(reconcile.updated_rows);
   workbook.save(); suppliers.save(); ledger.append per file (Processed/Flagged, rows_added).
8. write_summary; return summary.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from kosaccounts.bank.registry import parse_statement
from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeClient
from kosaccounts.config import Config
from kosaccounts.discovery import canonical_relative_path, discover, new_files
from kosaccounts.expenses.extract import cfg_payment_methods, extract_expense, to_purchase_row
from kosaccounts.ledger import Ledger
from kosaccounts.models import (
    BankRow,
    LedgerEntry,
    PurchaseRow,
    ReconcileResult,
    RunSummary,
    Stage,
)
from kosaccounts.reconcile import reconcile
from kosaccounts.suppliers import SupplierBook, normalise
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

    expense_rows: list[PurchaseRow] = []
    pending_ledger_entries: list[LedgerEntry] = []
    new_supplier_names: set[str] = set()
    bank_flagged_any = False

    new_expense_files = [f for f in new_list if f.stage == "expense"]
    new_bank_files = [f for f in new_list if f.stage == "bank"]

    # --- Step 2b: re-uploaded (corrected) expenses supersede the row(s) from the earlier upload.
    # `new_files()` already filters by sha256, so any new file whose relative_path is already in the
    # ledger is, by construction, a content change under the same filename (not a duplicate hash).
    # A same-name/different-content re-upload may also arrive under the Dropbox listener's collision
    # name ("stem__<hash8>.ext"); entries_for_canonical_path()/canonical_relative_path() match that
    # up against the original "stem.ext" too. ---
    superseded_by_reupload: list[PurchaseRow] = []
    if new_expense_files:
        run_date_str = run_time.strftime("%Y-%m-%d")
        rows_by_canonical: dict[str, list[PurchaseRow]] = {}
        for existing_row in workbook.rows():
            rows_by_canonical.setdefault(canonical_relative_path(existing_row.source_file), []).append(
                existing_row
            )
        bank_rows_by_ref = {br.ref: br for br in workbook.bank_rows()}
        bank_rows_to_release: dict[int, BankRow] = {}

        for file in new_expense_files:
            if not ledger.entries_for_canonical_path(file.relative_path):
                continue  # never seen this path before: not a re-upload

            file_canonical = canonical_relative_path(file.relative_path)
            for row in rows_by_canonical.get(file_canonical, []):
                if row.status == "Superseded":
                    continue
                row.status = "Superseded"
                note = f"Superseded by re-upload on {run_date_str}"
                row.notes = f"{row.notes}; {note}" if row.notes else note
                if row.bank_ref:
                    bank_row = bank_rows_by_ref.get(row.bank_ref)
                    if bank_row is not None:
                        bank_row.status = "No receipt"
                        bank_row.matched_source = ""
                        bank_rows_to_release[id(bank_row)] = bank_row
                    row.bank_ref = ""
                superseded_by_reupload.append(row)
                logger.info(
                    "expense: %s -> Superseded by re-upload of %s", row.source_file, file.relative_path
                )

        if superseded_by_reupload:
            workbook.update(superseded_by_reupload)
        if bank_rows_to_release:
            workbook.update_bank(list(bank_rows_to_release.values()))

    summary.rows_superseded += len(superseded_by_reupload)

    # --- Step 3: expense stage ---
    for file in new_expense_files:
        try:
            extract = extract_expense(file, client, cfg, payment_methods)
            match = suppliers.match(
                extract.supplier_name or "",
                context=extract.line_summary or "",
                source_file=file.relative_path,
            )
            row = to_purchase_row(extract, match, categories, cfg, run_time)
        except Exception as exc:  # per-file isolation: any exception here must not abort the run
            notes = f"{type(exc).__name__}: {exc}"[:200]
            summary.errors.append(f"{file.relative_path}: {exc}")
            summary.files_errored["expense"] += 1
            pending_ledger_entries.append(
                LedgerEntry(
                    filename=file.filename,
                    relative_path=file.relative_path,
                    sha256=file.sha256,
                    size=file.size,
                    date_processed=run_time,
                    stage="expense",
                    status="Error",
                    rows_added=0,
                    notes=notes,
                )
            )
            logger.exception("expense: %s -> Error", file.relative_path)
            continue

        expense_rows.append(row)
        if match.is_new:
            new_supplier_names.add(row.company)

        file_status = "Flagged" if row.status == "Review" else "Processed"
        if file_status == "Flagged":
            summary.files_flagged["expense"] += 1
        pending_ledger_entries.append(
            LedgerEntry(
                filename=file.filename,
                relative_path=file.relative_path,
                sha256=file.sha256,
                size=file.size,
                date_processed=run_time,
                stage="expense",
                status=file_status,
                rows_added=1,
                notes="",
            )
        )
        logger.info("expense: %s -> %s %s %s", file.relative_path, row.company, row.total, row.status)

    # --- Step 3b: flag duplicate expenses. Dropbox lets the same receipt get uploaded twice under
    # two filenames; catch it by (date, normalised company, total) matching either an earlier row in
    # this same batch or a row already saved in the workbook. ---
    if expense_rows:
        legal_suffixes = cfg.suppliers.legal_suffixes
        ledger_by_relpath = {
            entry.relative_path: entry for entry in pending_ledger_entries if entry.stage == "expense"
        }

        def _dup_key(row: PurchaseRow) -> tuple:
            return (row.date, normalise(row.company, legal_suffixes), row.total.quantize(Decimal("0.01")))

        seen_keys: dict[tuple, str] = {}
        for existing_row in workbook.rows():
            if existing_row.status == "Superseded":
                continue
            seen_keys.setdefault(_dup_key(existing_row), existing_row.source_file)

        for row in expense_rows:
            key = _dup_key(row)
            other_source = seen_keys.get(key)
            if other_source is not None:
                note = f"Possible duplicate of {other_source}"
                row.notes = f"{row.notes}; {note}" if row.notes else note
                row.status = "Review"
                logger.info("expense: %s -> possible duplicate of %s", row.source_file, other_source)
                entry = ledger_by_relpath.get(row.source_file)
                if entry is not None and entry.status != "Flagged":
                    entry.status = "Flagged"
                    summary.files_flagged["expense"] += 1
            seen_keys[key] = row.source_file

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

    # --- Step 5: reconcile. The Bank sheet is the source of truth for bank transactions: every
    # existing row still "No receipt" is resurrected as a txn so a late (or re-uploaded) receipt can
    # claim it, even on an expenses-only run with no new bank file. ---
    existing_bank_rows = workbook.bank_rows()
    old_no_receipt_bank_rows = [br for br in existing_bank_rows if br.status == "No receipt"]
    txns_for_reconcile = all_txns + [br.to_txn() for br in old_no_receipt_bank_rows]

    current_workbook_rows = workbook.rows()
    eligible_workbook_rows = [
        r for r in current_workbook_rows if r.status != "Superseded" and r.bank_ref == ""
    ]
    rows_for_reconcile = eligible_workbook_rows + expense_rows

    if expense_rows or txns_for_reconcile:
        reconcile_result = reconcile(
            rows_for_reconcile,
            txns_for_reconcile,
            suppliers,
            categories,
            cfg,
            run_time,
            all_rows=current_workbook_rows,
        )
    else:
        reconcile_result = ReconcileResult()

    # New BankRow entries for this run's freshly parsed txns only (old resurrected ones already
    # have a Bank-sheet row; that row is updated below instead of duplicated).
    ignored_refs = {t.ref for t in reconcile_result.ignored_txns}
    new_bank_entries: list[BankRow] = []
    for txn in all_txns:
        if txn.ref in reconcile_result.matched:
            bank_status = "Matched"
            matched_source = reconcile_result.matched[txn.ref]
        elif txn.ref in ignored_refs:
            bank_status = "Ignored"
            matched_source = ""
        else:
            bank_status = "No receipt"
            matched_source = ""
        new_bank_entries.append(
            BankRow(
                date=txn.date,
                description=txn.description,
                amount=txn.amount,
                direction=txn.direction,
                status=bank_status,
                matched_source=matched_source,
                ref=txn.ref,
                statement_file=txn.source_file,
                statement_start=txn.statement_start,
                statement_end=txn.statement_end,
                processed_on=run_time,
            )
        )

    bank_rows_to_update: list[BankRow] = [
        br for br in old_no_receipt_bank_rows if br.ref in reconcile_result.matched
    ]
    for br in bank_rows_to_update:
        br.status = "Matched"
        br.matched_source = reconcile_result.matched[br.ref]

    for entry in pending_ledger_entries:
        if entry.stage == "bank" and entry.status != "Error":
            entry.rows_added = sum(
                1 for r in reconcile_result.new_rows if r.source_file == entry.relative_path
            )

    for new_row in reconcile_result.new_rows:
        if new_row.category == "":
            new_supplier_names.add(new_row.company)

    summary.new_suppliers = sorted(new_supplier_names)
    summary.rows_review = sum(1 for r in expense_rows if r.status == "Review") + sum(
        1 for r in reconcile_result.new_rows if r.status == "Review"
    )
    summary.rows_superseded += len(reconcile_result.superseded_rows)
    summary.unmatched_bank_txns = len(reconcile_result.unmatched_txns)
    summary.unmatched_expenses = len(reconcile_result.unmatched_expenses)
    if "expense" in run_stages:
        summary.rows_added["expense"] = len(expense_rows)
    if "bank" in run_stages:
        summary.rows_added["bank"] = len(reconcile_result.new_rows)

    # --- Step 6: block_on_flags ---
    review_present = any(r.status == "Review" for r in expense_rows) or any(
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
            workbook.append(expense_rows + reconcile_result.new_rows)
            workbook.update(reconcile_result.updated_rows + reconcile_result.superseded_rows)
            if new_bank_entries:
                workbook.append_bank(new_bank_entries)
            if bank_rows_to_update:
                workbook.update_bank(bank_rows_to_update)
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
