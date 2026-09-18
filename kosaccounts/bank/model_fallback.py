"""Model-based statement extraction for formats with no parser yet.

Prompt asks the model to Read the file and return ONLY a JSON object:
{"statement_start": "YYYY-MM-DD"|null, "statement_end": ..., "transactions": [
   {"date": "YYYY-MM-DD", "description": str, "amount": number, "direction": "debit"|"credit",
    "balance": number|null, "ref": str|null}, ...]}

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dateutil import parser as dateparser

from kosaccounts.claude_client import ClaudeClient
from kosaccounts.models import BankTxn

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """\
You are extracting transactions from a bank statement file for a bookkeeping pipeline.

Read the file listed below (it is a bank statement, likely HTML or PDF). Extract every transaction
row you find and return ONLY a single JSON object, with no prose, no markdown fences, and no
explanation before or after it. The JSON object must have exactly this shape:

{{
  "statement_start": "YYYY-MM-DD" or null,
  "statement_end": "YYYY-MM-DD" or null,
  "transactions": [
    {{
      "date": "YYYY-MM-DD",
      "description": "string",
      "amount": positive number (always positive magnitude, never negative),
      "direction": "debit" or "credit" ("debit" = money out / a purchase or payment made,
                    "credit" = money in / a payment received, refund, or deposit),
      "balance": number or null,
      "ref": "string" or null (a reference/id from the statement if one is printed, else null)
    }}
  ]
}}

Rules:
- Amounts must be positive numbers; the sign is carried by "direction", never by a negative amount.
- Dates must be ISO format YYYY-MM-DD.
- Never invent rows. Only include transactions you can actually see in the file. If you cannot find
  any transactions, return an empty "transactions" list.
- Do not include summary/total rows as transactions.
- Return ONLY the JSON object described above.
"""


def extract_with_model(path: Path, source_file: str, client: ClaudeClient) -> list[BankTxn]:
    prompt = PROMPT_TEMPLATE
    response = client.run_json(prompt, files=[path])

    raw_transactions = response.get("transactions") or []
    statement_start = _parse_date(response.get("statement_start"))
    statement_end = _parse_date(response.get("statement_end"))

    txns: list[BankTxn] = []
    for i, row in enumerate(raw_transactions, start=1):
        if not isinstance(row, dict):
            logger.warning("model_fallback: skipping non-dict row %r in %s", row, source_file)
            continue

        txn_date = _parse_date(row.get("date"))
        description = row.get("description")
        amount_raw = row.get("amount")
        direction = row.get("direction")

        if txn_date is None:
            logger.warning("model_fallback: skipping row with unparsable date %r in %s", row.get("date"), source_file)
            continue
        if not description or not isinstance(description, str):
            logger.warning("model_fallback: skipping row with missing description in %s: %r", source_file, row)
            continue
        if direction not in ("debit", "credit"):
            logger.warning("model_fallback: skipping row with invalid direction %r in %s", direction, source_file)
            continue

        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning("model_fallback: skipping row with unparsable amount %r in %s", amount_raw, source_file)
            continue

        balance = None
        if row.get("balance") is not None:
            try:
                balance = Decimal(str(row.get("balance")))
            except (InvalidOperation, TypeError, ValueError):
                balance = None

        ref = row.get("ref")
        if not ref or not isinstance(ref, str):
            ref = f"{source_file}#{i}"

        txns.append(
            BankTxn(
                date=txn_date,
                description=description,
                amount=amount,
                direction=direction,
                source_file=source_file,
                ref=ref,
                balance=balance,
                statement_start=statement_start,
                statement_end=statement_end,
            )
        )

    return txns


def _parse_date(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return dateparser.parse(value, dayfirst=False).date()
    except (ValueError, OverflowError, TypeError):
        return None
