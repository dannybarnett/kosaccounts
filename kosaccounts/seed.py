"""Build data/*.csv from the master workbook (plan §3).

Master: sheet "Purchases", Excel table "KosibahPurchases" (header row 3, columns B..M:
Date, Company, Category, Schedule C, Net, Sales Tax, Total, Tax rate, Payment method, Check, Notes, Year;
last row is a "Total" row to skip). Sheet "Lookup values" has table "ScheduleC" at F2:G31
(Original -> Schedule C). Expected on 2026-09-18: 1438 data rows, 236 suppliers, 30 categories.

Outputs (columns exactly as suppliers.py / categories.py expect):
- suppliers.csv: Supplier, Default category (most frequent; ties -> most recent), Other categories,
  Count, Last date
- categories.csv: Category, Schedule C
- payment_methods.csv: Payment method, Count   (strip cheque numbers: "Cheque 0104" -> "Cheque")
- supplier_aliases.csv: seeded with case/punctuation collisions (normalise() equal but spelling
  differs; the more frequent spelling is canonical). MERGE with an existing aliases file, never drop
  hand-added rows.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import openpyxl

PURCHASES_SHEET = "Purchases"
LOOKUP_SHEET = "Lookup values"

# Purchases sheet: header row 3, data starts row 4. Columns B..M -> 0-based tuple indices
# when reading a full row with values_only=True (column A is index 0).
COL_DATE = 1
COL_COMPANY = 2
COL_CATEGORY = 3
COL_PAYMENT_METHOD = 9

# Lookup values sheet: "Original" / "Schedule C" table in columns F, G.
LOOKUP_COL_ORIGINAL = 6  # 1-based column index (F)
LOOKUP_COL_SCHEDULE_C = 7  # 1-based column index (G)

_CHEQUE_NUMBER_RE = re.compile(r"^(cheque)\s+\d+$", re.IGNORECASE)


@dataclass
class SeedStats:
    purchase_rows: int
    suppliers: int
    categories: int
    payment_methods: int
    aliases_added: int


def _normalise(name: str) -> str:
    """Lowercase; map runs of non-alphanumeric characters to a single space; trim.
    Used only to detect spelling collisions between otherwise-identical supplier names
    (e.g. "T Mobile" / "T-Mobile"). Deliberately simpler than suppliers.normalise()."""
    lowered = name.lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return collapsed.strip()


def _clean_payment_method(raw: str) -> str:
    """Strip a trailing cheque number: "Cheque 0104" -> "Cheque"."""
    stripped = raw.strip()
    match = _CHEQUE_NUMBER_RE.match(stripped)
    if match:
        return match.group(1).capitalize()
    return stripped


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _read_categories(master_path: Path) -> list[tuple[str, str]]:
    """Read the Original -> Schedule C table from the "Lookup values" sheet (columns F, G),
    starting at the header row and continuing until a blank Original cell. File order preserved."""
    wb = openpyxl.load_workbook(master_path, data_only=True, read_only=True)
    try:
        ws = wb[LOOKUP_SHEET]
        header_row = None
        for r, row in enumerate(
            ws.iter_rows(min_row=1, max_row=ws.max_row or 1, values_only=True), start=1
        ):
            original = row[LOOKUP_COL_ORIGINAL - 1] if len(row) >= LOOKUP_COL_ORIGINAL else None
            schedule_c = row[LOOKUP_COL_SCHEDULE_C - 1] if len(row) >= LOOKUP_COL_SCHEDULE_C else None
            if (
                isinstance(original, str)
                and original.strip().lower() == "original"
                and isinstance(schedule_c, str)
                and schedule_c.strip().lower() == "schedule c"
            ):
                header_row = r
                break
        if header_row is None:
            raise ValueError(f'"{LOOKUP_SHEET}" sheet: could not find "Original" / "Schedule C" header')

        categories: list[tuple[str, str]] = []
        for r in range(header_row + 1, (ws.max_row or header_row) + 1):
            original = ws.cell(row=r, column=LOOKUP_COL_ORIGINAL).value
            schedule_c = ws.cell(row=r, column=LOOKUP_COL_SCHEDULE_C).value
            if original is None or str(original).strip() == "":
                break
            categories.append((str(original).strip(), str(schedule_c).strip() if schedule_c else ""))
        return categories
    finally:
        wb.close()


@dataclass
class _SupplierAgg:
    category_counts: Counter
    count: int = 0
    last_date: Optional[date] = None
    last_category: Optional[str] = None
    first_seen_index: int = 0

    def __init__(self, first_seen_index: int) -> None:
        self.category_counts = Counter()
        self.count = 0
        self.last_date = None
        self.last_category = None
        self.first_seen_index = first_seen_index

    def add(self, category: str, row_date: Optional[date]) -> None:
        self.count += 1
        if category:
            self.category_counts[category] += 1
        if row_date is not None and (self.last_date is None or row_date >= self.last_date):
            self.last_date = row_date
            self.last_category = category
        elif self.last_date is None and row_date is None:
            self.last_category = self.last_category or category

    def default_category(self) -> str:
        if not self.category_counts:
            return ""
        max_count = max(self.category_counts.values())
        tied = [c for c, n in self.category_counts.items() if n == max_count]
        if len(tied) == 1:
            return tied[0]
        if self.last_category in tied:
            return self.last_category
        return sorted(tied)[0]

    def other_categories(self) -> list[str]:
        default = self.default_category()
        remaining = [c for c in self.category_counts if c != default]
        remaining.sort(key=lambda c: (-self.category_counts[c], c))
        return remaining


def _read_purchases(master_path: Path) -> tuple[list[tuple], int]:
    """Read data rows from the Purchases sheet, stopping at (and excluding) the "Total" row.
    Returns (rows, purchase_row_count) where each row is the raw values_only tuple."""
    wb = openpyxl.load_workbook(master_path, data_only=True, read_only=True)
    try:
        ws = wb[PURCHASES_SHEET]
        # Find the header row (has "Date" in the Date column) so this isn't dependent on a
        # hardcoded row number.
        header_row = None
        for r, row in enumerate(
            ws.iter_rows(min_row=1, max_row=min(ws.max_row or 10, 10), values_only=True), start=1
        ):
            date_cell = row[COL_DATE] if len(row) > COL_DATE else None
            company_cell = row[COL_COMPANY] if len(row) > COL_COMPANY else None
            if (
                isinstance(date_cell, str)
                and date_cell.strip().lower() == "date"
                and isinstance(company_cell, str)
                and company_cell.strip().lower() == "company"
            ):
                header_row = r
                break
        if header_row is None:
            raise ValueError(f'"{PURCHASES_SHEET}" sheet: could not find header row')

        rows: list[tuple] = []
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            b = row[COL_DATE] if len(row) > COL_DATE else None
            c = row[COL_COMPANY] if len(row) > COL_COMPANY else None
            if b is None and c is None:
                continue
            if isinstance(b, str) and b.strip().lower() == "total":
                break
            if isinstance(c, str) and c.strip().lower() == "total" and b is None:
                break
            rows.append(row)
        return rows, len(rows)
    finally:
        wb.close()


def _write_csv_atomic(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    os.replace(tmp_path, path)


def _read_existing_aliases(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [
            (row.get("Alias", "").strip(), row.get("Supplier", "").strip())
            for row in reader
            if row.get("Alias", "").strip()
        ]


def seed_from_master(master: Path, data_dir: Path) -> SeedStats:
    master = Path(master)
    data_dir = Path(data_dir)

    # --- categories.csv ---
    categories = _read_categories(master)
    _write_csv_atomic(
        data_dir / "categories.csv",
        ["Category", "Schedule C"],
        [[name, schedule_c] for name, schedule_c in categories],
    )

    # --- purchases + suppliers + payment methods ---
    raw_rows, purchase_row_count = _read_purchases(master)

    suppliers: dict[str, _SupplierAgg] = {}
    payment_counts: Counter = Counter()

    for idx, row in enumerate(raw_rows):
        company_raw = row[COL_COMPANY]
        company = str(company_raw).strip() if company_raw is not None else ""
        category_raw = row[COL_CATEGORY]
        category = str(category_raw).strip() if category_raw is not None else ""
        row_date = _as_date(row[COL_DATE])
        payment_raw = row[COL_PAYMENT_METHOD]

        if company:
            agg = suppliers.get(company)
            if agg is None:
                agg = _SupplierAgg(first_seen_index=idx)
                suppliers[company] = agg
            agg.add(category, row_date)

        if payment_raw is not None and str(payment_raw).strip():
            payment_counts[_clean_payment_method(str(payment_raw))] += 1

    # --- detect and fold spelling collisions ---
    groups: dict[str, list[str]] = defaultdict(list)
    for name in suppliers:
        groups[_normalise(name)].append(name)

    detected_aliases: list[tuple[str, str]] = []  # (alias, canonical)
    for names in groups.values():
        if len(names) < 2:
            continue
        # Canonical = highest count; ties broken by earliest first-seen row.
        names_sorted = sorted(
            names,
            key=lambda n: (-suppliers[n].count, suppliers[n].first_seen_index),
        )
        canonical = names_sorted[0]
        canonical_agg = suppliers[canonical]
        for alias_name in names_sorted[1:]:
            alias_agg = suppliers.pop(alias_name)
            canonical_agg.count += alias_agg.count
            canonical_agg.category_counts.update(alias_agg.category_counts)
            if alias_agg.last_date is not None and (
                canonical_agg.last_date is None or alias_agg.last_date >= canonical_agg.last_date
            ):
                canonical_agg.last_date = alias_agg.last_date
                canonical_agg.last_category = alias_agg.last_category
            detected_aliases.append((alias_name, canonical))

    # --- suppliers.csv ---
    supplier_rows = []
    for name in sorted(suppliers.keys(), key=lambda n: n.lower()):
        agg = suppliers[name]
        default_category = agg.default_category()
        other = agg.other_categories()
        last_date_str = agg.last_date.strftime("%Y-%m-%d") if agg.last_date else ""
        supplier_rows.append([name, default_category, ";".join(other), agg.count, last_date_str])

    _write_csv_atomic(
        data_dir / "suppliers.csv",
        ["Supplier", "Default category", "Other categories", "Count", "Last date"],
        supplier_rows,
    )

    # --- payment_methods.csv ---
    payment_rows = [[method, count] for method, count in sorted(payment_counts.items())]
    _write_csv_atomic(
        data_dir / "payment_methods.csv",
        ["Payment method", "Count"],
        payment_rows,
    )

    # --- supplier_aliases.csv (merge, never drop hand-added rows) ---
    aliases_path = data_dir / "supplier_aliases.csv"
    existing_aliases = _read_existing_aliases(aliases_path)
    existing_keys = {alias for alias, _ in existing_aliases}

    merged_aliases = list(existing_aliases)
    aliases_added = 0
    for alias, canonical in detected_aliases:
        if alias in existing_keys:
            continue
        merged_aliases.append((alias, canonical))
        existing_keys.add(alias)
        aliases_added += 1

    _write_csv_atomic(
        aliases_path,
        ["Alias", "Supplier"],
        [[alias, canonical] for alias, canonical in merged_aliases],
    )

    return SeedStats(
        purchase_rows=purchase_row_count,
        suppliers=len(suppliers),
        categories=len(categories),
        payment_methods=len(payment_rows),
        aliases_added=aliases_added,
    )
