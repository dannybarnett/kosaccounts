#!/usr/bin/env python3
"""Kosibah Couture LLC: Dropbox to Dropbox copy (Stage 1, Mac side).

Copies top level files from two shared Dropbox folders into the kosaccounts
app folder, where the Ubuntu server picks them up.

    Kosibah expenses              ->  Apps/kosaccounts/expenses
    KOSIBAH LLC BANK STATEMENTS   ->  Apps/kosaccounts/bank

Function overview:
    * sha256_file() calculates a file's SHA-256 hash for integrity checks.
    * is_eligible() filters out hidden files, temporary files, and partial downloads.
    * list_files() lists eligible top-level regular files and records their metadata.
    * detect_dropbox_root() finds the configured or automatically detected Dropbox folder.
    * atomic_write_json() safely writes state data without leaving a partially written file.
    * place() moves or copies a completed staged file into its destination atomically.
    * Job represents one copy operation, such as expenses or bank statements.
    * Job.warn_throttled() prevents repeated warning messages from flooding the log.
    * Job.is_sent() checks whether a file with the same size and modification time was
        already processed.
    * Job.tick() performs one polling cycle: detects changes, waits for the folder to
        settle, checks file readiness, and starts delivery when appropriate.
    * Job.deliver() hashes, stages, verifies, and places files in the destination, then
        updates the processing state.
    * setup_logging() configures rotating file logging and stderr logging.
    * main() parses arguments and configuration, prevents multiple instances, discovers
        Dropbox, creates jobs, handles shutdown signals, and runs the polling loop.
    * The nested save_state() function writes the current processing state.
    * The nested handle_signal() function lets the service shut down cleanly when it
        receives SIGTERM or SIGINT.

Rules this program keeps:
  * Copy only. Nothing is ever deleted, moved, or modified in the sources,
    and nothing is ever deleted in the destinations.
  * Top level files only. Subfolders are ignored.
  * Batches. Nothing is copied until the source folder has stopped changing.
  * A file appears under its final name in the destination only when complete.
  * Online only (dataless) placeholders are never forced to download; the
    batch is treated as not ready and retried later.

Standard library only. Runs as a launchd service (see install.sh).
"""

import argparse
import configparser
import fcntl
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import time
from pathlib import Path

HOME = Path.home()
DEFAULT_STATE_DIR = HOME / "Library" / "Application Support" / "kosaccounts"
DEFAULT_LOG_DIR = HOME / "Library" / "Logs" / "kosaccounts"

# (source folder name at the Dropbox root, destination path relative to the root)
JOBS = {
    "expenses": ("Kosibah expenses", "Apps/kosaccounts/expenses"),
    "bank": ("KOSIBAH LLC BANK STATEMENTS", "Apps/kosaccounts/bank"),
}

SF_DATALESS = 0x40000000  # macOS file flag: contents are online only
IGNORED_PREFIXES = (".", "~$")
IGNORED_SUFFIXES = (".tmp", ".part", ".crdownload")
PARTIAL_SUFFIX = ".kosaccounts_partial"
WARN_EVERY_SECONDS = 1800

DEFAULTS = {
    "dropbox_root": "",
    "poll_seconds": "60",
    "settle_stable_polls": "2",
    "settle_max_wait_minutes": "60",
    "state_dir": str(DEFAULT_STATE_DIR),
    "log_dir": str(DEFAULT_LOG_DIR),
}

log = logging.getLogger("kosaccounts")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_eligible(name):
    lower = name.lower()
    return not (lower.startswith(IGNORED_PREFIXES) or lower.endswith(IGNORED_SUFFIXES))


