"""Wait for a Dropbox folder's top-level contents to stop changing before downloading a batch.

A "settled" folder has produced `stable_polls` consecutive identical snapshots (sorted tuples of
(name, size, content_hash)). The first snapshot counts as poll 1, so stable_polls=2 means we need
one further identical poll (one sleep) after the first snapshot to call it settled.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .types import RemoteFile

Snapshot = tuple[tuple[str, int, str], ...]


def snapshot_of(files: list[RemoteFile]) -> Snapshot:
    return tuple(sorted((f.name, f.size, f.content_hash) for f in files))


def wait_for_settle(
    list_fn: Callable[[], list[RemoteFile]],
    poll_seconds: float,
    stable_polls: int,
    max_wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[list[RemoteFile]]:
    start = clock()
    files = list_fn()
    snap = snapshot_of(files)
    consecutive = 1

    if consecutive >= stable_polls:
        return files

    while True:
        if clock() - start > max_wait_seconds:
            return None
        sleep(poll_seconds)
        files = list_fn()
        new_snap = snapshot_of(files)
        if new_snap == snap:
            consecutive += 1
        else:
            consecutive = 1
            snap = new_snap
        if consecutive >= stable_polls:
            return files
