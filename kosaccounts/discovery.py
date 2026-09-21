"""Walk imports/ and identify files not yet in the ledger.

CONTRACT (implement; do not change signatures):
- Stage is derived from the first path component: expenses/ -> "expense", bank/ -> "bank".
  Any other top-level folder (invoices/, stray files) is ignored.
- Hidden files, zero-byte files, and rclone partial files (*.partial, .~tmp~*) are ignored.
- relative_path uses POSIX separators relative to imports_root.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from kosaccounts.ledger import Ledger
from kosaccounts.models import DiscoveredFile, Stage

STAGE_FOLDERS: dict[str, Stage] = {"expenses": "expense", "bank": "bank"}

# A Dropbox re-upload of a same-name-different-content file is saved by the listener as
# "<stem>__<hash8><ext>" (per specs/README_server_intake.md). Exactly 8 lowercase hex chars.
_COLLISION_SUFFIX_RE = re.compile(r"^(?P<stem>.*)__[0-9a-f]{8}$")


def sha256_file(path: Path) -> str:
    """Compute SHA256 hash of file contents."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def discover(imports_root: Path, stages: Iterable[Stage] = ("expense", "bank")) -> list[DiscoveredFile]:
    """All eligible files under imports_root for the given stages, sorted by relative_path."""
    # Build set of target stage folders
    target_folders = {folder for folder, stage in STAGE_FOLDERS.items() if stage in stages}

    discovered: list[DiscoveredFile] = []

    for stage_folder in target_folders:
        stage_path = imports_root / stage_folder
        if not stage_path.exists():
            continue

        # Walk all files in the stage folder
        for file_path in stage_path.rglob("*"):
            # Skip directories
            if file_path.is_dir():
                continue

            # Skip hidden files and anything inside a hidden directory (e.g. the
            # expenses/.converted/ cache written by expense extraction).
            if any(part.startswith(".") for part in file_path.relative_to(stage_path).parts):
                continue

            # Skip zero-byte files
            if file_path.stat().st_size == 0:
                continue

            # Skip rclone partial files
            if file_path.name.endswith(".partial") or file_path.name.startswith(".~tmp~"):
                continue

            # Compute relative path with POSIX separators
            relative = file_path.relative_to(imports_root)
            relative_path = relative.as_posix()

            # Compute SHA256
            sha256 = sha256_file(file_path)

            discovered.append(
                DiscoveredFile(
                    path=file_path,
                    relative_path=relative_path,
                    stage=STAGE_FOLDERS[stage_folder],  # type: ignore
                    sha256=sha256,
                    size=file_path.stat().st_size,
                )
            )

    # Sort by relative_path
    discovered.sort(key=lambda f: f.relative_path)
    return discovered


def new_files(files: list[DiscoveredFile], ledger: Ledger) -> list[DiscoveredFile]:
    """Subset of `files` whose sha256 is not in the ledger."""
    return [f for f in files if not ledger.has_hash(f.sha256)]


def canonical_relative_path(relative_path: str) -> str:
    """Strip a trailing "__<hash8>" collision suffix from the file's stem (before the extension),
    leaving the rest of the path untouched.

    "expenses/2026-06-30 Guide Fabrics__a1b2c3d4.pdf" -> "expenses/2026-06-30 Guide Fabrics.pdf"
    A path without that exact suffix (uppercase hex, or not exactly 8 hex chars) is returned
    unchanged. Only the last path component (the filename) is examined."""
    p = Path(relative_path)
    m = _COLLISION_SUFFIX_RE.match(p.stem)
    if not m:
        return relative_path
    canonical_name = m.group("stem") + p.suffix
    parent = p.parent
    if str(parent) in (".", ""):
        return canonical_name
    return f"{parent.as_posix()}/{canonical_name}"