def list_files(folder):
    """Top level regular files only: {name: (size, mtime_ns, flags)}."""
    found = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if not is_eligible(entry.name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            found[entry.name] = (st.st_size, st.st_mtime_ns, getattr(st, "st_flags", 0))
    return found


def detect_dropbox_root(configured):
    if configured:
        return Path(configured).expanduser()
    candidates = [HOME / "Dropbox"]
    candidates += [Path(p) for p in sorted(glob.glob(str(HOME / "Library" / "CloudStorage" / "Dropbox*")))]
    for candidate in candidates:
        if any((candidate / src).is_dir() for src, _ in JOBS.values()):
            return candidate
    return None


def atomic_write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def place(staged, final):
    """Put a finished file at its final name. A rename where possible;
    otherwise copy to a hidden partial name in the same folder, then rename."""
    try:
        os.replace(staged, final)
        return
    except OSError:
        pass
    partial = final.with_name("." + final.name + PARTIAL_SUFFIX)
    try:
        shutil.copyfile(staged, partial)
        os.replace(partial, final)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    os.unlink(staged)


class Job:
    def __init__(self, key, root, state_dir, state, settle_polls, max_wait_minutes):
        src_name, dest_rel = JOBS[key]
        self.key = key
        self.root = root
        self.source = root / src_name
        self.dest = root / dest_rel
        self.staging = state_dir / "staging" / key
        self.state = state.setdefault(key, {})
        self.settle_polls = settle_polls
        self.max_wait = max_wait_minutes * 60
        self.last_snapshot = None
        self.stable = 0
        self.pending_since = None
        self.last_warn = {}

    def warn_throttled(self, tag, message):
        now = time.monotonic()
        if now - self.last_warn.get(tag, -1e9) >= WARN_EVERY_SECONDS:
            self.last_warn[tag] = now
            log.warning("%s: %s", self.key, message)

    def is_sent(self, name, size, mtime_ns):
        rec = self.state.get(name)
        return bool(rec) and rec["size"] == size and rec["mtime_ns"] == mtime_ns

    def tick(self, save_state):
        if not self.source.is_dir():
            self.warn_throttled("nosource", "source folder not found: %s" % self.source)
            return
        files = list_files(self.source)
        snapshot = tuple(sorted((n, s, m) for n, (s, m, _f) in files.items()))
        pending = sorted(n for n, (s, m, _f) in files.items() if not self.is_sent(n, s, m))

        if not pending:
            self.last_snapshot = snapshot
            self.stable = 0
            self.pending_since = None
            return

        if self.pending_since is None:
            self.pending_since = time.monotonic()
            log.info("%s: change detected, %d file(s) to send; waiting for the folder to settle",
                     self.key, len(pending))
        if snapshot == self.last_snapshot:
            self.stable += 1
        else:
            self.stable = 0
            self.last_snapshot = snapshot
        if self.stable < self.settle_polls:
            if time.monotonic() - self.pending_since > self.max_wait:
                self.warn_throttled("nosettle", "folder has not settled after %d minutes; still waiting"
                                    % (self.max_wait // 60))
            return

        if any(files[n][2] & SF_DATALESS for n in pending):
            self.warn_throttled("dataless", "some files are online only placeholders; batch not ready")
            return
        if not self.dest.parent.is_dir():
            self.warn_throttled("noapp", "app folder missing: %s (not creating it)" % self.dest.parent)
            return

        self.deliver(files, pending)
        save_state()
        self.stable = 0
        self.pending_since = None

    def deliver(self, files, pending):
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir(parents=True)
        records = {}
        to_send = []
        total_bytes = 0

        for name in pending:
            size, mtime_ns, _flags = files[name]
            src = self.source / name
            sha = sha256_file(src)
            record = {"size": size, "mtime_ns": mtime_ns, "sha256": sha}

            prev = self.state.get(name)
            if prev and prev["sha256"] == sha:
                records[name] = record  # touched but content unchanged
                continue
            dest_file = self.dest / name
            if dest_file.is_file() and sha256_file(dest_file) == sha:
                records[name] = record  # already present at the destination
                continue

            staged = self.staging / name
            shutil.copyfile(src, staged)
            if sha256_file(staged) != sha:
                raise RuntimeError("verification failed for %s" % name)
            st = os.stat(src)
            if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
                raise RuntimeError("%s changed while being copied; will retry" % name)
            to_send.append(name)
            records[name] = record
            total_bytes += size

        self.dest.mkdir(exist_ok=True)
        for name in to_send:
            place(self.staging / name, self.dest / name)
        shutil.rmtree(self.staging, ignore_errors=True)

        self.state.update(records)
        if to_send:
            log.info("%s: delivered %d file(s), %d bytes: %s", self.key, len(to_send), total_bytes,
                     ", ".join(to_send))
        else:
            log.info("%s: nothing new to deliver (%d file(s) already present)", self.key, len(records))


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(log_dir / "mac_copy.log", maxBytes=1_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)


def main():
    parser = argparse.ArgumentParser(description="Kosibah Dropbox to Dropbox copy (Mac side)")
    parser.add_argument("--config", default=str(DEFAULT_STATE_DIR / "mac_copy.conf"))
    parser.add_argument("--once", action="store_true",
                        help="run a single pass now, without waiting for the folders to settle, then exit")
    args = parser.parse_args()

    cp = configparser.ConfigParser()
    cp.read_dict({"main": DEFAULTS})
    if Path(args.config).exists():
        cp.read(args.config)
    cfg = cp["main"]

    state_dir = Path(cfg["state_dir"]).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(Path(cfg["log_dir"]).expanduser())

    lock_file = open(state_dir / "mac_copy.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another instance is already running; exiting")
        return 0

    state_path = state_dir / "state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            log.error("state file unreadable; starting fresh (files will be compared by content, not resent blindly)")

    def save_state():
        atomic_write_json(state_path, state)

    poll = int(cfg["poll_seconds"])
    settle_polls = 0 if args.once else int(cfg["settle_stable_polls"])
    max_wait = int(cfg["settle_max_wait_minutes"])

    stop = {"flag": False}

    def handle_signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("started; poll every %ds, settle after %d stable poll(s)", poll, settle_polls)
    jobs = None
    warned_root = 0.0

    while not stop["flag"]:
        if jobs is None:
            root = detect_dropbox_root(cfg["dropbox_root"])
            if root is None:
                if time.monotonic() - warned_root >= WARN_EVERY_SECONDS:
                    warned_root = time.monotonic()
                    log.error("Dropbox folder not found; set dropbox_root in %s", args.config)
            else:
                log.info("Dropbox root: %s", root)
                jobs = [Job(k, root, state_dir, state, settle_polls, max_wait) for k in JOBS]
        if jobs:
            for job in jobs:
                try:
                    job.tick(save_state)
                except Exception:
                    log.exception("%s: cycle failed; will retry", job.key)
        if args.once:
            break
        for _ in range(poll):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
