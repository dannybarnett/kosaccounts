from __future__ import annotations

import csv
from pathlib import Path

from kosaccounts.duplicates import suggest_duplicates, write_report


def _write_suppliers(path: Path, rows: list[tuple[str, int]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Supplier", "Default category", "Other categories", "Count", "Last date"])
        for name, count in rows:
            w.writerow([name, "Fabric", "", count, "2026-01-01"])


def test_finds_prefix_and_fuzzy_pairs_and_ignores_unrelated(tmp_path):
    p = tmp_path / "suppliers.csv"
    _write_suppliers(
        p,
        [
            ("Amazon", 2),
            ("Amazon.com", 70),
            ("Delta", 6),
            ("Delta Airlines", 3),
            ("Y Cepeda", 4),
            ("Y Cepedea", 1),
            ("Mood", 27),
            ("Pacific Trimming", 68),
        ],
    )
    pairs = suggest_duplicates(p)
    found = {(x.a, x.b) for x in pairs}
    assert ("Amazon", "Amazon.com") in found
    assert ("Delta", "Delta Airlines") in found
    assert ("Y Cepeda", "Y Cepedea") in found
    assert all("Mood" not in pair and "Pacific Trimming" not in pair for pair in found)

    by_pair = {(x.a, x.b): x for x in pairs}
    assert by_pair[("Amazon", "Amazon.com")].suggested_canonical == "Amazon.com"  # more frequent
    assert by_pair[("Delta", "Delta Airlines")].suggested_canonical == "Delta"  # more frequent wins


def test_write_report(tmp_path):
    p = tmp_path / "suppliers.csv"
    _write_suppliers(p, [("Blick", 2), ("Blick Art Materials", 2)])
    out = write_report(suggest_duplicates(p), tmp_path / "dupes.csv")
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert rows[0]["Suggested canonical"] == "Blick Art Materials"  # tie -> longer name
    assert not (tmp_path / "dupes.csv.tmp").exists()
