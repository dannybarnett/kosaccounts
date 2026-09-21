"""Best-effort git commit + push of the output workbook and its supporting data CSVs after a run.

The server is the only writer of these files; each run that changes them commits and pushes so
`git pull` on a laptop picks up the latest workbook (specs/kosaccounts-pipeline-project.md decision
from 2026-09-21). Never touches history: no pull/merge/rebase/reset, and only the paths in
OUTPUT_PATHS are ever staged. Failures here are logged and swallowed by the caller (cli.cmd_run);
a git problem must never change the run's own exit code.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from kosaccounts.models import RunSummary

logger = logging.getLogger("kosaccounts.gitsync")

OUTPUT_PATHS = ["output/kosibah_import.xlsx", "data/suppliers.csv", "data/supplier_aliases.csv"]

_FALLBACK_IDENTITY = ["-c", "user.name=kosaccounts", "-c", "user.email=kosaccounts@localhost"]


def _is_git_repo(root: Path, run) -> bool:
    result = run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def changed_output_paths(root: Path, paths: list[str] = OUTPUT_PATHS, run=subprocess.run) -> list[str]:
    """Paths (from `paths`) that are modified, added or untracked, per `git status --porcelain`.

    Returns [] if `root` is not inside a git work tree."""
    if not _is_git_repo(root, run):
        logger.debug("%s is not inside a git work tree; skipping git status", root)
        return []

    result = run(
        ["git", "-C", str(root), "status", "--porcelain", "--", *paths],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.debug("git status failed for %s: %s", root, (result.stderr or "").strip())
        return []

    changed: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        # Porcelain format: "XY <path>" (or "XY <path> -> <path>" for renames, not relevant here).
        path = line[3:].strip()
        if path:
            changed.append(path)
    return changed


def commit_outputs(root: Path, message: str, push: bool, run=subprocess.run) -> str:
    """Commit (and optionally push) whichever of OUTPUT_PATHS changed under `root`.

    Returns one of "nothing", "committed", "pushed", "commit-failed", "push-failed". Never runs
    git pull/merge/rebase/reset, and never stages anything outside OUTPUT_PATHS.
    """
    changed = changed_output_paths(root, run=run)
    if not changed:
        logger.info("git: nothing changed under %s; skipping commit", root)
        return "nothing"

    logger.info("git: staging %s", ", ".join(changed))
    add_result = run(["git", "-C", str(root), "add", "--", *changed], capture_output=True, text=True)
    if add_result.returncode != 0:
        logger.error("git add failed: %s", (add_result.stderr or "").strip())
        return "commit-failed"

    commit_argv = ["git", "-C", str(root), "commit", "-q", "-m", message]
    commit_result = run(commit_argv, capture_output=True, text=True)
    if commit_result.returncode != 0:
        logger.info("git commit failed, retrying with fallback identity: %s", (commit_result.stderr or "").strip())
        fallback_argv = ["git", "-C", str(root), *_FALLBACK_IDENTITY, "commit", "-q", "-m", message]
        commit_result = run(fallback_argv, capture_output=True, text=True)
        if commit_result.returncode != 0:
            logger.error("git commit failed: %s", (commit_result.stderr or "").strip())
            return "commit-failed"

    logger.info("git: committed %d path(s)", len(changed))

    if not push:
        return "committed"

    push_result = run(
        ["git", "-C", str(root), "push", "-q", "origin", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if push_result.returncode != 0:
        logger.warning(
            "git push failed (typical cause: the laptop pushed first, non-fast-forward): %s; "
            "commit stays local and the next successful push will carry it",
            (push_result.stderr or "").strip(),
        )
        return "push-failed"

    logger.info("git: pushed to origin")
    return "pushed"


def run_message(summary: RunSummary) -> str:
    """e.g. 'Workbook update 2026-09-21 20:41: expense rows +3, bank rows +0, review 0, superseded 0'"""
    timestamp = summary.started.strftime("%Y-%m-%d %H:%M")
    expense_rows = summary.rows_added.get("expense", 0)
    bank_rows = summary.rows_added.get("bank", 0)
    return (
        f"Workbook update {timestamp}: expense rows +{expense_rows}, bank rows +{bank_rows}, "
        f"review {summary.rows_review}, superseded {summary.rows_superseded}"
    )
