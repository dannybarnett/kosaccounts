"""SQLite-backed record of every file the intake pipeline has downloaded and renamed into
`imports/<folder>/`. This is the source of truth for "already imported" -- NOT the presence of
a file in the imports folder, since the processor may move finished files elsewhere.

    with IntakeState(cfg.db_path) as state:
        if not state.is_imported(folder, name, content_hash):
            ...
        state.record(remote, local_name)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .types import RemoteFile

SCHEMA = """
CREATE TABLE IF NOT EXISTS imported (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    dropbox_id TEXT NOT NULL,
    dropbox_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    local_name TEXT NOT NULL,
    UNIQUE(folder, name, content_hash)
);
"""


@dataclass(frozen=True)
class ImportedEntry:
    id: int
    folder: str
    name: str
    dropbox_id: str
    dropbox_path: str
    size: int
    content_hash: str
    imported_at: str
    local_name: str


class IntakeState:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def is_imported(self, folder: str, name: str, content_hash: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM imported WHERE folder = ? AND name = ? AND content_hash = ? LIMIT 1",
            (folder, name, content_hash),
        )
        return cur.fetchone() is not None

    def known_hashes_for_name(self, folder: str, name: str) -> set[str]:
        cur = self._conn.execute(
            "SELECT content_hash FROM imported WHERE folder = ? AND name = ?",
            (folder, name),
        )
        return {row[0] for row in cur.fetchall()}

    def record(
        self,
        remote: RemoteFile,
        local_name: str,
        imported_at: Optional[datetime] = None,
    ) -> None:
        ts = imported_at or datetime.now(timezone.utc)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO imported
                (folder, name, dropbox_id, dropbox_path, size, content_hash, imported_at, local_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                remote.folder,
                remote.name,
                remote.dropbox_id,
                remote.path_lower,
                remote.size,
                remote.content_hash,
                ts.isoformat(),
                local_name,
            ),
        )
        self._conn.commit()

    def entries(self, folder: Optional[str] = None) -> list[ImportedEntry]:
        if folder is None:
            cur = self._conn.execute(
                "SELECT id, folder, name, dropbox_id, dropbox_path, size, content_hash, "
                "imported_at, local_name FROM imported ORDER BY id"
            )
        else:
            cur = self._conn.execute(
                "SELECT id, folder, name, dropbox_id, dropbox_path, size, content_hash, "
                "imported_at, local_name FROM imported WHERE folder = ? ORDER BY id",
                (folder,),
            )
        return [ImportedEntry(*row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "IntakeState":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
