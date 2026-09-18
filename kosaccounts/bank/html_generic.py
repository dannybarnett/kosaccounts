"""Generic HTML statement parser (BeautifulSoup + lxml).

Heuristic: among all <table>s pick the one with the most rows whose header row contains a date-like
column, a description-like column (description/details/payee/memo/transaction) and an amount-like
column (amount, or separate debit/credit or withdrawal/deposit columns). Parse rows; skip totals.
Statement period: look for two dates in text near "statement period"/"from ... to"; else min/max txn date.
Direction: negative amounts, parenthesised amounts, or a debit/withdrawal column -> "debit".

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from bs4.element import Tag
from dateutil import parser as dateparser

from kosaccounts.models import BankTxn

logger = logging.getLogger(__name__)

DATE_HEADER_WORDS = ("date", "posted", "transaction date", "post date")
AMOUNT_HEADER_WORDS = (
    "amount",
    "debit",
    "credit",
    "withdrawal",
    "deposit",
    "withdrawals",
    "deposits",
)
DESCRIPTION_HEADER_WORDS = ("description", "details", "payee", "memo", "transaction", "merchant")

DEBIT_COLUMN_WORDS = ("debit", "withdrawal", "withdrawals")
CREDIT_COLUMN_WORDS = ("credit", "deposit", "deposits")
TYPE_COLUMN_WORDS = ("type",)
REF_COLUMN_WORDS = ("reference", "ref", "id")
BALANCE_COLUMN_WORDS = ("balance",)
DATE_COLUMN_WORDS = ("date", "posted", "posting date", "transaction date", "post date")

SKIP_FIRST_CELL_WORDS = ("total", "balance", "opening", "closing")

CREDIT_DESCRIPTION_WORDS = ("PAYMENT", "CREDIT", "REFUND", "DEPOSIT")
CREDIT_TYPE_WORDS = ("credit", "payment", "deposit")
DEBIT_TYPE_WORDS = ("debit", "purchase", "withdrawal", "sale")


def _cell_text(cell: Tag) -> str:
    return cell.get_text(separator=" ", strip=True)


def _normalize_header(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _row_cells(row: Tag) -> list[Tag]:
    return row.find_all(["td", "th"], recursive=False)


def _header_row_and_cells(table: Tag) -> Optional[list[Tag]]:
    """Return the header cells for a table: a row of <th>, or the first <tr>'s <td> cells."""
    thead = table.find("thead")
    if thead is not None:
        header_tr = thead.find("tr")
        if header_tr is not None:
            cells = _row_cells(header_tr)
            if cells:
                return cells

    rows = table.find_all("tr", recursive=True)
    if not rows:
        return None

    # Prefer a row made of <th> cells anywhere in the table.
    for row in rows:
        cells = _row_cells(row)
        if cells and all(c.name == "th" for c in cells):
            return cells

    # Fall back to the first row's <td> cells.
    first_cells = _row_cells(rows[0])
    if first_cells:
        return first_cells
    return None


def _classify_headers(headers: list[str]) -> dict[str, int]:
    """Map a semantic column name -> index, based on header keyword matches.

    Each header column is assigned to at most one category (priority order below), so an ambiguous
    header like "Transaction Date" is claimed by "date" and cannot also be claimed by "description"
    (which would otherwise match on the word "transaction").
    """
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        h_norm = _normalize_header(h)

        if "date" not in mapping and any(w in h_norm for w in DATE_COLUMN_WORDS):
            mapping["date"] = i
            continue
        if "debit" not in mapping and any(w in h_norm for w in DEBIT_COLUMN_WORDS):
            mapping["debit"] = i
            continue
        if "credit" not in mapping and any(w in h_norm for w in CREDIT_COLUMN_WORDS):
            mapping["credit"] = i
            continue
        if "amount" not in mapping and h_norm == "amount":
            mapping["amount"] = i
            continue
        if "type" not in mapping and any(w in h_norm for w in TYPE_COLUMN_WORDS):
            mapping["type"] = i
            continue
        if "ref" not in mapping and any(w in h_norm for w in REF_COLUMN_WORDS):
            mapping["ref"] = i
            continue
        if "balance" not in mapping and any(w in h_norm for w in BALANCE_COLUMN_WORDS):
            mapping["balance"] = i
            continue
        if "description" not in mapping and any(w in h_norm for w in DESCRIPTION_HEADER_WORDS):
            mapping["description"] = i
            continue

    # A generic "amount" header word match (covers e.g. "Amount ($)"), only for still-unclaimed columns.
    if "amount" not in mapping:
        claimed = set(mapping.values())
        for i, h in enumerate(headers):
            if i in claimed:
                continue
            h_norm = _normalize_header(h)
            if "amount" in h_norm:
                mapping["amount"] = i
                break
    return mapping


