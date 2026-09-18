"""Load config.toml into a typed Config. Paths are resolved relative to the repo root
(the directory containing config.toml) so cron can run from anywhere."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PathsConfig:
    root: Path
    imports: Path
    output_workbook: Path
    data: Path
    logs: Path
    master_workbook: Path

    # derived data files
    @property
    def ledger(self) -> Path:
        return self.data / "processing_log.csv"

    @property
    def suppliers(self) -> Path:
        return self.data / "suppliers.csv"

    @property
    def aliases(self) -> Path:
        return self.data / "supplier_aliases.csv"

    @property
    def categories(self) -> Path:
        return self.data / "categories.csv"

    @property
    def payment_methods(self) -> Path:
        return self.data / "payment_methods.csv"

    @property
    def pending_suppliers(self) -> Path:
        return self.data / "new_suppliers_pending.csv"


@dataclass
class ProcessingConfig:
    block_on_flags: bool = False
    stages: list[str] = field(default_factory=lambda: ["receipt", "bank"])


@dataclass
class ReceiptsConfig:
    min_confidence: float = 0.75
    max_image_px: int = 2000
    default_currency: str = "USD"
    amount_tolerance: str = "0.01"


@dataclass
class SuppliersConfig:
    auto_match_threshold: int = 92
    model_review_threshold: int = 70
    legal_suffixes: list[str] = field(
        default_factory=lambda: ["inc", "llc", "ltd", "limited", "corp", "co", "company", "plc"]
    )


@dataclass
class BankConfig:
    match_window_days: int = 5
    ignore_descriptions: list[str] = field(default_factory=list)


@dataclass
class ClaudeConfig:
    command: str = "claude"
    model: str = "sonnet"
    timeout_seconds: int = 120
    max_turns: int = 4
    retries: int = 1
    allowed_tools: list[str] = field(default_factory=lambda: ["Read"])


@dataclass
class Config:
    paths: PathsConfig
    processing: ProcessingConfig
    receipts: ReceiptsConfig
    suppliers: SuppliersConfig
    bank: BankConfig
    claude: ClaudeConfig


def find_config(start: Optional[Path] = None) -> Path:
    """Walk up from `start` (default cwd) to find config.toml; fall back to the package's parent."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        p = candidate / "config.toml"
        if p.is_file():
            return p
    fallback = Path(__file__).resolve().parent.parent / "config.toml"
    if fallback.is_file():
        return fallback
    raise FileNotFoundError("config.toml not found")


def load_config(path: Optional[Path] = None) -> Config:
    cfg_path = Path(path) if path else find_config()
    root = cfg_path.resolve().parent
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    def p(key: str, default: str) -> Path:
        return (root / raw.get("paths", {}).get(key, default)).resolve()

    paths = PathsConfig(
        root=root,
        imports=p("imports", "imports"),
        output_workbook=p("output_workbook", "output/kosibah_import.xlsx"),
        data=p("data", "data"),
        logs=p("logs", "logs"),
        master_workbook=p("master_workbook", "specs/Kosibah LLC purchases and receipts.xlsx"),
    )
    return Config(
        paths=paths,
        processing=ProcessingConfig(**raw.get("processing", {})),
        receipts=ReceiptsConfig(**raw.get("receipts", {})),
        suppliers=SuppliersConfig(**raw.get("suppliers", {})),
        bank=BankConfig(**raw.get("bank", {})),
        claude=ClaudeConfig(**raw.get("claude", {})),
    )
