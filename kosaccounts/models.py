"""Shared data contract for the pipeline.

Every module imports these and nothing else defines cross-module shapes.
Do NOT change this file inside a subagent task; report back if it is insufficient.

Conventions:
- Money is Decimal, quantised to 2 dp when written to the workbook.
- tax_rate is a percentage (Decimal("8.875")), matching the master workbook's "Tax rate" column.
- Dates are datetime.date; timestamps are timezone-naive local datetime.
- source_file is the path relative to the imports/ root, e.g. "receipts/2026-09-01 - Mood.pdf".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal, Optional

Stage = Literal["receipt", "bank"]
"""Which pipeline stage a file belongs to; derived from its imports/ subfolder."""

RowStatus = Literal["OK", "Review"]
LedgerStatus = Literal["Processed", "Flagged", "Error", "Skipped"]
MatchMethod = Literal["alias", "exact", "fuzzy", "model", "new"]
Direction = Literal["debit", "credit"]


@dataclass
class DiscoveredFile:
    """A file found under imports/ during a run."""

    path: Path  # absolute
    relative_path: str  # relative to imports/, POSIX separators
    stage: Stage
    sha256: str
    size: int

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class LedgerEntry:
    """One row of data/processing_log.csv."""

    filename: str
    relative_path: str
    sha256: str
    size: int
    date_processed: datetime
    stage: Stage
    status: LedgerStatus
    rows_added: int = 0
    notes: str = ""

    COLUMNS: ClassVar[list[str]] = [
        "Filename",
        "Relative path",
        "SHA256",
        "Size",
        "Date processed",
        "Stage",
        "Status",
        "Rows added",
        "Notes",
    ]


@dataclass
class ReceiptExtract:
    """Fields pulled from one receipt (by the model, or by pdfplumber + filename hint)."""

    source_file: str
    date: Optional[date] = None
    supplier_name: Optional[str] = None
    total: Optional[Decimal] = None
    sales_tax: Optional[Decimal] = None
    net: Optional[Decimal] = None
    tax_rate: Optional[Decimal] = None  # percent
    currency: Optional[str] = None  # ISO 4217, e.g. "USD"
    payment_method_hint: Optional[str] = None  # free text from receipt, e.g. "VISA ****1234"
    line_summary: Optional[str] = None  # one line describing what was bought
    confidence: float = 0.0  # 0..1 as reported by the model (1.0 for deterministic paths)
    notes: list[str] = field(default_factory=list)  # validation messages, non-USD warnings, etc.
    raw: dict = field(default_factory=dict)  # the model's JSON, untouched


@dataclass
class SupplierMatch:
    """Result of resolving a raw supplier string to a canonical supplier."""

    input_name: str
    canonical: Optional[str]  # None when is_new
    default_category: Optional[str]  # from suppliers.csv, or model suggestion for a new supplier
    score: float  # 0..100 (rapidfuzz scale); 100 for alias/exact
    method: MatchMethod
    is_new: bool = False


@dataclass
class BankTxn:
    """One transaction parsed from a bank statement."""

    date: date
    description: str
    amount: Decimal  # positive magnitude; sign carried by direction
    direction: Direction
    source_file: str
    ref: str  # stable id: bank reference if present, else f"{source_file}#{index}"
    balance: Optional[Decimal] = None
    statement_start: Optional[date] = None
    statement_end: Optional[date] = None


@dataclass
class PurchaseRow:
    """One row of the output workbook. Column order is COLUMNS; the Check column is a formula
    written by workbook.py and is not a field here."""

    date: date
    company: str
    category: str
    schedule_c: str
    net: Decimal
    sales_tax: Decimal
    total: Decimal
    tax_rate: Decimal
    payment_method: str
    notes: str
    year: int
    source_file: str
    processed_on: datetime
    status: RowStatus = "OK"
    bank_ref: str = ""
    sheet_row: Optional[int] = None  # 1-based worksheet row; set when read back from the workbook

    COLUMNS: ClassVar[list[str]] = [
        "Date",
        "Company",
        "Category",
        "Schedule C",
        "Net",
        "Sales Tax",
        "Total",
        "Tax rate",
        "Payment method",
        "Check",
        "Notes",
        "Year",
        "Source file",
        "Processed on",
        "Status",
        "Bank ref",
    ]
    TABLE_NAME: ClassVar[str] = "KosibahImport"
    SHEET_NAME: ClassVar[str] = "Purchases"


@dataclass
class ReconcileResult:
    """Output of reconcile.reconcile()."""

    new_rows: list[PurchaseRow] = field(default_factory=list)  # bank debits with no receipt
    updated_rows: list[PurchaseRow] = field(default_factory=list)  # existing rows given a bank_ref / note
    unmatched_receipts: list[PurchaseRow] = field(default_factory=list)  # inside a statement period, no txn
    unmatched_txns: list[BankTxn] = field(default_factory=list)  # debits that became new_rows
    ignored_txns: list[BankTxn] = field(default_factory=list)  # credits / ignore-list hits


@dataclass
class RunSummary:
    """What one `run` did; written by summary.py."""

    started: datetime
    finished: Optional[datetime] = None
    dry_run: bool = False
    files_found: dict[str, int] = field(default_factory=dict)  # stage -> count
    files_new: dict[str, int] = field(default_factory=dict)
    files_flagged: dict[str, int] = field(default_factory=dict)
    files_errored: dict[str, int] = field(default_factory=dict)
    rows_added: dict[str, int] = field(default_factory=dict)  # stage -> rows appended
    rows_review: int = 0
    new_suppliers: list[str] = field(default_factory=list)
    unmatched_bank_txns: int = 0
    unmatched_receipts: int = 0
    errors: list[str] = field(default_factory=list)  # "relative_path: message"

    @property
    def nothing_to_do(self) -> bool:
        return sum(self.files_new.values()) == 0
