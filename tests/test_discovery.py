"""Tests for discovery.py"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kosaccounts.discovery import canonical_relative_path, discover, new_files, sha256_file
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

    def test_discover_files_in_expenses_and_bank(self, tmp_path: Path) -> None:
        """discover should find files in expenses and bank folders."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        bank_dir = imports_root / "bank"
        expenses_dir.mkdir(parents=True)
        bank_dir.mkdir(parents=True)

        # Create test files
        expense_file = expenses_dir / "expense.pdf"
        expense_file.write_text("expense content")
        bank_file = bank_dir / "statement.html"
        bank_file.write_text("bank content")

        discovered = discover(imports_root)
        assert len(discovered) == 2

        # Check stages are correct
        expense_discovered = next(f for f in discovered if f.filename == "expense.pdf")
        bank_discovered = next(f for f in discovered if f.filename == "statement.html")

        assert expense_discovered.stage == "expense"
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
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create a hidden file and a normal file
        hidden_file = expenses_dir / ".hidden"
        hidden_file.write_text("hidden")
        normal_file = expenses_dir / "normal.pdf"
        normal_file.write_text("normal")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_ignores_zero_byte_files(self, tmp_path: Path) -> None:
        """discover should ignore zero-byte files."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create a zero-byte file and a normal file
        zero_file = expenses_dir / "zero.pdf"
        zero_file.write_text("")
        normal_file = expenses_dir / "normal.pdf"
        normal_file.write_text("content")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_ignores_partial_files(self, tmp_path: Path) -> None:
        """discover should ignore *.partial and .~tmp~* files."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create files to be ignored and a normal file
        partial_file = expenses_dir / "upload.pdf.partial"
        partial_file.write_text("partial")
        tmp_file = expenses_dir / ".~tmp~file"
        tmp_file.write_text("tmp")
        normal_file = expenses_dir / "normal.pdf"
        normal_file.write_text("normal")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        assert discovered[0].filename == "normal.pdf"

    def test_discover_sorted_by_relative_path(self, tmp_path: Path) -> None:
        """discover should return files sorted by relative_path."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create files in non-alphabetical order
        for name in ["zebra.pdf", "apple.pdf", "banana.pdf"]:
            (expenses_dir / name).write_text(name)

        discovered = discover(imports_root)
        assert len(discovered) == 3
        # Should be sorted by relative_path (all in expenses/)
        assert discovered[0].filename == "apple.pdf"
        assert discovered[1].filename == "banana.pdf"
        assert discovered[2].filename == "zebra.pdf"

    def test_discover_posix_separators(self, tmp_path: Path) -> None:
        """discover should use POSIX separators in relative_path."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        subdir = expenses_dir / "subfolder"
        subdir.mkdir(parents=True)

        test_file = subdir / "test.pdf"
        test_file.write_text("content")

        discovered = discover(imports_root)
        assert len(discovered) == 1
        # On all platforms, should use forward slashes
        assert discovered[0].relative_path == "expenses/subfolder/test.pdf"


class TestNewFiles:
    """Tests for new_files function."""

    def test_renamed_copy_with_same_hash_ignored(self, tmp_path: Path) -> None:
        """A renamed copy of an already-ledgered file should not be returned by new_files."""
        imports_root = tmp_path / "imports"
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create a file
        original_file = expenses_dir / "original.pdf"
        original_file.write_text("content")

        # Get its hash
        file_hash = sha256_file(original_file)

        # Create ledger and add an entry with this hash
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="original.pdf",
            relative_path="expenses/original.pdf",
            sha256=file_hash,
            size=original_file.stat().st_size,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="expense",
            status="Processed",
        )
        ledger.append(entry)

        # Create a renamed copy
        renamed_file = expenses_dir / "renamed.pdf"
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
        expenses_dir = imports_root / "expenses"
        expenses_dir.mkdir(parents=True)

        # Create a file
        original_file = expenses_dir / "document.pdf"
        original_file.write_text("content v1")

        # Get its hash
        original_hash = sha256_file(original_file)

        # Create ledger with this hash
        ledger_path = tmp_path / "ledger.csv"
        ledger = Ledger(ledger_path)
        entry = LedgerEntry(
            filename="document.pdf",
            relative_path="expenses/document.pdf",
            sha256=original_hash,
            size=original_file.stat().st_size,
            date_processed=datetime.fromisoformat("2026-09-18T10:30:00"),
            stage="expense",
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
    """expenses/.converted/*.jpg is a cache written by expense extraction, not a new expense."""
    from kosaccounts.discovery import discover

    expenses = tmp_repo / "imports" / "expenses"
    (expenses / "2026-09-01 - Mood.png").write_bytes(b"png-bytes")
    cache = expenses / ".converted"
    cache.mkdir()
    (cache / "2026-09-01 - Mood.jpg").write_bytes(b"jpg-bytes")

    found = discover(tmp_repo / "imports")
    assert [f.relative_path for f in found] == ["expenses/2026-09-01 - Mood.png"]


class TestCanonicalRelativePath:
    """Tests for canonical_relative_path: strips a Dropbox listener collision suffix
    ("stem__<hash8>.ext") from the file's stem, leaving everything else untouched."""

    def test_strips_collision_suffix(self) -> None:
        assert (
            canonical_relative_path("expenses/2026-06-30 Guide Fabrics__a1b2c3d4.pdf")
            == "expenses/2026-06-30 Guide Fabrics.pdf"
        )

    def test_path_without_suffix_unchanged(self) -> None:
        assert (
            canonical_relative_path("expenses/2026-06-30 Guide Fabrics.pdf")
            == "expenses/2026-06-30 Guide Fabrics.pdf"
        )

    def test_uppercase_hex_not_stripped(self) -> None:
        assert canonical_relative_path("expenses/X__A1B2C3D4.pdf") == "expenses/X__A1B2C3D4.pdf"

    def test_seven_hex_chars_not_stripped(self) -> None:
        assert canonical_relative_path("expenses/X__a1b2c3d.pdf") == "expenses/X__a1b2c3d.pdf"

    def test_nine_hex_chars_not_stripped(self) -> None:
        assert canonical_relative_path("expenses/X__a1b2c3d4e.pdf") == "expenses/X__a1b2c3d4e.pdf"

    def test_only_last_component_touched(self) -> None:
        assert (
            canonical_relative_path("expenses__a1b2c3d4/X__a1b2c3d4.pdf")
            == "expenses__a1b2c3d4/X.pdf"
        )

    def test_no_parent_directory(self) -> None:
        assert canonical_relative_path("X__a1b2c3d4.pdf") == "X.pdf"
