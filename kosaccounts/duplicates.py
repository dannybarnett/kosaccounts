"""Suggest likely duplicate suppliers in data/suppliers.csv (instructions.md: "suggest where there
might be duplications"). Report only; merging is Danny's decision (add a row to
supplier_aliases.csv mapping the minor spelling to the canonical one)."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from kosaccounts.suppliers import normalise

COLUMNS = ["Score", "Supplier A", "Count A", "Supplier B", "Count B", "Suggested canonical"]


@dataclass
class DuplicatePair:
    score: float
    a: str
    count_a: int
    b: str
    count_b: int

    @property
    def suggested_canonical(self) -> str:
        """The more frequent spelling; on a tie the longer (more specific) one."""
        if self.count_a != self.count_b:
            return self.a if self.count_a > self.count_b else self.b
        return self.a if len(self.a) >= len(self.b) else self.b


def suggest_duplicates(suppliers_csv: Path, min_score: float = 85.0) -> list[DuplicatePair]:
    with suppliers_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    names = [r["Supplier"] for r in rows]
    counts = {r["Supplier"]: int(r.get("Count") or 0) for r in rows}
    norms = {n: normalise(n) for n in names}

    pairs: list[DuplicatePair] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            na, nb = norms[a], norms[b]
            if not na or not nb:
                continue
            score = float(fuzz.token_set_ratio(na, nb))
            ta, tb = na.split(), nb.split()
            leads = ta[: len(tb)] == tb or tb[: len(ta)] == ta
            if score >= min_score or (leads and min(len(na), len(nb)) >= 4):
                pairs.append(DuplicatePair(max(score, 100.0 if leads else score), a, counts[a], b, counts[b]))
    pairs.sort(key=lambda p: (-p.score, p.a.lower()))
    return pairs


def write_report(pairs: list[DuplicatePair], out_csv: Path) -> Path:
    tmp = out_csv.with_suffix(out_csv.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for p in pairs:
            w.writerow([f"{p.score:.1f}", p.a, p.count_a, p.b, p.count_b, p.suggested_canonical])
    os.replace(tmp, out_csv)
    return out_csv
