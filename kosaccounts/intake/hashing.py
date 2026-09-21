"""Dropbox content hash (https://www.dropbox.com/developers/reference/content-hash).

For each 4 MiB block of the file, compute SHA-256 of the block. Concatenate the raw block
digests (32 bytes each) in order, then SHA-256 the concatenation. The result is the lowercase
hex digest of that final hash. An empty file has zero blocks, so its hash is
sha256(b"").hexdigest() passed through sha256 once more, i.e. sha256(sha256(b"").digest()).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

BLOCK_SIZE = 4 * 1024 * 1024


def dropbox_content_hash(path: Path) -> str:
    """Compute the Dropbox content hash of the file at `path`."""
    overall = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(BLOCK_SIZE)
            if not block:
                break
            overall.update(hashlib.sha256(block).digest())
    return overall.hexdigest()


def dropbox_content_hash_bytes(data: bytes) -> str:
    """Compute the Dropbox content hash of an in-memory bytes object (for tests)."""
    overall = hashlib.sha256()
    for start in range(0, len(data), BLOCK_SIZE):
        block = data[start : start + BLOCK_SIZE]
        overall.update(hashlib.sha256(block).digest())
    return overall.hexdigest()
