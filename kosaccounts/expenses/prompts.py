"""Prompt builders for expense extraction. Pure functions returning strings.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

EXPENSE_JSON_KEYS = [
    "date",  # YYYY-MM-DD or null
    "supplier_name",  # as printed on the receipt, or null
    "total",  # number or null
    "sales_tax",  # number or null (0 if the receipt shows no tax line)
    "net",  # number or null
    "tax_rate",  # percent number or null
    "currency",  # ISO code or null
    "payment_method_hint",  # string or null
    "line_summary",  # <= 12 words describing what was bought
    "confidence",  # 0..1
    "notes",  # list of strings: anything odd (partial receipt, handwriting, tips, multiple pages)
]

MAX_PDF_TEXT_CHARS = 6000


def expense_prompt(
    file_path: Path,
    date_hint: Optional[date],
    supplier_hint: Optional[str],
    pdf_text: Optional[str],
    payment_methods: list[str],
) -> str:
    """Instruct the model to Read `file_path`, use hints only as tie-breakers, never guess (use null),
    and reply with ONLY a JSON object containing exactly EXPENSE_JSON_KEYS."""
    hint_lines = []
    if date_hint is not None:
        hint_lines.append(f"- Filename suggests date: {date_hint.isoformat()}")
    if supplier_hint:
        hint_lines.append(f"- Filename suggests supplier: {supplier_hint}")
    hints_block = (
        "\n".join(hint_lines)
        if hint_lines
        else "- (none available from the filename)"
    )

    methods_block = ", ".join(payment_methods) if payment_methods else "(none configured)"

    pdf_block = ""
    if pdf_text:
        truncated = pdf_text[:MAX_PDF_TEXT_CHARS]
        pdf_block = (
            "\n\nText extracted from the PDF (may be incomplete or out of order; "
            "if it disagrees with what you see in the file itself, trust the file):\n"
            "-----BEGIN PDF TEXT-----\n"
            f"{truncated}\n"
            "-----END PDF TEXT-----"
        )

    return f"""Use your Read tool to open this file: {file_path}

It is a purchase receipt (or invoice) for a small fashion business based in New York. Extract
what is actually printed or written on it.

Filename hints (tie-breakers ONLY - never let these override what the receipt itself shows;
use them only to disambiguate when the receipt is genuinely ambiguous, e.g. a faint date):
{hints_block}
{pdf_block}

Known payment method names used by this business: {methods_block}
For payment_method_hint, copy the raw text shown on the receipt for how it was paid (e.g.
"VISA ****1234", "MC ending 4402", "CASH"). Do NOT guess or normalise it to one of the names
above - just transcribe what's there, or null if nothing indicates payment method.

Rules:
- If a field is not clearly visible on the receipt, set it to null. NEVER guess or estimate.
- total is the amount actually paid, including tax and tip if any.
- sales_tax is 0 only if the receipt explicitly shows a tax line/amount of zero or states no tax
  was charged; if there is no tax information at all, set sales_tax to null.
- tax_rate is a percentage (e.g. 8.875), not a fraction.
- confidence is your own 0..1 estimate of how confident you are in this extraction as a whole.
- notes is a list of short strings for anything odd: partial/cut-off receipt, handwriting,
  multiple pages, a tip included, illegible amounts, etc. Empty list if nothing odd.

Reply with ONLY a single JSON object, no prose, no markdown code fences, containing exactly
these keys: {", ".join(EXPENSE_JSON_KEYS)}."""
