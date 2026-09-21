"""Run summary output: append a human-readable block to logs/run-YYYY-MM-DD.log and overwrite
logs/last_run.json with the RunSummary as JSON (datetimes ISO).

If summary.nothing_to_do: a single line "<timestamp> nothing to do (0 new files)".

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

from kosaccounts.models import RunSummary


def write_summary(summary: RunSummary, logs_dir: Path) -> Path:
    """Returns the path of the .log file written."""
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Format the summary text block
    text_block = format_summary(summary)

    # Append to the daily log file
    log_path = logs_dir / f"run-{summary.started:%Y-%m-%d}.log"
    with open(log_path, "a") as f:
        f.write(text_block)
        f.write("\n\n")

    # Write last_run.json with datetimes as ISO strings
    def datetime_handler(obj: object) -> str:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    json_path = logs_dir / "last_run.json"
    with open(json_path, "w") as f:
        json.dump(dataclasses.asdict(summary), f, default=datetime_handler, indent=2)

    return log_path


def format_summary(summary: RunSummary) -> str:
    """The text block (also printed to stdout by the CLI)."""
    if summary.nothing_to_do:
        return f"{summary.started.isoformat()} nothing to do (0 new files)"

    # Build the formatted block
    lines = []

    # Header line
    finished_str = summary.finished.isoformat() if summary.finished else "unfinished"
    dry_run_str = " (dry run)" if summary.dry_run else ""
    lines.append(f"=== run {summary.started.isoformat()} -> {finished_str}{dry_run_str}")

    # Files found, new, flagged, errored per stage
    stages = sorted(set(list(summary.files_found.keys()) +
                       list(summary.files_new.keys()) +
                       list(summary.files_flagged.keys()) +
                       list(summary.files_errored.keys())))

    if stages:
        found_parts = [f"{stage}: {summary.files_found.get(stage, 0)}" for stage in stages]
        lines.append(f"Files found:     {', '.join(found_parts)}")

        new_parts = [f"{stage}: {summary.files_new.get(stage, 0)}" for stage in stages]
        lines.append(f"Files new:       {', '.join(new_parts)}")

        flagged_parts = [f"{stage}: {summary.files_flagged.get(stage, 0)}" for stage in stages]
        lines.append(f"Files flagged:   {', '.join(flagged_parts)}")

        errored_parts = [f"{stage}: {summary.files_errored.get(stage, 0)}" for stage in stages]
        lines.append(f"Files errored:   {', '.join(errored_parts)}")

    # Rows added per stage
    if summary.rows_added:
        rows_parts = [f"{stage}: {summary.rows_added[stage]}" for stage in sorted(summary.rows_added.keys())]
        lines.append(f"Rows added:      {', '.join(rows_parts)}")

    # Review rows
    lines.append(f"Review rows:     {summary.rows_review}")

    # Superseded rows
    lines.append(f"Superseded rows: {summary.rows_superseded}")

    # New suppliers
    if summary.new_suppliers:
        suppliers_str = ", ".join(summary.new_suppliers)
    else:
        suppliers_str = "none"
    lines.append(f"New suppliers:   {suppliers_str}")

    # Unmatched transactions
    lines.append(f"Unmatched bank:  {summary.unmatched_bank_txns}")
    lines.append(f"Unmatched expense: {summary.unmatched_expenses}")

    # Warnings (non-fatal per-file problems)
    if summary.warnings:
        lines.append("Warnings:")
        for warning in summary.warnings:
            lines.append(f"  {warning}")

    # Errors
    if summary.errors:
        lines.append("Errors:")
        for error in summary.errors:
            lines.append(f"  {error}")

    return "\n".join(lines)
