"""Tests for ledger.py"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kosaccounts.ledger import Ledger
from kosaccounts.models import LedgerEntry


class TestLedger:
    """Tests for CSV-backed Ledger."""

    def test_append_then_reload_from_disk(self, tmp_path: Path) -> None:
        """Append entries, then reload from disk and verify they match."""
        ledger_path = tmp_path / "ledger.csv"

        # Create ledger and append an entry
        ledger1 = Ledger(ledger_path)
        entry1 = LedgerEntry(
            filename="receipt.pdf",
            relative_path="receipts/2026-09-01 - Mood.pdf",
            sha256="abc123def456",
            size=1024,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
            rows_added=1,
            notes="",
        )
        ledger1.append(entry1)

        # Reload from disk and verify
        ledger2 = Ledger(ledger_path)
        entries = ledger2.entries()
        assert len(entries) == 1
        assert entries[0].filename == "receipt.pdf"
        assert entries[0].sha256 == "abc123def456"
        assert entries[0].rows_added == 1

    def test_has_hash_true(self, tmp_path: Path) -> None:
        """Test has_hash returns True for entries in the ledger."""
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)

        entry = LedgerEntry(
            filename="receipt.pdf",
            relative_path="receipts/2026-09-01 - Mood.pdf",
            sha256="abc123def456",
            size=1024,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry)

        assert ledger.has_hash("abc123def456") is True

    def test_has_hash_false(self, tmp_path: Path) -> None:
        """Test has_hash returns False for entries not in the ledger."""
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)

        assert ledger.has_hash("notinhashset") is False

    def test_header_only_written_once(self, tmp_path: Path) -> None:
        """Header should only be written once, even across multiple appends."""
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)

        # First append writes header + row
        entry1 = LedgerEntry(
            filename="receipt1.pdf",
            relative_path="receipts/2026-09-01 - Mood.pdf",
            sha256="abc123",
            size=1024,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry1)

        # Read file content to check header
        content_after_first = ledger_path.read_text()
        header_count_after_first = content_after_first.count("Filename")
        assert header_count_after_first == 1, "Header should be written once"

        # Second append writes only row, not header
        entry2 = LedgerEntry(
            filename="receipt2.pdf",
            relative_path="receipts/2026-09-02 - Mood.pdf",
            sha256="def456",
            size=2048,
            date_processed=datetime.fromisoformat("2026-09-18T11:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry2)

        # Check header count is still 1
        content_after_second = ledger_path.read_text()
        header_count_after_second = content_after_second.count("Filename")
        assert header_count_after_second == 1, "Header should still be written only once"

    def test_missing_sources_finds_deleted_file(self, tmp_path: Path) -> None:
        """missing_sources should find entries whose file no longer exists."""
        ledger_path = tmp_path / "ledger.csv"
        imports_root = tmp_path / "imports"
        imports_root.mkdir(parents=True)

        # Create a ledger with an entry
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="deleted.pdf",
            relative_path="receipts/deleted.pdf",
            sha256="abc123",
            size=1024,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry)

        # File doesn't exist, so missing_sources should return it
        missing = ledger.missing_sources(imports_root)
        assert len(missing) == 1
        assert missing[0].filename == "deleted.pdf"

    def test_missing_sources_ignores_existing_file(self, tmp_path: Path) -> None:
        """missing_sources should not return entries for existing files."""
        ledger_path = tmp_path / "ledger.csv"
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create a file
        test_file = receipts_dir / "test.pdf"
        test_file.write_text("test content")

        # Create a ledger entry for it
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="test.pdf",
            relative_path="receipts/test.pdf",
            sha256="abc123",
            size=12,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry)

        # missing_sources should return empty list
        missing = ledger.missing_sources(imports_root)
        assert len(missing) == 0


def test_append_creates_missing_data_directory(tmp_path):
    from datetime import datetime
    from kosaccounts.ledger import Ledger
    from kosaccounts.models import LedgerEntry

    path = tmp_path / "data" / "processing_log.csv"
    ledger = Ledger(path)
    ledger.append(
        LedgerEntry("a.pdf", "receipts/a.pdf", "ab" * 32, 10, datetime(2026, 9, 18, 12, 0), "receipt", "Processed", 1, "")
    )
    assert path.exists()
    assert Ledger(path).has_hash("ab" * 32)
