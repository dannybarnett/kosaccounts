"""Reconcile receipt rows against bank transactions (plan §8).

Inputs: ALL existing PurchaseRows from the output workbook (old + this run's receipt rows, with
sheet_row set for old ones) and the BankTxns parsed this run.

Rules:
- Ignore credits and any txn whose description contains (case-insensitive) an entry of
  cfg.bank.ignore_descriptions -> ReconcileResult.ignored_txns.
- A txn matches a row when amount == row.total (exact, 2 dp) and |txn.date - row.date| <= match_window_days
  and the row has no bank_ref yet. Ties broken by rapidfuzz partial_ratio(normalise(description),
  normalise(company)); each txn matches at most one row and vice versa.
- Matched: row.bank_ref = txn.ref; row.payment_method filled from statement if empty -> updated_rows.
- Unmatched debit: new PurchaseRow via suppliers.match(description, context=description, source_file),
  category = match.default_category or "" (Review), total = amount, sales_tax = 0, net = amount,
  tax_rate = 0, notes = "From bank statement; no receipt", status "Review" -> new_rows + unmatched_txns.
- Row (no bank_ref) whose date lies within any parsed statement's [start, end] but got no match:
  append "No bank transaction found" to notes (once) -> updated_rows + unmatched_receipts.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from rapidfuzz import fuzz

from kosaccounts.categories import Categories
from kosaccounts.config import Config
from kosaccounts.models import BankTxn, PurchaseRow, ReconcileResult
from kosaccounts.suppliers import SupplierBook

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_NO_BANK_TXN_NOTE = "No bank transaction found"
_NO_RECEIPT_NOTE = "From bank statement; no receipt"
_STATEMENT_PAYMENT_METHOD = "Card (statement)"
_TWO_DP = Decimal("0.01")


def _norm(text: str) -> str:
    """Lowercase; non-alphanumerics -> space; collapse whitespace. Used only for the
    similarity tie-break (not supplier normalisation)."""
    if not text:
        return ""
    lowered = text.lower()
    collapsed = _NON_ALNUM_RE.sub(" ", lowered)
    return " ".join(collapsed.split())


def reconcile(
    rows: list[PurchaseRow],
    txns: list[BankTxn],
    suppliers: SupplierBook,
    categories: Categories,
    cfg: Config,
    processed_on: datetime,
) -> ReconcileResult:
    result = ReconcileResult()

    ignore_terms = [s.lower() for s in cfg.bank.ignore_descriptions]

    active_txns: list[BankTxn] = []
    for txn in txns:
        desc_lower = txn.description.lower()
        if txn.direction == "credit" or any(term in desc_lower for term in ignore_terms):
            result.ignored_txns.append(txn)
        else:
            active_txns.append(txn)

    # Candidate rows: no bank_ref yet (snapshot taken before any mutation below).
    unref_rows = [row for row in rows if row.bank_ref == ""]

    window = cfg.bank.match_window_days

    pairs: list[tuple[float, int, BankTxn, PurchaseRow]] = []
    for txn in active_txns:
        txn_amount = txn.amount.quantize(_TWO_DP)
        norm_desc = _norm(txn.description)
        for row in unref_rows:
            if row.total.quantize(_TWO_DP) != txn_amount:
                continue
            date_dist = abs((txn.date - row.date).days)
            if date_dist > window:
                continue
            score = fuzz.partial_ratio(norm_desc, _norm(row.company))
            pairs.append((score, date_dist, txn, row))

    # Greedy global assignment: best score first, ties broken by smaller date distance.
    pairs.sort(key=lambda p: (-p[0], p[1]))

    txn_to_row: dict[int, PurchaseRow] = {}
    matched_txn_ids: set[int] = set()
    matched_row_ids: set[int] = set()
    for _score, _date_dist, txn, row in pairs:
        if id(txn) in matched_txn_ids or id(row) in matched_row_ids:
            continue
        matched_txn_ids.add(id(txn))
        matched_row_ids.add(id(row))
        txn_to_row[id(txn)] = row

    updated_rows: list[PurchaseRow] = []
    updated_row_ids: set[int] = set()

    def add_updated(row: PurchaseRow) -> None:
        if id(row) not in updated_row_ids:
            updated_row_ids.add(id(row))
            updated_rows.append(row)

    for txn in active_txns:
        row = txn_to_row.get(id(txn))
        if row is None:
            continue
        row.bank_ref = txn.ref
        if row.payment_method == "":
            row.payment_method = _STATEMENT_PAYMENT_METHOD
        add_updated(row)

    new_rows: list[PurchaseRow] = []
    unmatched_txns: list[BankTxn] = []

    for txn in active_txns:
        if id(txn) in matched_txn_ids:
            continue

        match = suppliers.match(txn.description, context=txn.description, source_file=txn.source_file)
        company = match.canonical if not match.is_new else txn.description.strip()

        raw_category = match.default_category
        if raw_category and categories.is_valid(raw_category):
            category = categories.canonical(raw_category) or raw_category
            schedule_c = categories.schedule_c_for(category) or ""
        else:
            category = ""
            schedule_c = ""

        amount = txn.amount.quantize(_TWO_DP)
        new_row = PurchaseRow(
            date=txn.date,
            company=company,
            category=category,
            schedule_c=schedule_c,
            net=amount,
            sales_tax=Decimal("0.00"),
            total=amount,
            tax_rate=Decimal("0"),
            payment_method=_STATEMENT_PAYMENT_METHOD,
            notes=_NO_RECEIPT_NOTE,
            year=txn.date.year,
            source_file=txn.source_file,
            processed_on=processed_on,
            status="Review",
            bank_ref=txn.ref,
        )
        new_rows.append(new_row)
        unmatched_txns.append(txn)

    result.new_rows = new_rows
    result.unmatched_txns = unmatched_txns

    # Statement coverage: periods from any txn (matched, unmatched, ignored) that carries one.
    periods: list[tuple] = []
    for txn in txns:
        if txn.statement_start is not None and txn.statement_end is not None:
            periods.append((txn.statement_start, txn.statement_end))

    unmatched_receipts: list[PurchaseRow] = []
    for row in rows:
        if row.bank_ref != "":
            # Either had a bank_ref already, or was just matched above.
            continue
        if not any(start <= row.date <= end for start, end in periods):
            continue
        if _NO_BANK_TXN_NOTE not in row.notes:
            row.notes = f"{row.notes}; {_NO_BANK_TXN_NOTE}" if row.notes else _NO_BANK_TXN_NOTE
        add_updated(row)
        unmatched_receipts.append(row)

    result.updated_rows = updated_rows
    result.unmatched_receipts = unmatched_receipts

    return result
