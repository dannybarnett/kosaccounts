"""Tests for discovery.py"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kosaccounts.discovery import discover, new_files, sha256_file
from kosaccounts.ledger import Ledger
from kosaccounts.models import LedgerEntry


class TestSha256File:
    """Tests for sha256_file function."""

    def test_sha256_file_computes_hash(self, tmp_path: Path) -> None:
        """sha256_file should compute a consistent hash."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        hash1 = sha256_file(test_file)
        hash2 = sha256_file(test_file)

        # Same file should produce same hash
        assert hash1 == hash2
        # Should be a hex string of 64 characters (SHA256 is 32 bytes)
        assert len(hash1) == 64
        assert all(c in "0123456789abcdef" for c in hash1)


class TestDiscover:
    """Tests for discover function."""

    def test_discover_files_in_receipts_and_bank(self, tmp_path: Path) -> None:
        """discover should find files in receipts and bank folders."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        bank_dir = imports_root / "bank"
        receipts_dir.mkdir(parents=True)
        bank_dir.mkdir(parents=True)

        # Create test files
        receipt_file = receipts_dir / "receipt.pdf"
        receipt_file.write_text("receipt content")
        bank_file = bank_dir / "statement.html"
        bank_file.write_text("bank content")

        discovered = discover(imports_root)
        assert len(discovered) == 2

        # Check stages are correct
        receipt_discovered = next(f for f in discovered if f.filename == "receipt.pdf")
        bank_discovered = next(f for f in discovered if f.filename == "statement.html")

        assert receipt_discovered.stage == "receipt"
        assert bank_discovered.stage == "bank"

    def test_discover_ignores_invoices_folder(self, tmp_path: Path) -> None:
        """discover should ignore files in invoices folder."""
        imports_root = tmp_path / "imports"
        invoices_dir = imports_root / "invoices"
        invoices_dir.mkdir(parents=True)

        # Create a file in invoices
        invoice_file = invoices_dir / "invoice.pdf"
        invoice_file.write_text("invoice content")

        discovered = discover(imports_root)
        assert len(discovered) == 0

    def test_discover_ignores_hidden_files(self, tmp_path: Path) -> None:
        """discover should ignore hidden files."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create a hidden file and a normal file
        hidden_file = receipts_dir / ".hidden"
        hidden_file.write_text("hidden")
        normal_file = receipts_dir / "normal.pdf"
        normal_file.write_text("normal")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_ignores_zero_byte_files(self, tmp_path: Path) -> None:
        """discover should ignore zero-byte files."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create a zero-byte file and a normal file
        zero_file = receipts_dir / "zero.pdf"
        zero_file.write_text("")
        normal_file = receipts_dir / "normal.pdf"
        normal_file.write_text("content")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_ignores_partial_files(self, tmp_path: Path) -> None:
        """discover should ignore *.partial and .~tmp~* files."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create files to be ignored and a normal file
        partial_file = receipts_dir / "upload.pdf.partial"
        partial_file.write_text("partial")
        tmp_file = receipts_dir / ".~tmp~file"
        tmp_file.write_text("tmp")
        normal_file = receipts_dir / "normal.pdf"
        normal_file.write_text("normal")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_sorted_by_relative_path(self, tmp_path: Path) -> None:
        """discover should return files sorted by relative_path."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create files in non-alphabetical order
        for name in ["zebra.pdf", "apple.pdf", "banana.pdf"]:
            (receipts_dir / name).write_text(name)

        discovered = discover(imports_root)
        assert len(discovered) == 3
        # Should be sorted by relative_path (all in receipts/)
        assert discovered[0].filename == "apple.pdf"
        assert discovered[1].filename == "banana.pdf"
        assert discovered[2].filename == "zebra.pdf"

    def test_discover_posix_separators(self, tmp_path: Path) -> None:
        """discover should use POSIX separators in relative_path."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        subdir = receipts_dir / "subfolder"
        subdir.mkdir(parents=True)

        test_file = subdir / "test.pdf"
        test_file.write_text("content")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        # On all platforms, should use forward slashes
        assert discovered[0].relative_path == "receipts/subfolder/test.pdf"


class TestNewFiles:
    """Tests for new_files function."""

    def test_renamed_copy_with_same_hash_ignored(self, tmp_path: Path) -> None:
        """A renamed copy of an already-ledgered file should not be returned by new_files."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create a file
        original_file = receipts_dir / "original.pdf"
        original_file.write_text("content")

        # Get its hash
        file_hash = sha256_file(original_file)

        # Create ledger and add an entry with this hash
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="original.pdf",
            relative_path="receipts/original.pdf",
            sha256=file_hash,
            size=original_file.stat().st_size,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry)

        # Create a renamed copy
        renamed_file = receipts_dir / "renamed.pdf"
        renamed_file.write_text("content")  # Same content

        # Discover files
        discovered = discover(imports_root)
        assert len(discovered) == 2

        # new_files should only return files not in ledger
        new = new_files(discovered, ledger)
        assert len(new) == 0, "Renamed copy with same hash should not be in new_files"

    def test_edited_copy_with_different_hash_returned(self, tmp_path: Path) -> None:
        """An edited copy with the same name but different hash should be returned."""
        imports_root = tmp_path / "imports"
        receipts_dir = imports_root / "receipts"
        receipts_dir.mkdir(parents=True)

        # Create a file
        original_file = receipts_dir / "document.pdf"
        original_file.write_text("content v1")

        # Get its hash
        original_hash = sha256_file(original_file)

        # Create ledger with this hash
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="document.pdf",
            relative_path="receipts/document.pdf",
            sha256=original_hash,
            size=original_file.stat().st_size,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="receipt",
            status="Processed",
        )
        ledger.append(entry)

        # Edit the file with different content
        original_file.write_text("content v2 - modified")

        # Discover files
        discovered = discover(imports_root)
        assert len(discovered) == 1

        # new_files should return the edited file since hash changed
        new = new_files(discovered, ledger)
        assert len(new) == 1
        assert new[0].filename == "document.pdf"
        assert new[0].sha256 != original_hash, "Hash should be different after edit"


def test_files_inside_hidden_directories_are_ignored(tmp_repo):
    """receipts/.converted/*.jpg is a cache written by receipt extraction, not a new receipt."""
    from kosaccounts.discovery import discover

    receipts = tmp_repo / "imports" / "receipts"
    (receipts / "2026-09-01 - Mood.png").write_bytes(b"png-bytes")
    cache = receipts / ".converted"
    cache.mkdir()
    (cache / "2026-09-01 - Mood.jpg").write_bytes(b"jpg-bytes")

    found = discover(tmp_repo / "imports")
    assert [f.relative_path for f in found] == ["receipts/2026-09-01 - Mood.png"]
