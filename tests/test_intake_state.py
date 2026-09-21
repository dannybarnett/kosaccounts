"""Tests for kosaccounts.intake.state.IntakeState."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kosaccounts.intake.state import ImportedEntry, IntakeState
from kosaccounts.intake.types import RemoteFile


def make_remote(
    folder: str = "expenses",
    name: str = "invoice.pdf",
    content_hash: str = "a" * 64,
    dropbox_id: str = "id:1",
    size: int = 100,
) -> RemoteFile:
    return RemoteFile(
        folder=folder,
        name=name,
        path_lower=f"/{folder}/{name.lower()}",
        path_display=f"/{folder}/{name}",
        dropbox_id=dropbox_id,
        size=size,
        content_hash=content_hash,
    )


def test_creates_parent_dirs_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "intake.sqlite"
    state = IntakeState(db_path)
    try:
        assert db_path.exists()
        assert state.entries() == []
    finally:
        state.close()


def test_context_manager_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "intake.sqlite"
    with IntakeState(db_path) as state:
        assert state.entries() == []
    # closed connection should raise if used again
    with pytest.raises(Exception):
        state._conn.execute("SELECT 1")


def test_record_and_is_imported(tmp_path: Path) -> None:
    with IntakeState(tmp_path / "intake.sqlite") as state:
        remote = make_remote()
        assert not state.is_imported("expenses", "invoice.pdf", "a" * 64)
        state.record(remote, "invoice.pdf")
        assert state.is_imported("expenses", "invoice.pdf", "a" * 64)
        # different hash, same name -> not imported
        assert not state.is_imported("expenses", "invoice.pdf", "b" * 64)
        # different folder -> not imported
        assert not state.is_imported("bank", "invoice.pdf", "a" * 64)


def test_known_hashes_for_name(tmp_path: Path) -> None:
    with IntakeState(tmp_path / "intake.sqlite") as state:
        assert state.known_hashes_for_name("expenses", "invoice.pdf") == set()
        state.record(make_remote(content_hash="a" * 64), "invoice.pdf")
        state.record(
            make_remote(content_hash="b" * 64, dropbox_id="id:2"),
            "invoice__bbbbbbbb.pdf",
        )
        assert state.known_hashes_for_name("expenses", "invoice.pdf") == {
            "a" * 64,
            "b" * 64,
        }
        assert state.known_hashes_for_name("bank", "invoice.pdf") == set()


def test_record_is_idempotent(tmp_path: Path) -> None:
    with IntakeState(tmp_path / "intake.sqlite") as state:
        remote = make_remote()
        state.record(remote, "invoice.pdf")
        state.record(remote, "invoice.pdf")
        state.record(remote, "invoice.pdf")
        entries = state.entries("expenses")
        assert len(entries) == 1
        assert entries[0].local_name == "invoice.pdf"
        assert entries[0].dropbox_id == "id:1"


def test_entries_all_and_by_folder_ordered(tmp_path: Path) -> None:
    with IntakeState(tmp_path / "intake.sqlite") as state:
        state.record(make_remote(folder="expenses", name="a.pdf", dropbox_id="id:1"), "a.pdf")
        state.record(make_remote(folder="bank", name="b.pdf", dropbox_id="id:2"), "b.pdf")
        state.record(make_remote(folder="expenses", name="c.pdf", dropbox_id="id:3"), "c.pdf")

        all_entries = state.entries()
        assert [e.id for e in all_entries] == sorted(e.id for e in all_entries)
        assert [e.local_name for e in all_entries] == ["a.pdf", "b.pdf", "c.pdf"]

        expenses_entries = state.entries("expenses")
        assert [e.local_name for e in expenses_entries] == ["a.pdf", "c.pdf"]
        assert all(isinstance(e, ImportedEntry) for e in expenses_entries)

        bank_entries = state.entries("bank")
        assert [e.local_name for e in bank_entries] == ["b.pdf"]


def test_record_with_explicit_imported_at(tmp_path: Path) -> None:
    with IntakeState(tmp_path / "intake.sqlite") as state:
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.record(make_remote(), "invoice.pdf", imported_at=ts)
        entries = state.entries()
        assert entries[0].imported_at == ts.isoformat()
