"""Reconcile expense rows against bank transactions (plan §8).

Inputs: `rows` is all eligible PurchaseRows for matching -- existing workbook rows with
status != "Superseded" and bank_ref == "", plus this run's expense rows. `txns` is this run's
newly parsed BankTxns plus BankRow.to_txn() for every existing Bank-sheet row still "No receipt"
(so a late-arriving receipt can claim an old unmatched debit). `all_rows` is the full,
unfiltered list of workbook purchase rows (defaults to `rows` when omitted), used only to find a
pre-existing bank-only row (created in an earlier run for a txn with no receipt) so it can be
superseded once a receipt claims that txn.

Rules:
- A txn is "new" this run when its ref does not already belong to a bank-only row in `all_rows`
  (bank_ref set, notes containing "From bank statement; no receipt", not already Superseded);
  otherwise it is "old" (resurrected from the Bank sheet). Ignore filter (credits and
  cfg.bank.ignore_descriptions) applies only to new txns -- old txns already carry their status
  and always take part in matching -> ignored_txns.
- A txn matches a row when amount == row.total (exact, 2 dp) and |txn.date - row.date| <= match_window_days
  and the row has no bank_ref yet. Ties broken by rapidfuzz partial_ratio(normalise(description),
  normalise(company)); each txn matches at most one row and vice versa.
- Matched: row.bank_ref = txn.ref; row.payment_method filled from statement if empty -> updated_rows;
  ReconcileResult.matched[txn.ref] = row.source_file. If the txn was "old" (already had a bank-only
  row), that bank-only row's status becomes "Superseded" and its notes gain
  "Receipt arrived: <row.source_file>" -> superseded_rows.
- Unmatched active NEW debit: new PurchaseRow via suppliers.match(description, context=description,
  source_file), category = match.default_category or "" (Review), total = amount, sales_tax = 0,
  net = amount, tax_rate = 0, notes = "From bank statement; no receipt", status "Review" ->
  new_rows + unmatched_txns. An unmatched OLD debit is left alone (it already has a bank-only row).
- Row (no bank_ref) whose date lies within any statement period carried by a NEW txn but got no
  match: append "No bank transaction found" to notes (once) -> updated_rows + unmatched_expenses.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from kosaccounts.categories import Categories
from kosaccounts.config import Config
from kosaccounts.models import BankTxn, PurchaseRow, ReconcileResult
from kosaccounts.suppliers import SupplierBook

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_NO_BANK_TXN_NOTE = "No bank transaction found"
_NO_RECEIPT_NOTE = "From bank statement; no receipt"
_SUPERSEDED_NOTE_PREFIX = "Receipt arrived: "
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
    *,
    all_rows: Optional[list[PurchaseRow]] = None,
) -> ReconcileResult:
    result = ReconcileResult()

    if all_rows is None:
        all_rows = rows

    # Pre-existing bank-only rows (created in an earlier run for a debit with no receipt), keyed
    # by the bank ref they're waiting on. A txn whose ref appears here is "old" this run.
    bank_only_by_ref: dict[str, PurchaseRow] = {}
    for r in all_rows:
        if r.bank_ref and r.status != "Superseded" and _NO_RECEIPT_NOTE in r.notes:
            bank_only_by_ref[r.bank_ref] = r

    ignore_terms = [s.lower() for s in cfg.bank.ignore_descriptions]

    active_txns: list[BankTxn] = []
    for txn in txns:
        is_new = txn.ref not in bank_only_by_ref
        if is_new:
            desc_lower = txn.description.lower()
            if txn.direction == "credit" or any(term in desc_lower for term in ignore_terms):
                result.ignored_txns.append(txn)
                continue
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

    superseded_rows: list[PurchaseRow] = []
    statement_pm = cfg.bank.statement_payment_method or _STATEMENT_PAYMENT_METHOD

    for txn in active_txns:
        row = txn_to_row.get(id(txn))
        if row is None:
            continue
        row.bank_ref = txn.ref
        if row.payment_method == "":
            row.payment_method = statement_pm
        add_updated(row)
        result.matched[txn.ref] = row.source_file

        bank_only_row = bank_only_by_ref.get(txn.ref)
        if bank_only_row is not None:
            bank_only_row.status = "Superseded"
            note = f"{_SUPERSEDED_NOTE_PREFIX}{row.source_file}"
            bank_only_row.notes = f"{bank_only_row.notes}; {note}" if bank_only_row.notes else note
            superseded_rows.append(bank_only_row)

    new_rows: list[PurchaseRow] = []
    unmatched_txns: list[BankTxn] = []

    for txn in active_txns:
        if id(txn) in matched_txn_ids:
            continue
        if txn.ref in bank_only_by_ref:
            # Old debit, still unmatched: it already has a bank-only row; leave it alone.
            continue

        match = suppliers.match(txn.description, context=txn.description, source_file=txn.source_file)
        company = match.canonical if not match.is_new else txn.description.strip()

        raw_category = match.default_category
        if raw_category and categories.is_valid(raw_category):
            category = categories.canonical(raw_category) or raw_category
        else:
            category = ""

        amount = txn.amount.quantize(_TWO_DP)
        new_row = PurchaseRow(
            date=txn.date,
            company=company,
            category=category,
            net=amount,
            sales_tax=Decimal("0.00"),
            total=amount,
            tax_rate=Decimal("0"),
            payment_method=statement_pm,
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
    result.superseded_rows = superseded_rows

    # Statement coverage: periods carried by NEW txns only (matched, unmatched, ignored). Old
    # (resurrected) txns' periods were already used to annotate rows in an earlier run.
    periods: list[tuple] = []
    for txn in txns:
        if txn.ref in bank_only_by_ref:
            continue
        if txn.statement_start is not None and txn.statement_end is not None:
            periods.append((txn.statement_start, txn.statement_end))

    unmatched_expenses: list[PurchaseRow] = []
    for row in rows:
        if row.bank_ref != "":
            # Either had a bank_ref already, or was just matched above.
            continue
        if not any(start <= row.date <= end for start, end in periods):
            continue
        if _NO_BANK_TXN_NOTE not in row.notes:
            row.notes = f"{row.notes}; {_NO_BANK_TXN_NOTE}" if row.notes else _NO_BANK_TXN_NOTE
        add_updated(row)
        unmatched_expenses.append(row)

    result.updated_rows = updated_rows
    result.unmatched_expenses = unmatched_expenses

    return result