def _has_required_headers(headers: list[str]) -> bool:
    has_date = any(any(w in _normalize_header(h) for w in DATE_HEADER_WORDS) for h in headers)
    has_amount = any(any(w in _normalize_header(h) for w in AMOUNT_HEADER_WORDS) for h in headers)
    has_description = any(
        any(w in _normalize_header(h) for w in DESCRIPTION_HEADER_WORDS) for h in headers
    )
    return has_date and has_amount and has_description


def _find_qualifying_tables(soup: BeautifulSoup) -> list[tuple[Tag, list[Tag], list[str]]]:
    """Return (table, header_cells, header_texts) for every table with a qualifying header."""
    qualifying = []
    for table in soup.find_all("table"):
        header_cells = _header_row_and_cells(table)
        if not header_cells:
            continue
        header_texts = [_cell_text(c) for c in header_cells]
        if _has_required_headers(header_texts):
            qualifying.append((table, header_cells, header_texts))
    return qualifying


def _body_rows(table: Tag, header_cells: list[Tag]) -> list[Tag]:
    """All <tr> in the table that are not the header row and have at least one td."""
    header_row = header_cells[0].find_parent("tr") if header_cells else None
    rows = []
    for tr in table.find_all("tr", recursive=True):
        if tr is header_row:
            continue
        cells = _row_cells(tr)
        if not cells:
            continue
        if all(c.name == "th" for c in cells):
            continue
        rows.append(tr)
    return rows


AMOUNT_RE = re.compile(r"-?\$?\(?\s*-?[\d,]+\.?\d*\s*\)?\s*(CR|DR)?", re.IGNORECASE)


class ParsedAmount:
    """Result of parsing one amount cell.

    - magnitude: the absolute value.
    - explicit_direction: set only from an unambiguous "CR"/"DR" suffix -- the strongest signal,
      since it is the statement literally telling us the direction.
    - implicit_negative: True when the cell was written as negative or parenthesised, with no
      CR/DR suffix. This is a weaker signal than a description/type-column match: US credit-card
      statements commonly show purchases unsigned/positive and payments/credits as negative, so a
      bare negative sign does not always mean "debit" the way it would on a plain checking account.
    """

    def __init__(self, magnitude: Decimal, explicit_direction: Optional[str], implicit_negative: bool):
        self.magnitude = magnitude
        self.explicit_direction = explicit_direction
        self.implicit_negative = implicit_negative


def _parse_amount(text: str) -> Optional[ParsedAmount]:
    """Parse an amount cell such as "-12.34", "(12.34)", "$1,234.56", "12.34 CR"/"DR".
    Returns None if unparsable / empty."""
    raw = text.strip()
    if not raw:
        return None

    explicit_direction: Optional[str] = None
    working = raw

    # CR/DR suffix -- explicit, authoritative.
    m = re.search(r"\b(CR|DR)\b", working, re.IGNORECASE)
    if m:
        explicit_direction = "credit" if m.group(1).upper() == "CR" else "debit"
        working = working[: m.start()] + working[m.end():]

    working = working.strip()

    # Parentheses => negative.
    is_paren = working.startswith("(") and working.endswith(")")
    if is_paren:
        working = working[1:-1].strip()

    # Strip currency symbols and commas
    working = working.replace("$", "").replace(",", "").strip()
    if not working:
        return None

    is_negative = working.startswith("-")
    if is_negative:
        working = working.lstrip("-").strip()

    try:
        magnitude = Decimal(working)
    except InvalidOperation:
        return None

    implicit_negative = is_paren or is_negative

    return ParsedAmount(magnitude, explicit_direction, implicit_negative)


STATEMENT_PERIOD_PATTERNS = [
    re.compile(
        r"statement\s+period\s*:?\s*(?P<start>[A-Za-z0-9,/\-\. ]+?)\s*(?:-|to|through|–|—)\s*(?P<end>[A-Za-z0-9,/\-\. ]+?)(?=[\n\r<]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"from\s+(?P<start>[A-Za-z0-9,/\-\. ]+?)\s+(?:to|through)\s+(?P<end>[A-Za-z0-9,/\-\. ]+?)(?=[\n\r<.,]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<start>\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to|–|—)\s*(?P<end>\d{1,2}/\d{1,2}/\d{2,4})",
    ),
]


