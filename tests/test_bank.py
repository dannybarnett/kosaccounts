"""Tests for kosaccounts/bank/: registry, html_generic parser, model fallback."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from kosaccounts.bank import registry
from kosaccounts.bank.html_generic import HtmlGenericParser
from kosaccounts.bank.model_fallback import extract_with_model
from kosaccounts.bank.registry import parse_statement
from kosaccounts.claude_client import ClaudeError
from kosaccounts.models import BankTxn


class FakeClaudeClient:
    """Stands in for ClaudeClient without shelling out to the real `claude` binary."""

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    def run_json(self, prompt: str, files=()) -> dict:
        self.calls.append((prompt, tuple(files)))
        if self.error is not None:
            raise self.error
        return self.response


# ---------------------------------------------------------------------------
# HtmlGenericParser.can_parse
# ---------------------------------------------------------------------------


class TestCanParse:
    def test_sample_statement(self, fixtures_dir):
        parser = HtmlGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_sample.html") is True

    def test_debit_credit_statement(self, fixtures_dir):
        parser = HtmlGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_debit_credit.html") is True

    def test_no_transaction_table(self, fixtures_dir):
        parser = HtmlGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_no_table.html") is False

    def test_pdf_rejected(self, fixtures_dir):
        parser = HtmlGenericParser()
        assert parser.can_parse(Path("/tmp/not-a-real-statement.pdf")) is False

    def test_nonexistent_html_file(self, fixtures_dir):
        parser = HtmlGenericParser()
        assert parser.can_parse(fixtures_dir / "does_not_exist.html") is False


# ---------------------------------------------------------------------------
# HtmlGenericParser.parse - single Amount column fixture
# ---------------------------------------------------------------------------


class TestParseSampleStatement:
    @pytest.fixture
    def txns(self, fixtures_dir) -> list[BankTxn]:
        parser = HtmlGenericParser()
        path = fixtures_dir / "bank_statement_sample.html"
        return parser.parse(path, "bank/statement_aug.html")

    def test_row_count(self, txns):
        # 10 transaction rows; the totals row and the account-summary table are excluded.
        assert len(txns) == 10

    def test_all_are_banktxn_with_decimal_amounts(self, txns):
        for t in txns:
            assert isinstance(t, BankTxn)
            assert isinstance(t.amount, Decimal)
            assert t.amount >= 0

    def test_purchase_rows_are_debits(self, txns):
        by_desc = {t.description: t for t in txns}
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].direction == "debit"
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].amount == Decimal("19.50")
        assert by_desc["TMOBILE*AUTO PAY"].direction == "debit"
        assert by_desc["AMAZON.COM*2K4Y8 AMZN.COM/BILL"].direction == "debit"
        assert by_desc["SQ *MOOD FABRICS"].direction == "debit"
        assert by_desc["SQ *MOOD FABRICS"].amount == Decimal("156.32")

    def test_payment_and_credit_rows_are_credits(self, txns):
        by_desc = {t.description: t for t in txns}
        payment = by_desc["PAYMENT - THANK YOU"]
        assert payment.direction == "credit"
        assert payment.amount == Decimal("500.00")

        credit_adj = by_desc["CREDIT ADJUSTMENT"]
        assert credit_adj.direction == "credit"
        assert credit_adj.amount == Decimal("12.00")

    def test_totals_row_skipped(self, txns):
        descriptions = [t.description for t in txns]
        assert not any("total" in d.lower() for d in descriptions)

    def test_statement_period_from_header_text(self, txns):
        for t in txns:
            assert t.statement_start == date(2026, 8, 1)
            assert t.statement_end == date(2026, 8, 31)

    def test_refs_formatted_with_source_file_and_index(self, txns):
        # No reference/id column in this fixture -> fall back to f"{source_file}#{index}",
        # 1-based, in table order.
        assert txns[0].ref == "bank/statement_aug.html#1"
        assert txns[-1].ref == f"bank/statement_aug.html#{len(txns)}"
        refs = [t.ref for t in txns]
        assert refs == [f"bank/statement_aug.html#{i}" for i in range(1, len(txns) + 1)]

    def test_source_file_set(self, txns):
        for t in txns:
            assert t.source_file == "bank/statement_aug.html"

    def test_dates_parsed(self, txns):
        by_desc = {t.description: t for t in txns}
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].date == date(2026, 8, 2)
        assert by_desc["UPS STORE 0456"].date == date(2026, 8, 18)


# ---------------------------------------------------------------------------
# HtmlGenericParser.parse - separate Withdrawals/Deposits columns fixture
# ---------------------------------------------------------------------------


class TestParseDebitCreditStatement:
    @pytest.fixture
    def txns(self, fixtures_dir) -> list[BankTxn]:
        parser = HtmlGenericParser()
        path = fixtures_dir / "bank_statement_debit_credit.html"
        return parser.parse(path, "bank/checking_sep.html")

    def test_row_count(self, txns):
        assert len(txns) == 4

    def test_directions_and_amounts(self, txns):
        by_desc = {t.description: t for t in txns}

        grocery = by_desc["Grocery Store"]
        assert grocery.direction == "debit"
        assert grocery.amount == Decimal("45.67")

        payroll = by_desc["Payroll Deposit"]
        assert payroll.direction == "credit"
        assert payroll.amount == Decimal("1500.00")

        electric = by_desc["Electric Co"]
        assert electric.direction == "debit"
        assert electric.amount == Decimal("120.00")

        refund = by_desc["Refund - Office Depot"]
        assert refund.direction == "credit"
        assert refund.amount == Decimal("32.50")

    def test_balance_parsed(self, txns):
        by_desc = {t.description: t for t in txns}
        assert by_desc["Grocery Store"].balance == Decimal("954.33")
        assert by_desc["Payroll Deposit"].balance == Decimal("2454.33")

    def test_statement_period_from_from_through_text(self, txns):
        for t in txns:
            assert t.statement_start == date(2026, 9, 1)
            assert t.statement_end == date(2026, 9, 30)

    def test_totals_row_skipped(self, txns):
        assert all("total" not in t.description.lower() for t in txns)


# ---------------------------------------------------------------------------
# registry.parse_statement
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_html_parser_used_for_sample(self, fixtures_dir, cfg):
        txns, flagged = parse_statement(
            fixtures_dir / "bank_statement_sample.html", "bank/statement_aug.html", cfg
        )
        assert flagged is False
        assert len(txns) == 10

    def test_no_parser_no_client_returns_empty_and_flagged(self, fixtures_dir, cfg):
        txns, flagged = parse_statement(
            fixtures_dir / "bank_statement_no_table.html", "bank/welcome.html", cfg, client=None
        )
        assert txns == []
        assert flagged is True

    def test_no_parser_with_client_uses_model_fallback(self, fixtures_dir, cfg):
        canned = {
            "statement_start": "2026-08-01",
            "statement_end": "2026-08-31",
            "transactions": [
                {
                    "date": "2026-08-05",
                    "description": "MODEL EXTRACTED ROW",
                    "amount": 12.34,
                    "direction": "debit",
                    "balance": None,
                    "ref": None,
                }
            ],
        }
        client = FakeClaudeClient(response=canned)
        txns, flagged = parse_statement(
            fixtures_dir / "bank_statement_no_table.html", "bank/welcome.html", cfg, client=client
        )
        assert flagged is True
        assert len(txns) == 1
        assert isinstance(txns[0], BankTxn)
        assert txns[0].description == "MODEL EXTRACTED ROW"
        assert txns[0].amount == Decimal("12.34")
        assert txns[0].direction == "debit"
        assert client.calls, "expected the fake client's run_json to have been invoked"

    def test_model_fallback_claude_error_returns_empty_and_flagged(self, fixtures_dir, cfg):
        client = FakeClaudeClient(error=ClaudeError("boom"))
        txns, flagged = parse_statement(
            fixtures_dir / "bank_statement_no_table.html", "bank/welcome.html", cfg, client=client
        )
        assert txns == []
        assert flagged is True

    def test_registry_parsers_populated_at_import(self):
        assert len(registry.PARSERS) >= 1
        assert any(p.name == "html_generic" for p in registry.PARSERS)


# ---------------------------------------------------------------------------
# model_fallback.extract_with_model
# ---------------------------------------------------------------------------


class TestModelFallback:
    def test_maps_rows_to_banktxn(self, tmp_path):
        canned = {
            "statement_start": "2026-08-01",
            "statement_end": "2026-08-31",
            "transactions": [
                {
                    "date": "2026-08-02",
                    "description": "GOOD ROW",
                    "amount": 19.5,
                    "direction": "debit",
                    "balance": 100.25,
                    "ref": "TXN-001",
                },
                {
                    # malformed: unparsable amount, should be skipped rather than raising
                    "date": "2026-08-03",
                    "description": "BAD ROW",
                    "amount": "not-a-number",
                    "direction": "debit",
                    "balance": None,
                    "ref": None,
                },
            ],
        }
        client = FakeClaudeClient(response=canned)
        dummy_path = tmp_path / "statement.pdf"
        dummy_path.write_text("irrelevant")

        txns = extract_with_model(dummy_path, "bank/statement.pdf", client)

        assert len(txns) == 1
        t = txns[0]
        assert t.description == "GOOD ROW"
        assert t.amount == Decimal("19.5")
        assert t.direction == "debit"
        assert t.balance == Decimal("100.25")
        assert t.ref == "TXN-001"
        assert t.statement_start == date(2026, 8, 1)
        assert t.statement_end == date(2026, 8, 31)
        assert t.source_file == "bank/statement.pdf"

    def test_missing_ref_falls_back_to_source_file_and_index(self, tmp_path):
        canned = {
            "statement_start": None,
            "statement_end": None,
            "transactions": [
                {
                    "date": "2026-08-02",
                    "description": "NO REF ROW",
                    "amount": 5,
                    "direction": "credit",
                    "balance": None,
                    "ref": None,
                }
            ],
        }
        client = FakeClaudeClient(response=canned)
        dummy_path = tmp_path / "statement.pdf"
        dummy_path.write_text("irrelevant")

        txns = extract_with_model(dummy_path, "bank/statement.pdf", client)

        assert len(txns) == 1
        assert txns[0].ref == "bank/statement.pdf#1"

    def test_claude_error_propagates(self, tmp_path):
        client = FakeClaudeClient(error=ClaudeError("subprocess failed"))
        dummy_path = tmp_path / "statement.pdf"
        dummy_path.write_text("irrelevant")

        with pytest.raises(ClaudeError):
            extract_with_model(dummy_path, "bank/statement.pdf", client)
