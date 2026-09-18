"""Category -> Schedule C mapping from data/categories.csv (columns: Category, Schedule C).

Rule: NO new categories may be introduced by the pipeline. Anything not in the file is invalid.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional


class Categories:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[tuple[str, str]] = []
        with Path(path).open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                name = (row.get("Category") or "").strip()
                schedule_c = (row.get("Schedule C") or "").strip()
                if not name:
                    continue
                self._entries.append((name, schedule_c))
        self._by_lower: dict[str, tuple[str, str]] = {
            name.lower(): (name, schedule_c) for name, schedule_c in self._entries
        }

    @property
    def names(self) -> list[str]:
        """Category names in file order."""
        return [name for name, _ in self._entries]

    def is_valid(self, category: Optional[str]) -> bool:
        """Case-insensitive, whitespace-trimmed membership test."""
        if category is None:
            return False
        key = category.strip().lower()
        return key in self._by_lower

    def canonical(self, category: str) -> Optional[str]:
        """The exact spelling from the file for a case-insensitive match, else None."""
        entry = self._by_lower.get(category.strip().lower())
        return entry[0] if entry else None

    def schedule_c_for(self, category: str) -> Optional[str]:
        entry = self._by_lower.get(category.strip().lower())
        return entry[1] if entry else None
