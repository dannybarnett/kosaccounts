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
    stages: list[str] = field(default_factory=lambda: ["expense", "bank"])


@dataclass
class ExpensesConfig:
    min_confidence: float = 0.75
    max_image_px: int = 2000
    default_currency: str = "USD"
    amount_tolerance: str = "0.01"
    filename_strip: list[str] = field(default_factory=list)  # uploader names etc. removed from hints
    payment_method_map: dict[str, str] = field(default_factory=dict)  # receipt hint substring -> name


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
    statement_payment_method: str = "Card (statement)"  # for bank-only rows / receipts with no hint


@dataclass
class ClaudeConfig:
    command: str = "claude"
    model: str = "sonnet"
    timeout_seconds: int = 120
    max_turns: int = 4
    retries: int = 1
    allowed_tools: list[str] = field(default_factory=lambda: ["Read"])


@dataclass
class IntakeConfig:
    """Event-driven intake (specs/README_server_intake.md): Dropbox listener + import watcher.

    kos_root is the DATA root only (imports/, .staging/, state/ live there); code, config,
    data/, output/ and logs/ stay in the repo. Absolute path expected; a relative one is
    resolved against the repo root.
    """

    kos_root: Path = Path("/mnt/storage/docs/kosaccounts")
    folders: list[str] = field(default_factory=lambda: ["expenses", "bank"])  # Dropbox /<f> -> imports/<f>
    longpoll_timeout_seconds: int = 480
    settle_poll_seconds: int = 60
    settle_stable_polls: int = 2
    settle_max_wait_minutes: int = 60
    inotify_quiet_seconds: int = 120
    reconcile_interval_minutes: int = 60
    # folder -> command line (shlex-split; a relative argv[0] is resolved against the repo root).
    # The watcher appends the absolute imports/<folder> path as the last argument.
    process_commands: dict[str, str] = field(
        default_factory=lambda: {
            "expenses": "scripts/run_pipeline.sh --stage expense",
            "bank": "scripts/run_pipeline.sh --stage bank",
        }
    )

    @property
    def imports_dir(self) -> Path:
        return self.kos_root / "imports"

    @property
    def staging_dir(self) -> Path:
        return self.kos_root / ".staging"

    @property
    def state_dir(self) -> Path:
        return self.kos_root / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "intake.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "intake.lock"

    def import_folder(self, folder: str) -> Path:
        return self.imports_dir / folder

    def staging_folder(self, folder: str) -> Path:
        return self.staging_dir / folder


@dataclass
class Config:
    paths: PathsConfig
    processing: ProcessingConfig
    expenses: ExpensesConfig
    suppliers: SuppliersConfig
    bank: BankConfig
    claude: ClaudeConfig
    intake: IntakeConfig


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
        expenses=ExpensesConfig(**raw.get("expenses", {})),
        suppliers=SuppliersConfig(**raw.get("suppliers", {})),
        bank=BankConfig(**raw.get("bank", {})),
        claude=ClaudeConfig(**raw.get("claude", {})),
        intake=_load_intake(raw.get("intake", {}), root),
    )


def _load_intake(raw: dict, root: Path) -> IntakeConfig:
    raw = dict(raw)
    if "kos_root" in raw:
        raw["kos_root"] = (root / Path(raw["kos_root"]).expanduser()).resolve()
    return IntakeConfig(**raw)