def _find_statement_period(text: str) -> tuple[Optional[date], Optional[date]]:
    for pattern in STATEMENT_PERIOD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            start = dateparser.parse(m.group("start").strip(), dayfirst=False).date()
            end = dateparser.parse(m.group("end").strip(), dayfirst=False).date()
            return start, end
        except (ValueError, OverflowError):
            continue
    return None, None


def _parse_date_cell(text: str, statement_year_hint: Optional[int]) -> Optional[date]:
    raw = text.strip()
    if not raw:
        return None
    try:
        default = None
        if statement_year_hint is not None:
            default = date(statement_year_hint, 1, 1)
        if default is not None:
            parsed = dateparser.parse(raw, dayfirst=False, default=_as_datetime(default))
        else:
            parsed = dateparser.parse(raw, dayfirst=False)
        return parsed.date()
    except (ValueError, OverflowError):
        return None


def _as_datetime(d: date):
    from datetime import datetime

    return datetime(d.year, d.month, d.day)


class HtmlGenericParser:
    name = "html_generic"

    def can_parse(self, path: Path) -> bool:
        """True for .html/.htm files that contain at least one table with a recognisable header."""
        if path.suffix.lower() not in (".html", ".htm"):
            return False
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            return False
        try:
            soup = BeautifulSoup(text, "lxml")
        except Exception:
            return False
        return bool(_find_qualifying_tables(soup))

    def parse(self, path: Path, source_file: str) -> list[BankTxn]:
        text = path.read_text(errors="ignore")
        soup = BeautifulSoup(text, "lxml")

        qualifying = _find_qualifying_tables(soup)
        if not qualifying:
            return []

        best_table, best_headers, best_header_texts = max(
            qualifying, key=lambda t: len(_body_rows(t[0], t[1]))
        )

        columns = _classify_headers(best_header_texts)
        if "date" not in columns or "description" not in columns:
            return []

        doc_text = soup.get_text(separator="\n")
        period_start, period_end = _find_statement_period(doc_text)
        year_hint = period_start.year if period_start else None

        rows = _body_rows(best_table, best_headers)
        txns: list[BankTxn] = []
        index = 0
        for row in rows:
            cells = _row_cells(row)
            if not cells:
                continue

            first_cell_text = _cell_text(cells[0]).strip().lower()
            if any(w in first_cell_text for w in SKIP_FIRST_CELL_WORDS):
                continue

            def cell_text(key: str) -> str:
                idx = columns.get(key)
                if idx is None or idx >= len(cells):
                    return ""
                return _cell_text(cells[idx])

            date_text = cell_text("date")
            description = cell_text("description")

            if not date_text or not description:
                continue

            txn_date = _parse_date_cell(date_text, year_hint)
            if txn_date is None:
                logger.debug("html_generic: skipping row with unparsable date %r in %s", date_text, source_file)
                continue

            direction: Optional[str] = None
            magnitude: Optional[Decimal] = None

            debit_text = cell_text("debit")
            credit_text = cell_text("credit")

            if (columns.get("debit") is not None or columns.get("credit") is not None) and (
                debit_text or credit_text
            ):
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
                amount_text = cell_text("amount")
                parsed = _parse_amount(amount_text)
                if parsed is None:
                    logger.debug(
                        "html_generic: skipping row with unparsable amount %r in %s",
                        amount_text,
                        source_file,
                    )
                    continue
                magnitude = parsed.magnitude

                # Priority (most to least authoritative):
                # 1. Explicit "CR"/"DR" suffix on the amount itself.
                # 2. A "type" column (debit/credit/purchase/payment words).
                # 3. Description looking like a payment/credit ("PAYMENT", "CREDIT", "REFUND",
                #    "DEPOSIT") -- handles US card statements where purchases are unsigned/positive
                #    but payments/credits are written as negative amounts: the description is a more
                #    reliable signal there than a bare "-" sign.
                # 4. A bare negative/parenthesised amount with no other signal -> debit (checking
                #    account convention: money left the account).
                # 5. Otherwise (positive, unsigned, no signal at all) -> debit, since US card
                #    statements list purchases as plain positive numbers.
                type_text = cell_text("type").strip().lower()
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
                continue

            balance_text = cell_text("balance")
            balance: Optional[Decimal] = None
            if balance_text:
                parsed_balance = _parse_amount(balance_text)
                if parsed_balance:
                    balance = parsed_balance.magnitude

            ref_text = cell_text("ref").strip()
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

        if period_start is None or period_end is None:
            dates = [t.date for t in txns]
            period_start = period_start or min(dates)
            period_end = period_end or max(dates)

        for t in txns:
            t.statement_start = period_start
            t.statement_end = period_end

        return txns
