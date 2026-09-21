"""processing_log.csv: which files (by content hash) have already been handled.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from kosaccounts.models import LedgerEntry


class Ledger:
    """CSV-backed ledger. Loads on construction; `append` writes through immediately
    (one row per call, header written if the file is new) so a crash mid-run loses nothing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[LedgerEntry] = []
        self._hashes: set[str] = set()
        self._load()

    def _load(self) -> None:
        """Load existing entries from CSV, if file exists."""
        if not self.path.exists():
            return

        with self.path.open("r", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != LedgerEntry.COLUMNS:
                # Handle potential header mismatch gracefully
                pass
            for row in reader:
                if row:
                    entry = LedgerEntry(
                        filename=row["Filename"],
                        relative_path=row["Relative path"],
                        sha256=row["SHA256"],
                        size=int(row["Size"]),
                        date_processed=datetime.fromisoformat(row["Date processed"]),
                        stage=row["Stage"],  # type: ignore
                        status=row["Status"],  # type: ignore
                        rows_added=int(row["Rows added"]),
                        notes=row.get("Notes", ""),
                    )
                    self._entries.append(entry)
                    self._hashes.add(entry.sha256)

    def entries(self) -> list[LedgerEntry]:
        return self._entries.copy()

    def has_hash(self, sha256: str) -> bool:
        return sha256 in self._hashes

    def entries_for_path(self, relative_path: str) -> list[LedgerEntry]:
        """All ledger entries recorded for `relative_path`, in the order they were appended."""
        return [e for e in self._entries if e.relative_path == relative_path]

    def entries_for_canonical_path(self, relative_path: str) -> list[LedgerEntry]:
        """All ledger entries whose relative_path canonicalises (discovery.canonical_relative_path)
        to the same value as `relative_path`, in append order. Matches a Dropbox collision re-upload
        ("stem__<hash8>.ext") against the entry recorded for the original "stem.ext", and vice versa."""
        from kosaccounts.discovery import canonical_relative_path  # deferred: avoids a module cycle

        target = canonical_relative_path(relative_path)
        return [e for e in self._entries if canonical_relative_path(e.relative_path) == target]

    def append(self, entry: LedgerEntry) -> None:
        """Append entry to ledger and write to CSV immediately."""
        # Write header if file is new
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.path.exists()

        with self.path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LedgerEntry.COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "Filename": entry.filename,
                    "Relative path": entry.relative_path,
                    "SHA256": entry.sha256,
                    "Size": entry.size,
                    "Date processed": entry.date_processed.isoformat(),
                    "Stage": entry.stage,
                    "Status": entry.status,
                    "Rows added": entry.rows_added,
                    "Notes": entry.notes,
                }
            )

        # Update in-memory state
        self._entries.append(entry)
        self._hashes.add(entry.sha256)

    def missing_sources(self, imports_root: Path) -> list[LedgerEntry]:
        """Entries whose relative_path no longer exists under imports_root (report only)."""
        missing = []
        for entry in self._entries:
            full_path = imports_root / entry.relative_path
            if not full_path.exists():
                missing.append(entry)
        return missing
