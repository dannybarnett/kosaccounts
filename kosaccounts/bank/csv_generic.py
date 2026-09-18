"""Generic CSV statement parser.

Heuristic: scan the first 20 lines for a row that classifies (using html_generic's header
keyword logic) to at least a date column, a description column and an amount-like column
(amount, or debit/credit, or withdrawal/deposit). That row is the header; any lines before it
are a preamble (seen on some Chase/Bank of America exports) and are skipped.

Column mapping and per-row amount/direction rules reuse html_generic's keyword lists and helpers
verbatim so the two parsers agree on what "debit"/"credit"/"amount"/"type"/"ref"/"balance"/
"description" mean. Direction priority (most to least authoritative): explicit CR/DR suffix on
the amount cell; separate debit/credit (or withdrawal/deposit) columns; a type column
(debit/credit/purchase/payment words); credit-ish words in the description ("PAYMENT", "CREDIT",
"REFUND", "DEPOSIT"); a bare negative/parenthesised amount; otherwise debit.

Dates: when both a "Transaction Date" and a "Posted Date" column are present (Capital One's
export), the transaction date is used. CSV exports carry no statement-period text, so
statement_start/end are the min/max parsed transaction date.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal
from pathlib import Path
from typing import Optional

from kosaccounts.bank.html_generic import (
    CREDIT_DESCRIPTION_WORDS,
    CREDIT_TYPE_WORDS,
    DEBIT_TYPE_WORDS,
    SKIP_FIRST_CELL_WORDS,
    _classify_headers,
    _has_required_headers,
    _normalize_header,
    _parse_amount,
    _parse_date_cell,
)
from kosaccounts.models import BankTxn

logger = logging.getLogger(__name__)

MAX_HEADER_SEARCH_LINES = 20


def _sniff_dialect(sample: str) -> type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel  # comma-delimited fallback


def _read_rows(path: Path, limit: Optional[int] = None) -> list[list[str]]:
    """Read CSV rows (tolerating a UTF-8 BOM), sniffing the dialect with a comma fallback."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = _sniff_dialect(sample)
        reader = csv.reader(f, dialect)
        rows: list[list[str]] = []
        for row in reader:
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
        return rows


def _find_header_index(rows: list[list[str]]) -> Optional[int]:
    for i, row in enumerate(rows[:MAX_HEADER_SEARCH_LINES]):
        if not row or all(not c.strip() for c in row):
            continue
        if _has_required_headers(row):
            return i
    return None


def _select_date_index(headers: list[str], columns: dict[str, int]) -> Optional[int]:
    """Prefer a "Transaction Date" column over "Posted Date" when both are present."""
    for i, h in enumerate(headers):
        if "transaction date" in _normalize_header(h):
            return i
    return columns.get("date")


class CsvGenericParser:
    name = "csv_generic"

    def can_parse(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            rows = _read_rows(path, limit=MAX_HEADER_SEARCH_LINES)
        except (OSError, UnicodeDecodeError, csv.Error):
            return False
        return _find_header_index(rows) is not None

    def parse(self, path: Path, source_file: str) -> list[BankTxn]:
        rows = _read_rows(path)
        header_idx = _find_header_index(rows)
        if header_idx is None:
            raise ValueError(f"csv_generic: no recognisable header row found in {source_file}")

        headers = rows[header_idx]
        columns = _classify_headers(headers)
        date_idx = _select_date_index(headers, columns)

        n_headers = len(headers)

        def cell_text(row: list[str], key: str) -> str:
            idx = columns.get(key)
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        def date_cell_text(row: list[str]) -> str:
            if date_idx is None or date_idx >= len(row):
                return ""
            return row[date_idx].strip()

        txns: list[BankTxn] = []
        index = 0
        for row in rows[header_idx + 1 :]:
            if not row or all(not c.strip() for c in row):
                continue
            # Pad short rows so index lookups behave like a missing/blank cell.
            if len(row) < n_headers:
                row = row + [""] * (n_headers - len(row))

            first_cell_text = row[0].strip().lower()
            if any(w in first_cell_text for w in SKIP_FIRST_CELL_WORDS):
                continue

            date_text = date_cell_text(row)
            description = cell_text(row, "description")

            if not date_text or not description:
                logger.debug(
                    "csv_generic: skipping row missing date/description in %s: %r", source_file, row
                )
                continue

            txn_date = _parse_date_cell(date_text, None)
            if txn_date is None:
                logger.debug(
                    "csv_generic: skipping row with unparsable date %r in %s", date_text, source_file
                )
                continue

            direction: Optional[str] = None
            magnitude: Optional[Decimal] = None

            debit_text = cell_text(row, "debit")
            credit_text = cell_text(row, "credit")

            if columns.get("debit") is not None or columns.get("credit") is not None:
                if debit_text:
                    parsed = _parse_amount(debit_text)
                    if parsed:
                        magnitude = parsed.magnitude
                        direction = "debit"
                elif credit_text:
                    parsed = _parse_amount(credit_text)
                    if parsed:
                        magnitude = parsed.magnitude
                        direction = "credit"
                else:
                    logger.debug(
                        "csv_generic: skipping row with empty debit and credit cells in %s: %r",
                        source_file,
                        row,
                    )
                    continue
            else:
                amount_text = cell_text(row, "amount")
                parsed = _parse_amount(amount_text)
                if parsed is None:
                    logger.debug(
                        "csv_generic: skipping row with unparsable amount %r in %s",
                        amount_text,
                        source_file,
                    )
                    continue
                magnitude = parsed.magnitude

                type_text = cell_text(row, "type").lower()
                if parsed.explicit_direction is not None:
                    direction = parsed.explicit_direction
                elif type_text:
                    if any(w in type_text for w in CREDIT_TYPE_WORDS):
                        direction = "credit"
                    elif any(w in type_text for w in DEBIT_TYPE_WORDS):
                        direction = "debit"
                if direction is None:
                    desc_upper = description.upper()
                    if any(w in desc_upper for w in CREDIT_DESCRIPTION_WORDS):
                        direction = "credit"
                    elif parsed.implicit_negative:
                        direction = "debit"
                    else:
                        direction = "debit"

            if magnitude is None or direction is None:
                logger.debug("csv_generic: skipping row with no resolvable amount in %s: %r", source_file, row)
                continue

            balance_text = cell_text(row, "balance")
            balance: Optional[Decimal] = None
            if balance_text:
                parsed_balance = _parse_amount(balance_text)
                if parsed_balance:
                    balance = parsed_balance.magnitude

            ref_text = cell_text(row, "ref")
            index += 1
            ref = ref_text if ref_text else f"{source_file}#{index}"

            txns.append(
                BankTxn(
                    date=txn_date,
                    description=description,
                    amount=magnitude,
                    direction=direction,  # type: ignore[arg-type]
                    source_file=source_file,
                    ref=ref,
                    balance=balance,
                )
            )

        if not txns:
            return []

        dates = [t.date for t in txns]
        period_start = min(dates)
        period_end = max(dates)
        for t in txns:
            t.statement_start = period_start
            t.statement_end = period_end

        return txns
