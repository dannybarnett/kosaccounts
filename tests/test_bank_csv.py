"""Tests for kosaccounts/bank/csv_generic.py: CsvGenericParser + registry integration."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from kosaccounts.bank.csv_generic import CsvGenericParser
from kosaccounts.bank.registry import parse_statement
from kosaccounts.models import BankTxn


# ---------------------------------------------------------------------------
# CsvGenericParser.can_parse
# ---------------------------------------------------------------------------


class TestCanParse:
    def test_capitalone_statement(self, fixtures_dir):
        parser = CsvGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_capitalone.csv") is True

    def test_amount_signed_statement(self, fixtures_dir):
        parser = CsvGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_amount_signed.csv") is True

    def test_withdrawals_statement(self, fixtures_dir):
        parser = CsvGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_withdrawals.csv") is True

    def test_html_fixture_rejected(self, fixtures_dir):
        parser = CsvGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_sample.html") is False

    def test_csv_with_no_recognisable_header_rejected(self, fixtures_dir):
        parser = CsvGenericParser()
        assert parser.can_parse(fixtures_dir / "bank_statement_csv_no_header.csv") is False


# ---------------------------------------------------------------------------
# CsvGenericParser.parse - Capital One style (Debit/Credit columns, preamble-free)
# ---------------------------------------------------------------------------


class TestParseCapitalOne:
    @property
    def source_file(self) -> str:
        return "bank/capitalone_aug.csv"

    def _txns(self, fixtures_dir) -> list[BankTxn]:
        parser = CsvGenericParser()
        path = fixtures_dir / "bank_statement_capitalone.csv"
        return parser.parse(path, self.source_file)

    def test_row_count(self, fixtures_dir):
        txns = self._txns(fixtures_dir)
        assert len(txns) == 8

    def test_all_are_banktxn_with_decimal_amounts(self, fixtures_dir):
        for t in self._txns(fixtures_dir):
            assert isinstance(t, BankTxn)
            assert isinstance(t.amount, Decimal)
            assert t.amount >= 0

    def test_purchase_rows_are_debits(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].direction == "debit"
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].amount == Decimal("19.50")
        assert by_desc["TMOBILE*AUTO PAY"].direction == "debit"
        assert by_desc["TMOBILE*AUTO PAY"].amount == Decimal("85.00")
        assert by_desc["AMAZON.COM*2K4Y8"].direction == "debit"
        assert by_desc["AMAZON.COM*2K4Y8"].amount == Decimal("42.17")
        assert by_desc["SQ *MOOD FABRICS"].direction == "debit"
        assert by_desc["SQ *MOOD FABRICS"].amount == Decimal("156.32")
        assert by_desc["STAPLES STORE #1234"].direction == "debit"
        assert by_desc["UBER TRIP"].direction == "debit"

    def test_payment_and_refund_rows_are_credits(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        payment = by_desc["CAPITAL ONE AUTOPAY PYMT"]
        assert payment.direction == "credit"
        assert payment.amount == Decimal("300.00")

        refund = by_desc["REFUND ADJUSTMENT"]
        assert refund.direction == "credit"
        assert refund.amount == Decimal("19.50")

    def test_refs_formatted_with_source_file_and_index(self, fixtures_dir):
        txns = self._txns(fixtures_dir)
        assert txns[0].ref == f"{self.source_file}#1"
        assert txns[-1].ref == f"{self.source_file}#{len(txns)}"
        refs = [t.ref for t in txns]
        assert refs == [f"{self.source_file}#{i}" for i in range(1, len(txns) + 1)]

    def test_source_file_set(self, fixtures_dir):
        for t in self._txns(fixtures_dir):
            assert t.source_file == self.source_file

    def test_transaction_date_used_not_posted_date(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        # Transaction Date for this row is 2026-08-02; Posted Date is 2026-08-04.
        assert by_desc["PACIFIC TRIMMING NEW YORK NY"].date == date(2026, 8, 2)
        # Transaction Date for this row is 2026-08-25; Posted Date is 2026-08-26.
        assert by_desc["REFUND ADJUSTMENT"].date == date(2026, 8, 25)

    def test_statement_period_is_min_max_transaction_date(self, fixtures_dir):
        txns = self._txns(fixtures_dir)
        for t in txns:
            assert t.statement_start == date(2026, 8, 2)
            assert t.statement_end == date(2026, 8, 25)


# ---------------------------------------------------------------------------
# CsvGenericParser.parse - signed Amount column, preamble lines, UTF-8 BOM
# ---------------------------------------------------------------------------


class TestParseAmountSigned:
    def _txns(self, fixtures_dir) -> list[BankTxn]:
        parser = CsvGenericParser()
        path = fixtures_dir / "bank_statement_amount_signed.csv"
        return parser.parse(path, "bank/signed.csv")

    def test_row_count_preamble_skipped(self, fixtures_dir):
        txns = self._txns(fixtures_dir)
        assert len(txns) == 4

    def test_bom_tolerated_first_row_parsed(self, fixtures_dir):
        txns = self._txns(fixtures_dir)
        by_desc = {t.description: t for t in txns}
        assert "AMAZON.COM ORDER" in by_desc
        assert by_desc["AMAZON.COM ORDER"].date == date(2026, 8, 1)

    def test_negative_amounts_are_debits(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        amazon = by_desc["AMAZON.COM ORDER"]
        assert amazon.direction == "debit"
        assert amazon.amount == Decimal("42.17")

        whole_foods = by_desc["WHOLE FOODS MARKET"]
        assert whole_foods.direction == "debit"
        assert whole_foods.amount == Decimal("65.30")

        netflix = by_desc["NETFLIX.COM"]
        assert netflix.direction == "debit"
        assert netflix.amount == Decimal("15.99")

    def test_positive_payment_is_credit(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        payment = by_desc["ONLINE PAYMENT - THANK YOU"]
        assert payment.direction == "credit"
        assert payment.amount == Decimal("500.00")


# ---------------------------------------------------------------------------
# CsvGenericParser.parse - Withdrawals/Deposits/Balance columns
# ---------------------------------------------------------------------------


class TestParseWithdrawals:
    def _txns(self, fixtures_dir) -> list[BankTxn]:
        parser = CsvGenericParser()
        path = fixtures_dir / "bank_statement_withdrawals.csv"
        return parser.parse(path, "bank/checking_jul.csv")

    def test_row_count(self, fixtures_dir):
        assert len(self._txns(fixtures_dir)) == 4

    def test_directions_and_amounts(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}

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

    def test_balance_parsed(self, fixtures_dir):
        by_desc = {t.description: t for t in self._txns(fixtures_dir)}
        assert by_desc["Grocery Store"].balance == Decimal("954.33")
        assert by_desc["Payroll Deposit"].balance == Decimal("2454.33")
        assert by_desc["Electric Co"].balance == Decimal("2334.33")
        assert by_desc["Refund - Office Depot"].balance == Decimal("2366.83")


# ---------------------------------------------------------------------------
# registry.parse_statement
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_csv_parser_used_for_capitalone(self, fixtures_dir, cfg):
        txns, flagged = parse_statement(
            fixtures_dir / "bank_statement_capitalone.csv", "bank/capitalone_aug.csv", cfg
        )
        assert flagged is False
        assert len(txns) == 8
