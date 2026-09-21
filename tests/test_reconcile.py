"""Tests for kosaccounts.reconcile.reconcile()."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kosaccounts.categories import Categories
from kosaccounts.models import BankTxn, PurchaseRow, SupplierMatch
from kosaccounts.reconcile import _norm, reconcile

PROCESSED_ON = datetime(2026, 9, 15, 9, 0, 0)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeSupplierBook:
    """Stands in for suppliers.SupplierBook. `canned` maps a raw name -> SupplierMatch.
    Any name not in `canned` gets a default "new supplier" result."""

    def __init__(self, canned: dict[str, SupplierMatch] | None = None) -> None:
        self.canned = canned or {}
        self.calls: list[tuple[str, str, str]] = []

    def match(self, name: str, context: str = "", source_file: str = "") -> SupplierMatch:
        self.calls.append((name, context, source_file))
        if name in self.canned:
            return self.canned[name]
        return SupplierMatch(
            input_name=name,
            canonical=None,
            default_category=None,
            score=0.0,
            method="new",
            is_new=True,
        )


def _write_categories_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Category", "Schedule C"])
        writer.writerows(rows)


@pytest.fixture
def categories(tmp_path: Path) -> Categories:
    path = tmp_path / "categories.csv"
    _write_categories_csv(
        path,
        [
            ("Fabric", "Cost of Goods Sold"),
            ("Telephone", "Other expenses"),
            ("Software", "Office expense"),
        ],
    )
    return Categories(path)


def make_row(
    *,
    day: int = 1,
    company: str = "Mood Fabrics",
    total: str = "108.88",
    payment_method: str = "",
    notes: str = "",
    status: str = "OK",
    bank_ref: str = "",
    source_file: str = "expenses/2026-09-01 - Mood.pdf",
) -> PurchaseRow:
    return PurchaseRow(
        date=date(2026, 9, day),
        company=company,
        category="Fabric",
        net=Decimal(total),
        sales_tax=Decimal("0.00"),
        total=Decimal(total),
        tax_rate=Decimal("0"),
        payment_method=payment_method,
        notes=notes,
        year=2026,
        source_file=source_file,
        processed_on=PROCESSED_ON,
        status=status,
        bank_ref=bank_ref,
    )


def make_txn(
    *,
    day: int = 1,
    description: str = "MOOD FABRICS NYC",
    amount: str = "108.88",
    direction: str = "debit",
    ref: str = "ref-1",
    source_file: str = "bank/statement_sep.html",
    statement_start: date | None = None,
    statement_end: date | None = None,
) -> BankTxn:
    return BankTxn(
        date=date(2026, 9, day),
        description=description,
        amount=Decimal(amount),
        direction=direction,
        source_file=source_file,
        ref=ref,
        statement_start=statement_start,
        statement_end=statement_end,
    )


# ---------------------------------------------------------------------------
# _norm
# ---------------------------------------------------------------------------


def test_norm_lowercases_and_collapses() -> None:
    assert _norm("Mood Fabrics, NYC!!") == "mood fabrics nyc"
    assert _norm("  T-Mobile  ") == "t mobile"
    assert _norm("") == ""
    assert _norm(None) == ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_matches_within_amount_and_window_sets_bank_ref_and_payment_method(cfg, categories) -> None:
    row = make_row(day=3, total="108.88", payment_method="")
    txn = make_txn(day=5, amount="108.88", ref="txn-1")  # within default 5-day window
    suppliers = FakeSupplierBook()

    result = reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == "txn-1"
    assert row.payment_method == cfg.bank.statement_payment_method
    assert result.updated_rows == [row]
    assert result.new_rows == []
    assert result.unmatched_txns == []
    assert result.unmatched_expenses == []
    assert result.ignored_txns == []


def test_existing_payment_method_is_not_overwritten(cfg, categories) -> None:
    row = make_row(day=3, total="50.00", payment_method="VISA ****1234")
    txn = make_txn(day=4, amount="50.00", ref="txn-2")
    suppliers = FakeSupplierBook()

    reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == "txn-2"
    assert row.payment_method == "VISA ****1234"


def test_outside_window_does_not_match(cfg, categories) -> None:
    row = make_row(day=1, total="75.00")
    txn = make_txn(day=10, amount="75.00", ref="txn-3")  # 9 days > default window (5)
    suppliers = FakeSupplierBook()

    result = reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == ""
    assert result.updated_rows == []
    # Unmatched debit becomes a new Review row.
    assert len(result.new_rows) == 1
    assert result.new_rows[0].bank_ref == "txn-3"


def test_same_amount_two_rows_description_similarity_wins(cfg, categories) -> None:
    close = make_row(day=5, company="Mood Fabrics", total="200.00")
    far = make_row(day=5, company="Zzz Unrelated Co", total="200.00")
    txn = make_txn(day=5, description="MOOD FABRICS NYC", amount="200.00", ref="txn-4")
    suppliers = FakeSupplierBook()

    result = reconcile([close, far], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert close.bank_ref == "txn-4"
    assert far.bank_ref == ""


def test_txn_never_matches_two_rows_and_row_never_gets_two_txns(cfg, categories) -> None:
    row1 = make_row(day=5, company="Mood Fabrics", total="60.00")
    row2 = make_row(day=5, company="Mood Fabrics", total="60.00")
    txn1 = make_txn(day=5, description="MOOD FABRICS", amount="60.00", ref="txn-a")
    txn2 = make_txn(day=5, description="MOOD FABRICS", amount="60.00", ref="txn-b")
    suppliers = FakeSupplierBook()

    result = reconcile([row1, row2], [txn1, txn2], suppliers, categories, cfg, PROCESSED_ON)

    assert {row1.bank_ref, row2.bank_ref} == {"txn-a", "txn-b"}
    assert row1.bank_ref != row2.bank_ref
    assert result.new_rows == []
    assert result.unmatched_txns == []


def test_rows_with_existing_bank_ref_are_never_rematched(cfg, categories) -> None:
    row = make_row(day=5, total="40.00", bank_ref="already-set")
    txn = make_txn(day=5, amount="40.00", ref="txn-5")
    suppliers = FakeSupplierBook()

    result = reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == "already-set"
    # The txn didn't find a receipt row, so it becomes a new Review row instead.
    assert len(result.new_rows) == 1
    assert result.new_rows[0].bank_ref == "txn-5"
    assert result.updated_rows == []


# ---------------------------------------------------------------------------
# Ignore filter
# ---------------------------------------------------------------------------


def test_credits_are_ignored(cfg, categories) -> None:
    txn = make_txn(day=1, description="Refund", amount="20.00", direction="credit", ref="txn-6")
    suppliers = FakeSupplierBook()

    result = reconcile([], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert result.ignored_txns == [txn]
    assert result.new_rows == []
    assert result.unmatched_txns == []


def test_ignore_description_substring_is_case_insensitive(cfg, categories) -> None:
    txn = make_txn(day=1, description="payment - thank you", amount="500.00", ref="txn-7")
    suppliers = FakeSupplierBook()

    result = reconcile([], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert result.ignored_txns == [txn]
    assert result.new_rows == []


# ---------------------------------------------------------------------------
# Unmatched debit -> new Review row
# ---------------------------------------------------------------------------


def test_unmatched_debit_creates_review_row_with_known_supplier_category(cfg, categories) -> None:
    match = SupplierMatch(
        input_name="AMZ MKTP US*1234",
        canonical="Amazon",
        default_category="Fabric",
        score=95.0,
        method="fuzzy",
        is_new=False,
    )
    suppliers = FakeSupplierBook({"AMZ MKTP US*1234": match})
    txn = make_txn(day=7, description="AMZ MKTP US*1234", amount="33.21", ref="txn-8")

    result = reconcile([], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert result.updated_rows == []
    assert result.unmatched_txns == [txn]
    assert len(result.new_rows) == 1
    new_row = result.new_rows[0]

    assert new_row.company == "Amazon"
    assert new_row.category == "Fabric"
    assert new_row.net == Decimal("33.21")
    assert new_row.total == Decimal("33.21")
    assert new_row.sales_tax == Decimal("0")
    assert new_row.tax_rate == Decimal("0")
    assert new_row.payment_method == cfg.bank.statement_payment_method
    assert new_row.notes == "From bank statement; no receipt"
    assert new_row.year == 2026
    assert new_row.source_file == "bank/statement_sep.html"
    assert new_row.processed_on == PROCESSED_ON
    assert new_row.status == "Review"
    assert new_row.bank_ref == "txn-8"

    # suppliers.match was called with the bank description as name and context.
    assert suppliers.calls == [("AMZ MKTP US*1234", "AMZ MKTP US*1234", "bank/statement_sep.html")]


def test_unmatched_debit_new_supplier_gets_empty_category_and_description_as_company(
    cfg, categories
) -> None:
    suppliers = FakeSupplierBook()  # default result: is_new=True, canonical=None
    txn = make_txn(day=7, description="  Weird New Vendor LLC  ", amount="12.00", ref="txn-9")

    result = reconcile([], [txn], suppliers, categories, cfg, PROCESSED_ON)

    new_row = result.new_rows[0]
    assert new_row.company == "Weird New Vendor LLC"
    assert new_row.category == ""
    assert new_row.status == "Review"


# ---------------------------------------------------------------------------
# Statement coverage / "No bank transaction found"
# ---------------------------------------------------------------------------


def test_row_inside_statement_period_with_no_match_gets_note_once_across_two_calls(
    cfg, categories
) -> None:
    row = make_row(day=10, total="99.99")
    # A txn (any amount/desc) carrying the statement period, so coverage exists for Sept.
    covering_txn = make_txn(
        day=15,
        description="SOME OTHER PURCHASE",
        amount="1.23",
        ref="cover-1",
        statement_start=date(2026, 9, 1),
        statement_end=date(2026, 9, 30),
    )
    suppliers = FakeSupplierBook()

    result1 = reconcile([row], [covering_txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == ""  # no amount match, so it's genuinely unmatched
    assert row.notes == "No bank transaction found"
    assert row in result1.unmatched_expenses
    assert row in result1.updated_rows

    # Re-running reconcile on the same row (as the workbook would across a rerun) must not
    # duplicate the note.
    result2 = reconcile([row], [covering_txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.notes == "No bank transaction found"
    assert row.notes.count("No bank transaction found") == 1
    assert row in result2.unmatched_expenses


def test_row_inside_statement_period_with_no_match_appends_to_existing_notes(cfg, categories) -> None:
    row = make_row(day=10, total="99.99", notes="Split payment")
    covering_txn = make_txn(
        day=15,
        amount="1.23",
        ref="cover-2",
        statement_start=date(2026, 9, 1),
        statement_end=date(2026, 9, 30),
    )
    suppliers = FakeSupplierBook()

    reconcile([row], [covering_txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.notes == "Split payment; No bank transaction found"


def test_row_outside_every_statement_period_is_untouched(cfg, categories) -> None:
    row = make_row(day=1, total="99.99", notes="")  # January, but statement covers September
    covering_txn = make_txn(
        day=15,
        amount="1.23",
        ref="cover-3",
        statement_start=date(2026, 9, 1),
        statement_end=date(2026, 9, 30),
    )
    row.date = date(2026, 1, 1)
    suppliers = FakeSupplierBook()

    result = reconcile([row], [covering_txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.notes == ""
    assert row.bank_ref == ""
    assert row not in result.unmatched_expenses
    assert row not in result.updated_rows


def test_new_rows_created_this_call_are_excluded_from_coverage_check(cfg, categories) -> None:
    txn = make_txn(
        day=10,
        description="Brand New Vendor",
        amount="42.00",
        ref="txn-10",
        statement_start=date(2026, 9, 1),
        statement_end=date(2026, 9, 30),
    )
    suppliers = FakeSupplierBook()

    result = reconcile([], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert len(result.new_rows) == 1
    assert result.new_rows[0].notes == "From bank statement; no receipt"
    assert result.unmatched_expenses == []


# ---------------------------------------------------------------------------
# all_rows: resurrected "No receipt" bank rows and superseding bank-only rows
# ---------------------------------------------------------------------------


def test_late_receipt_claims_old_unmatched_debit_and_supersedes_bank_only_row(cfg, categories) -> None:
    """A prior run left a bank-only Review row for an unmatched debit. This run, a late receipt
    arrives for that same purchase; the resurrected txn (BankRow.to_txn()) should match the new
    receipt row and the old bank-only row should be superseded."""
    bank_only_row = make_row(
        day=5,
        company="Weird New Vendor LLC",
        total="12.00",
        notes="From bank statement; no receipt",
        status="Review",
        bank_ref="old-ref-1",
        source_file="bank/statement_aug.html",
    )
    receipt_row = make_row(
        day=5,
        company="Weird New Vendor LLC",
        total="12.00",
        source_file="expenses/2026-09-05 - Weird New Vendor.pdf",
    )
    # Resurrected from the Bank sheet: same ref, description, amount, date as when it first appeared.
    old_txn = make_txn(day=5, description="Weird New Vendor LLC", amount="12.00", ref="old-ref-1")
    suppliers = FakeSupplierBook()

    result = reconcile(
        [receipt_row],
        [old_txn],
        suppliers,
        categories,
        cfg,
        PROCESSED_ON,
        all_rows=[bank_only_row, receipt_row],
    )

    assert receipt_row.bank_ref == "old-ref-1"
    assert result.matched == {"old-ref-1": receipt_row.source_file}
    assert bank_only_row.status == "Superseded"
    assert f"Receipt arrived: {receipt_row.source_file}" in bank_only_row.notes
    assert result.superseded_rows == [bank_only_row]
    # No duplicate bank-only row is created for the (now matched) old debit.
    assert result.new_rows == []


def test_old_unmatched_no_receipt_txn_is_left_alone(cfg, categories) -> None:
    """An old "No receipt" txn that still has no matching receipt this run must not spawn a
    duplicate bank-only row: it already has one in the workbook."""
    bank_only_row = make_row(
        day=5,
        company="Weird New Vendor LLC",
        total="12.00",
        notes="From bank statement; no receipt",
        status="Review",
        bank_ref="old-ref-2",
        source_file="bank/statement_aug.html",
    )
    old_txn = make_txn(day=5, description="Weird New Vendor LLC", amount="12.00", ref="old-ref-2")
    suppliers = FakeSupplierBook()

    result = reconcile(
        [],  # no receipt rows this run to claim it
        [old_txn],
        suppliers,
        categories,
        cfg,
        PROCESSED_ON,
        all_rows=[bank_only_row],
    )

    assert result.new_rows == []
    assert result.unmatched_txns == []
    assert result.superseded_rows == []
    assert bank_only_row.status == "Review"  # untouched


def test_all_rows_defaults_to_rows_when_omitted(cfg, categories) -> None:
    """Backward compatible: omitting all_rows behaves exactly as before (no resurrection tricks)."""
    row = make_row(day=3, total="108.88", payment_method="")
    txn = make_txn(day=5, amount="108.88", ref="txn-1")
    suppliers = FakeSupplierBook()

    result = reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert row.bank_ref == "txn-1"
    assert result.superseded_rows == []


def test_does_not_mutate_input_txns(cfg, categories) -> None:
    txn = make_txn(day=5, amount="15.00", ref="txn-11")
    row = make_row(day=5, total="15.00")
    suppliers = FakeSupplierBook()

    original_ref = txn.ref
    reconcile([row], [txn], suppliers, categories, cfg, PROCESSED_ON)

    assert txn.ref == original_ref  # BankTxn itself untouched
    assert row.bank_ref == "txn-11"


def test_statement_payment_method_comes_from_config(cfg, tmp_path):
    """Bank-only rows and matched receipts with no hint use cfg.bank.statement_payment_method."""
    from datetime import date, datetime
    from decimal import Decimal
    from kosaccounts.models import BankTxn, PurchaseRow, SupplierMatch
    from kosaccounts.reconcile import reconcile
    from kosaccounts.categories import Categories

    cats = tmp_path / "categories.csv"
    cats.write_text("Category,Schedule C\nFabric,Cost of Goods Sold\n")
    categories = Categories(cats)

    class Book:
        def match(self, name, context="", source_file=""):
            return SupplierMatch(name, None, None, 0, "new", is_new=True)

    cfg.bank.statement_payment_method = "Discover"
    now = datetime(2026, 9, 18, 12, 0)
    receipt = PurchaseRow(date(2026, 8, 1), "Mood", "Fabric", Decimal("10"), Decimal("0"), Decimal("10"),
                          Decimal("0"), "", "", 2026, "expenses/a.pdf", now)
    txns = [
        BankTxn(date(2026, 8, 1), "SQ *MOOD", Decimal("10"), "debit", "bank/s.csv", "bank/s.csv#1"),
        BankTxn(date(2026, 8, 2), "UNKNOWN SHOP", Decimal("7"), "debit", "bank/s.csv", "bank/s.csv#2"),
    ]
    result = reconcile([receipt], txns, Book(), categories, cfg, now)
    assert receipt.payment_method == "Discover"
    assert result.new_rows[0].payment_method == "Discover"
