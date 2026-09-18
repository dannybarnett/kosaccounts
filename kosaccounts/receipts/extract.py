"""Turn one receipt file into a ReceiptExtract, then into a PurchaseRow.

Flow for extract_receipt():
1. parse_filename_hint(file.filename) -> (date, supplier) hint.
2. If PDF: pdfplumber text (may be empty for scans). If image (jpg/png/heic/webp/tiff): convert to
   JPEG <= cfg.receipts.max_image_px on the long side into a sibling ".converted/" folder next to the
   file (idempotent), and hand that path to the model.
3. Build prompt (receipts/prompts.py) with hints + any PDF text + allowed payment methods; call
   client.run_json(prompt, files=[path]) and map to ReceiptExtract (Decimal for money, ISO date).
4. validate(): fill net/tax/total when exactly one is missing; check net+tax==total within
   cfg.receipts.amount_tolerance; derive tax_rate if absent; non-USD -> note "Currency XXX; converted?";
   date missing -> use hint; supplier missing -> use hint. Every problem appends to .notes.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from dateutil import parser as dateutil_parser

from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeClient, ClaudeError
from kosaccounts.config import Config
from kosaccounts.models import DiscoveredFile, PurchaseRow, ReceiptExtract, SupplierMatch
from kosaccounts.receipts.prompts import receipt_prompt

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".heif"}
PDF_SUFFIXES = {".pdf"}

# Leading date: YYYY-MM-DD or YYYYMMDD, optionally followed by more digits/separators.
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})|^(\d{4})(\d{2})(\d{2})(?!\d)")
_SEP_RE = re.compile(r"^[\s\-_]+")
# Trailing noise to drop from the supplier hint: "(1)" copy markers, the word "receipt", and any
# parenthesised group such as the uploader's name that Dropbox File Requests may append.
_TRAILING_NOISE_RE = re.compile(r"\s*(\([^()]*\)|receipt)\s*$", re.IGNORECASE)

# C0 control characters that can end up in pdfplumber output (e.g. \x00 from malformed PDFs) and
# break subprocess argv; \n and \t are left alone since they're harmless and legible in a prompt.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_pdf_text(text: Optional[str]) -> Optional[str]:
    """Strip NUL and other C0 control characters (except \\n, \\t) from extracted PDF text."""
    if text is None:
        return None
    return _CONTROL_CHARS_RE.sub("", text)


def apply_filename_strip(supplier_hint: Optional[str], strip_list: list[str]) -> Optional[str]:
    """Remove each `strip_list` entry from `supplier_hint` (case-insensitive whole-word match,
    tolerant of surrounding spaces/dashes/underscores), then strip leftover separators.
    'Guide Fabrics INC Yemi Osunkoya' with strip_list=['Yemi Osunkoya'] -> 'Guide Fabrics INC'.
    'Yemi Osunkoya' -> None (nothing left)."""
    if not supplier_hint:
        return None

    result = supplier_hint
    for term in strip_list:
        term = (term or "").strip()
        if not term:
            continue
        pattern = re.compile(r"[\s\-_]*\b" + re.escape(term) + r"\b[\s\-_]*", re.IGNORECASE)
        result = pattern.sub(" ", result)

    result = re.sub(r"\s+", " ", result).strip(" \t-_")
    return result or None


def parse_filename_hint(filename: str) -> tuple[Optional[date], Optional[str]]:
    """'2026-09-01 - Mood.pdf' -> (date(2026,9,1), 'Mood'); '2026-09-01 Mood Fabrics.jpg' works too;
    'IMG_4471.jpg' -> (None, None)."""
    stem = Path(filename).stem

    m = _DATE_RE.match(stem)
    if not m:
        return (None, None)

    if m.group(1) is not None:
        year, month, day = m.group(1), m.group(2), m.group(3)
    else:
        year, month, day = m.group(4), m.group(5), m.group(6)

    rest = stem[m.end():]
    rest = _SEP_RE.sub("", rest, count=1)

    supplier: Optional[str] = None
    rest = rest.strip()
    if rest:
        cleaned = rest
        while True:  # strip repeatedly: "Mood receipt (Yemi) (1)"
            stripped = _TRAILING_NOISE_RE.sub("", cleaned).strip()
            if stripped == cleaned:
                break
            cleaned = stripped
        supplier = cleaned or None

    try:
        parsed_date: Optional[date] = date(int(year), int(month), int(day))
    except ValueError:
        parsed_date = None

    if parsed_date is None:
        return (None, supplier)

    return (parsed_date, supplier)


def _pdf_text(path: Path) -> Optional[str]:
    """Best-effort pdfplumber text extraction. Returns None on any failure, "" if no text found."""
    try:
        import pdfplumber

        texts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                texts.append(page_text)
        joined = "\n".join(texts)
        return joined if joined.strip() else ""
    except Exception:
        return None


def _convert_image_for_model(path: Path, max_px: int) -> Path:
    """Convert `path` to a JPEG (RGB, quality 85, long side <= max_px) under a sibling
    ".converted/" folder. Idempotent: reused if already newer than the source."""
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()

    converted_dir = path.parent / ".converted"
    converted_path = converted_dir / (path.stem + ".jpg")

    if converted_path.exists() and converted_path.stat().st_mtime >= path.stat().st_mtime:
        return converted_path

    converted_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(path) as img:
        img = img.convert("RGB")
        long_side = max(img.size)
        if long_side > max_px:
            scale = max_px / long_side
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(new_size, Image.LANCZOS)
        img.save(converted_path, "JPEG", quality=85)

    return converted_path


def _to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return dateutil_parser.parse(text).date()
        except (ValueError, OverflowError, TypeError):
            return None
    return None


def _to_notes(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


def extract_receipt(file: DiscoveredFile, client: ClaudeClient, cfg: Config, payment_methods: list[str]) -> ReceiptExtract:
    date_hint, raw_supplier_hint = parse_filename_hint(file.filename)
    supplier_hint = apply_filename_strip(raw_supplier_hint, cfg.receipts.filename_strip)
    hints = {"date": date_hint, "supplier": supplier_hint}

    suffix = file.path.suffix.lower()

    if suffix in PDF_SUFFIXES:
        pdf_text = sanitize_pdf_text(_pdf_text(file.path))
        model_path = file.path
    elif suffix in IMAGE_SUFFIXES:
        pdf_text = None
        try:
            model_path = _convert_image_for_model(file.path, cfg.receipts.max_image_px)
        except Exception as exc:
            extract = ReceiptExtract(
                source_file=file.relative_path,
                confidence=0.0,
                notes=[f"image conversion failed: {exc}"],
            )
            extract.raw["_hints"] = {"date": date_hint, "supplier": supplier_hint}
            return validate(extract, cfg)
    else:
        extract = ReceiptExtract(
            source_file=file.relative_path,
            confidence=0.0,
            notes=["unsupported file type"],
        )
        extract.raw["_hints"] = {"date": date_hint, "supplier": supplier_hint}
        return validate(extract, cfg)

    prompt = receipt_prompt(model_path, date_hint, supplier_hint, pdf_text, payment_methods)

    try:
        data = client.run_json(prompt, files=[model_path])
    except ClaudeError as exc:
        extract = ReceiptExtract(
            source_file=file.relative_path,
            confidence=0.0,
            notes=[f"model extraction failed: {exc}"],
        )
        extract.raw["_hints"] = {"date": date_hint, "supplier": supplier_hint}
        return validate(extract, cfg)

    extract = ReceiptExtract(
        source_file=file.relative_path,
        date=_to_date(data.get("date")),
        supplier_name=(data.get("supplier_name") or None),
        total=_to_decimal(data.get("total")),
        sales_tax=_to_decimal(data.get("sales_tax")),
        net=_to_decimal(data.get("net")),
        tax_rate=_to_decimal(data.get("tax_rate")),
        currency=(data.get("currency") or None),
        payment_method_hint=(data.get("payment_method_hint") or None),
        line_summary=(data.get("line_summary") or None),
        confidence=float(data.get("confidence") or 0.0),
        notes=_to_notes(data.get("notes")),
        raw=data,
    )
    extract.raw["_hints"] = {"date": date_hint, "supplier": supplier_hint}
    return validate(extract, cfg)


_RECONCILE_PROBLEM = "amounts do not reconcile: net+tax != total"
_NO_TAX_NOTE = "no tax shown; net = total"


def validate(extract: ReceiptExtract, cfg: Config) -> ReceiptExtract:
    """Mutates and returns `extract`; never raises for data problems.

    Model observations (whatever the model put in extract.notes) are left alone and never treated
    as problems. Validation adds its own messages to .notes for the human reviewer, and additionally
    records genuine problems (amounts don't reconcile, non-USD currency, missing date/supplier/total)
    in extract.raw["_problems"] -- that list, not .notes, is what to_purchase_row() uses to decide
    Status=Review. "No tax shown" is a note, never a problem: most purchases here are resale (no
    sales tax), so a receipt with a total but no tax line is normal, not a defect.
    """
    try:
        tolerance = Decimal(str(cfg.receipts.amount_tolerance))
    except InvalidOperation:
        tolerance = Decimal("0.01")

    problems: list[str] = []

    total, net, tax = extract.total, extract.net, extract.sales_tax

    if total is not None and net is None and tax is None:
        # No tax line at all: assume resale (no sales tax) rather than flagging for review.
        extract.sales_tax = Decimal("0")
        extract.net = total
        extract.tax_rate = Decimal("0")
        extract.notes.append(_NO_TAX_NOTE)
    elif total is not None and net is not None and tax is None:
        derived_tax = total - net
        if derived_tax >= 0:
            extract.sales_tax = derived_tax
        else:
            extract.notes.append(_RECONCILE_PROBLEM)
            problems.append(_RECONCILE_PROBLEM)
    elif total is not None and net is None and tax is not None:
        extract.net = total - tax
    elif total is None and net is not None and tax is not None:
        extract.total = net + tax
    elif total is None and net is not None and tax is None:
        extract.total = net
        extract.sales_tax = Decimal("0")
        extract.notes.append(_NO_TAX_NOTE)
    # else: total is None and net is None (tax present or not) -- nothing to derive; the
    # "total missing" problem below covers it.

    if extract.net is not None and extract.sales_tax is not None and extract.total is not None:
        if abs((extract.net + extract.sales_tax) - extract.total) > tolerance:
            extract.notes.append(_RECONCILE_PROBLEM)
            problems.append(_RECONCILE_PROBLEM)

    if extract.tax_rate is None and extract.net is not None and extract.net > 0 and extract.sales_tax is not None:
        if extract.sales_tax == 0:
            extract.tax_rate = Decimal("0")
        else:
            rate = (extract.sales_tax / extract.net * 100).quantize(Decimal("0.001"))
            extract.tax_rate = rate

    if extract.currency and extract.currency != cfg.receipts.default_currency:
        msg = f"currency {extract.currency}: convert to USD and note the rate"
        extract.notes.append(msg)
        problems.append(msg)

    hints = extract.raw.get("_hints", {}) if isinstance(extract.raw, dict) else {}
    hint_date = hints.get("date")
    hint_supplier = hints.get("supplier")

    if extract.date is None and hint_date is not None:
        extract.date = hint_date
    if extract.supplier_name is None and hint_supplier:
        extract.supplier_name = hint_supplier

    if extract.date is None:
        msg = "date missing"
        extract.notes.append(msg)
        problems.append(msg)
    if not extract.supplier_name:
        msg = "supplier missing"
        extract.notes.append(msg)
        problems.append(msg)
    if extract.total is None and extract.net is None:
        msg = "total missing"
        extract.notes.append(msg)
        problems.append(msg)

    if problems:
        extract.raw["_problems"] = problems

    return extract


_METHOD_ALIASES = {
    "mastercard": "Mastercard",
    "mc": "Mastercard",
    "master card": "Mastercard",
    "visa": "Visa",
    "discover": "Discover",
    "paypal": "Paypal",
    "cashapp": "CashApp",
    "cash app": "CashApp",
    "cash": "Cash",
    "cheque": "Cheque",
    "check": "Check",
    "bank transfer": "Bank transfer",
    "wire transfer": "Wire transfer",
    "amex": "American Express",
    "american express": "American Express",
}


def _normalise_payment_method(hint: Optional[str], payment_methods: list[str]) -> str:
    """Map a raw payment-method hint onto one of the known `payment_methods` names."""
    if not hint:
        return ""

    hint_lower = hint.strip().lower()
    if not hint_lower:
        return ""

    methods_lower = {m.lower(): m for m in payment_methods}

    # Exact match first.
    if hint_lower in methods_lower:
        return methods_lower[hint_lower]

    # Known alias keywords, checked as substrings/prefixes of the hint.
    for keyword, canonical_name in _METHOD_ALIASES.items():
        if keyword in hint_lower:
            if canonical_name.lower() in methods_lower:
                return methods_lower[canonical_name.lower()]

    # Case-insensitive substring match against the configured names themselves.
    for lower_name, name in methods_lower.items():
        if lower_name in hint_lower or hint_lower.startswith(lower_name):
            return name

    return hint.strip()


def to_purchase_row(
    extract: ReceiptExtract,
    match: SupplierMatch,
    categories: Categories,
    cfg: Config,
    processed_on: datetime,
) -> PurchaseRow:
    """Status is "Review" if, and only if: match.is_new, category invalid/empty,
    extract.confidence < cfg.receipts.min_confidence (this also covers extraction failure, which
    reports confidence 0), extract.total is None (amount missing), or extract.raw["_problems"] is
    non-empty (validate()'s own findings: unreconciled amounts, non-USD currency, missing
    date/supplier/total). Model observations in extract.notes are never, by themselves, a reason to
    flag a row -- they still appear in the Notes column text for the human reviewer.
    company = match.canonical or extract.supplier_name. payment_method = normalised hint mapped onto
    data/payment_methods.csv names when obvious (e.g. 'VISA ****1234' -> 'Visa', 'MasterCard' ->
    'Mastercard'), else the hint verbatim, else ''."""
    notes = list(extract.notes)

    company = match.canonical or extract.supplier_name or ""

    category = ""
    if match.default_category and categories.is_valid(match.default_category):
        category = categories.canonical(match.default_category) or match.default_category
    schedule_c = categories.schedule_c_for(category) or "" if category else ""

    def money(value: Optional[Decimal]) -> Decimal:
        if value is None:
            notes.append("amount missing; defaulted to 0")
            return Decimal("0")
        return value

    total_missing = extract.total is None
    net = money(extract.net)
    sales_tax = money(extract.sales_tax)
    total = money(extract.total)
    tax_rate = extract.tax_rate if extract.tax_rate is not None else Decimal("0")

    payment_method = _normalise_payment_method(extract.payment_method_hint, cfg_payment_methods(cfg))

    row_date = extract.date
    if row_date is None:
        row_date = processed_on.date()
        notes.append("date defaulted to processing date")

    problems = extract.raw.get("_problems") if isinstance(extract.raw, dict) else None

    status = "OK"
    if (
        match.is_new
        or not category
        or extract.confidence < cfg.receipts.min_confidence
        or extract.confidence <= 0
        or total_missing
        or bool(problems)
    ):
        status = "Review"

    return PurchaseRow(
        date=row_date,
        company=company,
        category=category,
        schedule_c=schedule_c,
        net=net,
        sales_tax=sales_tax,
        total=total,
        tax_rate=tax_rate,
        payment_method=payment_method,
        notes="; ".join(notes),
        year=row_date.year,
        source_file=extract.source_file,
        processed_on=processed_on,
        status=status,
    )


def cfg_payment_methods(cfg: Config) -> list[str]:
    """Load the allowed payment method names from data/payment_methods.csv."""
    import csv

    path = cfg.paths.payment_methods
    if not path.exists():
        return []
    names = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("Payment method") or "").strip()
            if name:
                names.append(name)
    return names
