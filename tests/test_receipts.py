"""Tests for kosaccounts.receipts.extract and kosaccounts.receipts.prompts."""

from __future__ import annotations

import csv
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeError
from kosaccounts.config import Config
from kosaccounts.models import DiscoveredFile, ReceiptExtract, SupplierMatch
from kosaccounts.receipts import extract as extract_mod
from kosaccounts.receipts.extract import (
    extract_receipt,
    parse_filename_hint,
    to_purchase_row,
    validate,
)
from kosaccounts.receipts.prompts import RECEIPT_JSON_KEYS, receipt_prompt

PAYMENT_METHODS = [
    "Mastercard",
    "Visa",
    "Discover",
    "Paypal",
    "CashApp",
    "Cash",
    "Cheque",
    "Check",
    "Bank transfer",
    "Wire transfer",
]


class FakeClient:
    """Stands in for ClaudeClient: returns a canned dict, or raises ClaudeError."""

    def __init__(self, response: Optional[dict] = None, error: Optional[str] = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, list]] = []

    def run_json(self, prompt: str, files=()) -> dict:
        self.calls.append((prompt, list(files)))
        if self.error is not None:
            raise ClaudeError(self.error)
        return self.response


def _write_categories_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Category", "Schedule C"])
        writer.writerows(rows)


