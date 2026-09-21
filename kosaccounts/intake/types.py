"""Shared shapes for the intake package. Do not change inside a subagent task; report back if
insufficient.

Folder names ("expenses", "bank") are the Dropbox top-level folder names relative to the app
folder root, identical to the local imports/<folder> names (IntakeConfig.folders).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RemoteFile:
    """One file directly inside a watched Dropbox folder (never a folder, never nested)."""

    folder: str          # "expenses" | "bank"
    name: str            # file name as displayed, e.g. "2026-06-30 Guide Fabrics.pdf"
    path_lower: str      # Dropbox path_lower, e.g. "/expenses/2026-06-30 guide fabrics.pdf"
    path_display: str    # Dropbox path_display
    dropbox_id: str      # FileMetadata.id, e.g. "id:abc123"
    size: int            # bytes
    content_hash: str    # Dropbox content hash (hex, 64 chars)


@dataclass(frozen=True)
class LongpollResult:
    changes: bool
    backoff_seconds: int = 0   # from the endpoint's `backoff`; 0 when absent


@dataclass
class ChangeSet:
    """Result of files/list_folder/continue after a long poll reported changes."""

    folders_changed: set[str] = field(default_factory=set)  # watched folders with top-level file changes
    new_cursor: str = ""
    reset: bool = False   # cursor invalid/reset: caller rebuilds the cursor and reconciles


class DropboxClient(Protocol):
    """What puller.py needs from Dropbox. RealDropboxClient (dropbox_client.py) implements it with
    the official SDK; tests use an in-memory fake."""

    def list_top_level_files(self, folder: str) -> list[RemoteFile]:
        """FileMetadata entries directly inside "/<folder>" (non-recursive; folders and anything
        nested are excluded). Raises if the folder does not exist."""
        ...

    def latest_cursor(self) -> str:
        """files_list_folder_get_latest_cursor on the app-folder root (""), recursive=True."""
        ...

    def longpoll(self, cursor: str, timeout_seconds: int) -> LongpollResult:
        """files/list_folder/longpoll. Blocks up to timeout_seconds (+ up to 90 s server jitter)."""
        ...

    def list_changes(self, cursor: str, watched_folders: list[str]) -> ChangeSet:
        """files_list_folder_continue until has_more is False. folders_changed holds each watched
        folder that has an entry (file, deleted, or folder metadata) whose path_lower is directly
        inside "/<folder>/" (exactly one more path component). Changes elsewhere are ignored. On
        ListFolderContinueError.is_reset() returns ChangeSet(reset=True)."""
        ...

    def download(self, remote: RemoteFile, dest: Path) -> None:
        """files_download_to_file(dest, remote.path_lower). Overwrites dest. Caller verifies."""
        ...
