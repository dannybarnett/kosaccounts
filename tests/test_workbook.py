"""Tests for workbook.py (OutputWorkbook)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from kosaccounts.models import PurchaseRow
from kosaccounts.workbook import OutputWorkbook


def make_row(
    *,
    day: int = 1,
    company: str = "Mood Fabrics",
    net: str = "100.00",
    sales_tax: str = "8.88",
    total: str = "108.88",
    status: str = "OK",
    notes: str = "",
    bank_ref: str = "",
) -> PurchaseRow:
    return PurchaseRow(
        date=date(2026, 9, day),
        company=company,
        category="Fabric",
        schedule_c="Supplies",
        net=Decimal(net),
        sales_tax=Decimal(sales_tax),
        total=Decimal(total),
        tax_rate=Decimal("8.875"),
        payment_method="VISA ****1234",
        notes=notes,
        year=2026,
        source_file=f"receipts/2026-09-0{day} - Mood.pdf",
        processed_on=datetime(2026, 9, day, 10, 30, 0),
        status=status,
        bank_ref=bank_ref,
    )


class TestOutputWorkbook:
    def test_create_append_save_reopen_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)

        row1 = make_row(day=1, company="Mood Fabrics")
        row2 = make_row(day=2, company="B&H Photo")
        wb.append([row1, row2])
        assert row1.sheet_row == 4
        assert row2.sheet_row == 5
        wb.save()

        assert path.exists()
        assert not path.with_suffix(".xlsx.tmp").exists()

        wb2 = OutputWorkbook(path)
        rows = wb2.rows()
        assert len(rows) == 2

        for original, reread in zip([row1, row2], rows):
            assert reread.date == original.date
            assert reread.company == original.company
            assert reread.category == original.category
            assert reread.schedule_c == original.schedule_c
            assert reread.net == round(Decimal(original.net), 2)
            assert reread.sales_tax == round(Decimal(original.sales_tax), 2)
            assert reread.total == round(Decimal(original.total), 2)
            assert reread.payment_method == original.payment_method
            assert reread.notes == original.notes
            assert reread.year == original.year
            assert reread.source_file == original.source_file
            assert reread.processed_on == original.processed_on
            assert reread.status == original.status
            assert reread.bank_ref == original.bank_ref
            assert reread.sheet_row is not None

    def test_append_after_reopen_and_totals_row(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)
        wb.append([make_row(day=1), make_row(day=2)])
        wb.save()

        wb2 = OutputWorkbook(path)
        wb2.append([make_row(day=3, company="Amazon.com")])
        wb2.save()

        rows = wb2.rows()
        assert len(rows) == 3
        assert [r.sheet_row for r in rows] == [4, 5, 6]

        raw = openpyxl.load_workbook(path, data_only=False)
        ws = raw[PurchaseRow.SHEET_NAME]

        # totals row sits directly below the last data row
        assert ws.cell(row=7, column=2).value == "Total"
        assert ws.cell(row=7, column=6).value == "=SUBTOTAL(109,F4:F6)"
        assert ws.cell(row=7, column=7).value == "=SUBTOTAL(109,G4:G6)"
        assert ws.cell(row=7, column=8).value == "=SUBTOTAL(109,H4:H6)"

        table = ws.tables[PurchaseRow.TABLE_NAME]
        assert table.ref == "B3:Q6"

        # Check formula text present in K4
        assert ws.cell(row=4, column=11).value == '=IF(ROUND(F4+G4,2)=ROUND(H4,2),"ok","!!!!")'

    def test_update_changes_fields_and_review_fill(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)
        row1 = make_row(day=1)
        row2 = make_row(day=2)
        wb.append([row1, row2])
        wb.save()

        row1.bank_ref = "TXN-123"
        row1.notes = "Matched to bank statement"
        row2.status = "Review"
        row2.notes = "No bank transaction found"
        wb.update([row1, row2])
        wb.save()

        wb2 = OutputWorkbook(path)
        rows = wb2.rows()
        assert rows[0].bank_ref == "TXN-123"
        assert rows[0].notes == "Matched to bank statement"
        assert rows[1].status == "Review"
        assert rows[1].notes == "No bank transaction found"

        raw = openpyxl.load_workbook(path, data_only=False)
        ws = raw[PurchaseRow.SHEET_NAME]
        review_fill = ws.cell(row=5, column=2).fill
        assert review_fill.fgColor.rgb in ("00FFF2CC", "FFFFF2CC", "FFF2CC")
        ok_fill = ws.cell(row=4, column=2).fill
        assert ok_fill.fill_type is None

    def test_update_without_sheet_row_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)
        row = make_row()
        with pytest.raises(ValueError):
            wb.update([row])

    def test_save_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)
        wb.append([make_row()])
        wb.save()
        tmp = path.with_suffix(".xlsx.tmp")
        assert not tmp.exists()

    def test_zero_row_workbook_saves_and_reopens_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "kosibah_import.xlsx"
        wb = OutputWorkbook(path)
        wb.save()

        assert path.exists()
        assert not path.with_suffix(".xlsx.tmp").exists()

        wb2 = OutputWorkbook(path)
        assert wb2.rows() == []


def test_save_creates_missing_output_directory(tmp_path):
    """A fresh install has no output/ folder yet; save() must create it."""
    from kosaccounts.workbook import OutputWorkbook

    target = tmp_path / "output" / "nested" / "kosibah_import.xlsx"
    wb = OutputWorkbook(target)
    wb.save()
    assert target.exists()
    assert OutputWorkbook(target).rows() == []
