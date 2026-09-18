"""Tests for kosaccounts.categories.Categories."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kosaccounts.categories import Categories


def _write_categories_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Category", "Schedule C"])
        writer.writerows(rows)


@pytest.fixture
def categories_path(tmp_repo: Path) -> Path:
    path = tmp_repo / "data" / "categories.csv"
    _write_categories_csv(
        path,
        [
            ("Fabric", "Cost of Goods Sold"),
            ("Telephone", "Other expenses"),
            ("Software", "Office expense"),
        ],
    )
    return path


def test_names_are_in_file_order(categories_path: Path) -> None:
    categories = Categories(categories_path)
    assert categories.names == ["Fabric", "Telephone", "Software"]


def test_is_valid_is_case_insensitive_and_trims_whitespace(categories_path: Path) -> None:
    categories = Categories(categories_path)
    assert categories.is_valid("Fabric") is True
    assert categories.is_valid("fabric") is True
    assert categories.is_valid("  FABRIC  ") is True
    assert categories.is_valid("Telephone") is True
    assert categories.is_valid(None) is False


def test_unknown_category_is_invalid(categories_path: Path) -> None:
    categories = Categories(categories_path)
    assert categories.is_valid("Not A Real Category") is False
    assert categories.canonical("Not A Real Category") is None
    assert categories.schedule_c_for("Not A Real Category") is None


def test_canonical_returns_exact_file_spelling(categories_path: Path) -> None:
    categories = Categories(categories_path)
    assert categories.canonical("software") == "Software"
    assert categories.canonical("  SOFTWARE  ") == "Software"
    assert categories.canonical("Software") == "Software"


def test_schedule_c_for(categories_path: Path) -> None:
    categories = Categories(categories_path)
    assert categories.schedule_c_for("Fabric") == "Cost of Goods Sold"
    assert categories.schedule_c_for("telephone") == "Other expenses"
    assert categories.schedule_c_for("Software") == "Office expense"