def _write_payment_methods_csv(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Payment method", "Count"])
        for name in names:
            writer.writerow([name, 1])


@pytest.fixture
def categories(tmp_repo: Path) -> Categories:
    path = tmp_repo / "data" / "categories.csv"
    _write_categories_csv(
        path,
        [
            ("Fabric", "Cost of Goods Sold"),
            ("Office supplies", "Office expense"),
        ],
    )
    return Categories(path)


@pytest.fixture(autouse=True)
def _payment_methods_csv(tmp_repo: Path) -> None:
    _write_payment_methods_csv(tmp_repo / "data" / "payment_methods.csv", PAYMENT_METHODS)


def make_discovered_file(path: Path, relative_path: Optional[str] = None) -> DiscoveredFile:
    return DiscoveredFile(
        path=path,
        relative_path=relative_path or f"receipts/{path.name}",
        stage="receipt",
        sha256="deadbeef",
        size=path.stat().st_size if path.exists() else 0,
    )


def make_match(
    *,
    canonical: Optional[str] = "Mood Fabrics",
    default_category: Optional[str] = "Fabric",
    is_new: bool = False,
    method: str = "exact",
) -> SupplierMatch:
    return SupplierMatch(
        input_name="Mood Fabrics",
        canonical=canonical,
        default_category=default_category,
        score=100.0,
        method=method,  # type: ignore[arg-type]
        is_new=is_new,
    )


def make_extract(**overrides) -> ReceiptExtract:
    defaults = dict(
        source_file="receipts/2026-09-01 - Mood.pdf",
        date=date(2026, 9, 1),
        supplier_name="Mood Fabrics",
        total=Decimal("108.88"),
        sales_tax=Decimal("8.88"),
        net=Decimal("100.00"),
        tax_rate=Decimal("8.875"),
        currency="USD",
        payment_method_hint="VISA ****1234",
        line_summary="Fabric purchase",
        confidence=0.95,
        notes=[],
    )
    defaults.update(overrides)
    return ReceiptExtract(**defaults)


# ---------------------------------------------------------------------------
# parse_filename_hint
# ---------------------------------------------------------------------------


class TestParseFilenameHint:
    def test_date_dash_supplier(self) -> None:
        assert parse_filename_hint("2026-09-01 - Mood.pdf") == (date(2026, 9, 1), "Mood")

    def test_date_space_supplier(self) -> None:
        assert parse_filename_hint("2026-09-01 Mood Fabrics.jpg") == (date(2026, 9, 1), "Mood Fabrics")

    def test_no_date_no_hint(self) -> None:
        assert parse_filename_hint("IMG_4471.jpg") == (None, None)

    def test_compact_yyyymmdd(self) -> None:
        assert parse_filename_hint("20260901 Mood.jpg") == (date(2026, 9, 1), "Mood")

    def test_underscore_separator(self) -> None:
        assert parse_filename_hint("2026-09-01_Mood.jpg") == (date(2026, 9, 1), "Mood")

    def test_invalid_date_returns_supplier_only(self) -> None:
        result = parse_filename_hint("2026-13-40 - Something.jpg")
        assert result == (None, "Something")

    def test_invalid_date_no_supplier(self) -> None:
        result = parse_filename_hint("2026-13-40.jpg")
        assert result == (None, None)

    def test_strips_trailing_receipt_noise(self) -> None:
        assert parse_filename_hint("2026-09-01 - Mood receipt.pdf") == (date(2026, 9, 1), "Mood")

    def test_strips_trailing_parenthetical_noise(self) -> None:
        assert parse_filename_hint("2026-09-01 - Mood (1).pdf") == (date(2026, 9, 1), "Mood")

    def test_date_only_no_supplier_text(self) -> None:
        assert parse_filename_hint("2026-09-01.pdf") == (date(2026, 9, 1), None)


# ---------------------------------------------------------------------------
# receipt_prompt
# ---------------------------------------------------------------------------


class TestReceiptPrompt:
    def test_contains_path_hints_and_methods(self, tmp_path: Path) -> None:
        p = tmp_path / "receipt.jpg"
        prompt = receipt_prompt(p, date(2026, 9, 1), "Mood Fabrics", None, PAYMENT_METHODS)
        assert str(p) in prompt
        assert "2026-09-01" in prompt
        assert "Mood Fabrics" in prompt
        for method in PAYMENT_METHODS:
            assert method in prompt
        for key in RECEIPT_JSON_KEYS:
            assert key in prompt
        assert "```" not in prompt

    def test_includes_truncated_pdf_text(self, tmp_path: Path) -> None:
        p = tmp_path / "receipt.pdf"
        long_text = "A" * 7000
        prompt = receipt_prompt(p, None, None, long_text, PAYMENT_METHODS)
        assert "A" * 6000 in prompt
        assert "A" * 6001 not in prompt

    def test_no_pdf_text_block_when_none(self, tmp_path: Path) -> None:
        p = tmp_path / "receipt.jpg"
        prompt = receipt_prompt(p, None, None, None, PAYMENT_METHODS)
        assert "BEGIN PDF TEXT" not in prompt

    def test_under_length_budget(self, tmp_path: Path) -> None:
        p = tmp_path / "receipt.jpg"
        prompt = receipt_prompt(p, date(2026, 9, 1), "Mood Fabrics", None, PAYMENT_METHODS)
        assert len(prompt) < 2500


# ---------------------------------------------------------------------------
# extract_receipt: image conversion
# ---------------------------------------------------------------------------


class TestImageConversion:
    def test_converts_image_and_caches(self, tmp_repo: Path, cfg: Config) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        img_path = receipts_dir / "2026-09-01 - Mood.png"
        img = Image.new("RGB", (4000, 3000), color=(200, 100, 50))
        img.save(img_path)

        file = make_discovered_file(img_path)
        client = FakeClient(
            response={
                "date": "2026-09-01",
                "supplier_name": "Mood Fabrics",
                "total": 100.0,
                "sales_tax": 0,
                "net": 100.0,
                "tax_rate": 0,
                "currency": "USD",
                "payment_method_hint": "Cash",
                "line_summary": "Fabric",
                "confidence": 0.9,
                "notes": [],
            }
        )

        extract_receipt(file, client, cfg, PAYMENT_METHODS)

        converted = receipts_dir / ".converted" / "2026-09-01 - Mood.jpg"
        assert converted.exists()
        with Image.open(converted) as converted_img:
            assert max(converted_img.size) <= cfg.receipts.max_image_px

        # Model was handed the converted path, not the original.
        prompt, files = client.calls[0]
        assert files == [converted]

        mtime_before = converted.stat().st_mtime

        # Second call reuses the cached conversion (mtime unchanged).
        time.sleep(0.01)
        client2 = FakeClient(response=client.response)
        extract_receipt(file, client2, cfg, PAYMENT_METHODS)
        assert converted.stat().st_mtime == mtime_before


# ---------------------------------------------------------------------------
# extract_receipt: PDF text branch (via monkeypatching the module's pdf-text helper)
# ---------------------------------------------------------------------------


class TestPdfBranch:
    def test_pdf_text_is_passed_to_prompt(self, tmp_repo: Path, cfg: Config, monkeypatch) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = receipts_dir / "2026-09-01 - Mood.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        monkeypatch.setattr(extract_mod, "_pdf_text", lambda path: "Mood Fabrics\nTotal: $100.00")

        file = make_discovered_file(pdf_path)
        client = FakeClient(
            response={
                "date": "2026-09-01",
                "supplier_name": "Mood Fabrics",
                "total": "100.00",
                "sales_tax": "0",
                "net": "100.00",
                "tax_rate": "0",
                "currency": "USD",
                "payment_method_hint": None,
                "line_summary": "Fabric",
                "confidence": 0.9,
                "notes": [],
            }
        )

        result = extract_receipt(file, client, cfg, PAYMENT_METHODS)

        prompt, files = client.calls[0]
        assert "Mood Fabrics" in prompt
        assert "Total: $100.00" in prompt
        assert files == [pdf_path]
        assert result.total == Decimal("100.00")

    def test_pdf_text_extraction_failure_yields_none(self, tmp_repo: Path, cfg: Config, monkeypatch) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = receipts_dir / "2026-09-01 - Mood.pdf"
        pdf_path.write_bytes(b"not really a pdf")

        # Real pdfplumber will fail to parse this garbage; extraction should not raise.
        file = make_discovered_file(pdf_path)
        client = FakeClient(
            response={
                "date": "2026-09-01",
                "supplier_name": "Mood Fabrics",
                "total": "50.00",
                "sales_tax": "0",
                "net": "50.00",
                "tax_rate": "0",
                "currency": "USD",
                "payment_method_hint": None,
                "line_summary": "Fabric",
                "confidence": 0.9,
                "notes": [],
            }
        )
        result = extract_receipt(file, client, cfg, PAYMENT_METHODS)
        assert result.total == Decimal("50.00")


# ---------------------------------------------------------------------------
# extract_receipt: JSON mapping, unsupported types, ClaudeError
# ---------------------------------------------------------------------------


class TestExtractReceiptMapping:
    def test_maps_string_amounts_with_dollar_and_commas(self, tmp_repo: Path, cfg: Config, monkeypatch) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = receipts_dir / "2026-09-01 - Mood.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(extract_mod, "_pdf_text", lambda path: "")

        file = make_discovered_file(pdf_path)
        client = FakeClient(
            response={
                "date": "2026-09-01",
                "supplier_name": "Mood Fabrics",
                "total": "$1,234.56",
                "sales_tax": "$100.00",
                "net": "$1,134.56",
                "tax_rate": "8.875",
                "currency": "USD",
                "payment_method_hint": "VISA ****1234",
                "line_summary": "Fabric purchase",
                "confidence": 0.9,
                "notes": ["multiple items"],
            }
        )

        result = extract_receipt(file, client, cfg, PAYMENT_METHODS)

        assert result.total == Decimal("1234.56")
        assert result.sales_tax == Decimal("100.00")
        assert result.net == Decimal("1134.56")
        assert result.date == date(2026, 9, 1)
        assert "multiple items" in result.notes

    def test_unsupported_file_type(self, tmp_repo: Path, cfg: Config) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        odd_path = receipts_dir / "2026-09-01 - Mood.txt"
        odd_path.write_text("not a receipt")

        file = make_discovered_file(odd_path)
        client = FakeClient(response={})

        result = extract_receipt(file, client, cfg, PAYMENT_METHODS)

        assert result.confidence == 0
        assert "unsupported file type" in result.notes
        assert client.calls == []

    def test_claude_error_yields_zero_confidence_note(self, tmp_repo: Path, cfg: Config, monkeypatch) -> None:
        receipts_dir = tmp_repo / "imports" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = receipts_dir / "2026-09-01 - Mood.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(extract_mod, "_pdf_text", lambda path: "")

        file = make_discovered_file(pdf_path)
        client = FakeClient(error="boom")

        result = extract_receipt(file, client, cfg, PAYMENT_METHODS)

        assert result.confidence == 0
        assert any("model extraction failed" in n and "boom" in n for n in result.notes)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_derives_missing_total(self, cfg: Config) -> None:
        extract = make_extract(total=None)
        result = validate(extract, cfg)
        assert result.total == Decimal("108.88")

    def test_derives_missing_net(self, cfg: Config) -> None:
        extract = make_extract(net=None)
        result = validate(extract, cfg)
        assert result.net == Decimal("100.00")

    def test_derives_missing_sales_tax(self, cfg: Config) -> None:
        extract = make_extract(sales_tax=None)
        result = validate(extract, cfg)
        assert result.sales_tax == Decimal("8.88")

    def test_flags_non_reconciling_amounts(self, cfg: Config) -> None:
        extract = make_extract(net=Decimal("100.00"), sales_tax=Decimal("8.88"), total=Decimal("500.00"))
        result = validate(extract, cfg)
        assert any("do not reconcile" in n for n in result.notes)

    def test_derives_tax_rate(self, cfg: Config) -> None:
        extract = make_extract(tax_rate=None, net=Decimal("100.00"), sales_tax=Decimal("8.875"))
        result = validate(extract, cfg)
        assert result.tax_rate == Decimal("8.875")

    def test_derives_zero_tax_rate_when_no_tax(self, cfg: Config) -> None:
        extract = make_extract(tax_rate=None, net=Decimal("100.00"), sales_tax=Decimal("0"))
        result = validate(extract, cfg)
        assert result.tax_rate == Decimal("0")

    def test_notes_non_usd_currency(self, cfg: Config) -> None:
        extract = make_extract(currency="GBP")
        result = validate(extract, cfg)
        assert any("GBP" in n for n in result.notes)

    def test_date_missing_noted_when_no_hint(self, cfg: Config) -> None:
        extract = make_extract(date=None)
        result = validate(extract, cfg)
        assert "date missing" in result.notes

    def test_date_filled_from_hint_no_note(self, cfg: Config) -> None:
        extract = make_extract(date=None)
        extract.raw["_hints"] = {"date": date(2026, 9, 1), "supplier": None}
        result = validate(extract, cfg)
        assert result.date == date(2026, 9, 1)
        assert "date missing" not in result.notes

    def test_supplier_missing_noted_when_no_hint(self, cfg: Config) -> None:
        extract = make_extract(supplier_name=None)
        result = validate(extract, cfg)
        assert "supplier missing" in result.notes

    def test_supplier_filled_from_hint_no_note(self, cfg: Config) -> None:
        extract = make_extract(supplier_name=None)
        extract.raw["_hints"] = {"date": None, "supplier": "Mood Fabrics"}
        result = validate(extract, cfg)
        assert result.supplier_name == "Mood Fabrics"
        assert "supplier missing" not in result.notes


# ---------------------------------------------------------------------------
# to_purchase_row
# ---------------------------------------------------------------------------


class TestToPurchaseRow:
    def test_ok_path(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract()
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2, 9, 0, 0))
        assert row.status == "OK"
        assert row.company == "Mood Fabrics"
        assert row.category == "Fabric"
        assert row.schedule_c == "Cost of Goods Sold"
        assert row.net == Decimal("100.00")
        assert row.sales_tax == Decimal("8.88")
        assert row.total == Decimal("108.88")
        assert row.payment_method == "Visa"
        assert row.year == 2026
        assert row.date == date(2026, 9, 1)

    def test_review_when_new_supplier(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract()
        match = make_match(is_new=True, canonical=None)
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.status == "Review"
        assert row.company == "Mood Fabrics"  # falls back to extract.supplier_name

    def test_review_when_low_confidence(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(confidence=0.1)
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.status == "Review"

    def test_review_when_money_missing(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(net=None, sales_tax=None, total=None)
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.status == "Review"
        assert row.net == Decimal("0")
        assert row.sales_tax == Decimal("0")
        assert row.total == Decimal("0")
        assert "amount missing" in row.notes

    def test_review_when_invalid_category(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract()
        match = make_match(default_category="Not A Real Category")
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.status == "Review"
        assert row.category == ""
        assert row.schedule_c == ""

    def test_review_when_notes_present(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(notes=["currency GBP: convert to USD and note the rate"])
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.status == "Review"

    def test_payment_method_mapping_mastercard(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(payment_method_hint="MasterCard ending 4402")
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.payment_method == "Mastercard"

    def test_payment_method_unrecognised_kept_verbatim(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(payment_method_hint="AMEX ****9999")
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.payment_method == "AMEX ****9999"

    def test_payment_method_none_is_empty_string(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(payment_method_hint=None)
        match = make_match()
        row = to_purchase_row(extract, match, categories, cfg, datetime(2026, 9, 2))
        assert row.payment_method == ""

    def test_date_defaulted_to_processing_date(self, categories: Categories, cfg: Config) -> None:
        extract = make_extract(date=None)
        match = make_match()
        processed_on = datetime(2026, 9, 5, 14, 30, 0)
        row = to_purchase_row(extract, match, categories, cfg, processed_on)
        assert row.date == date(2026, 9, 5)
        assert row.year == 2026
        assert "date defaulted to processing date" in row.notes
        # Nothing else about this extract is flagged, so status is unaffected by the date default.
        assert row.status == "OK"


def test_filename_hint_strips_file_request_uploader_name():
    """Dropbox File Requests may append the uploader's name in parentheses."""
    from datetime import date
    from kosaccounts.receipts.extract import parse_filename_hint

    assert parse_filename_hint("2026-09-12 - Mood (Yemi Osunkoya).pdf") == (date(2026, 9, 12), "Mood")
    assert parse_filename_hint("2026-09-12 - Mood receipt (Yemi) (1).jpg") == (date(2026, 9, 12), "Mood")
    assert parse_filename_hint("2026-09-12 - C&C Button Inc.png") == (date(2026, 9, 12), "C&C Button Inc")
