"""Advisory file locks (fcntl.flock), shared by the listener, the reconcile timer and the watcher.

    with hold_lock(cfg.intake.lock_path):        # raises LockHeld at once if another holder exists
        ...

The lock file is created if missing (parents too) and never deleted. Compatible with
`flock(1)` as used by scripts/run_pipeline.sh: both lock the same path with LOCK_EX, so the
watcher's `hold_lock(logs/pipeline.lock)` and a manual `scripts/run_pipeline.sh` exclude each other.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockHeld(RuntimeError):
    """Another process holds the lock."""


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeld(f"lock held: {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
