"""Tests for kosaccounts.suppliers."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeError
from kosaccounts.config import Config
from kosaccounts.suppliers import SupplierBook, normalise

SUPPLIERS_ROWS = [
    ("T-Mobile", "Telephone", "", "5", "2026-01-01"),
    ("Amazon.com", "Office supplies", "Haberdashery", "70", "2026-06-10"),
    ("Mood", "Fabric", "", "12", "2026-05-01"),
    ("Pacific Trimming", "Fabric", "", "8", "2026-04-01"),
    ("C&C Button Inc", "Fabric", "", "3", "2026-03-01"),
    ("SIL Thread Inc", "Fabric", "", "4", "2026-02-01"),
    ("Extra Space", "Rent", "", "2", "2026-01-15"),
]

CATEGORY_ROWS = [
    ("Fabric", "Cost of Goods Sold"),
    ("Telephone", "Other expenses"),
    ("Office supplies", "Office expense"),
    ("Rent", "Rent or lease"),
]


def _write_suppliers_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Supplier", "Default category", "Other categories", "Count", "Last date"])
        writer.writerows(SUPPLIERS_ROWS)


def _write_categories_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Category", "Schedule C"])
        writer.writerows(CATEGORY_ROWS)


def _write_aliases_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Alias", "Supplier"])
        writer.writerows(rows)


@pytest.fixture
def categories(cfg: Config) -> Categories:
    _write_categories_csv(cfg.paths.categories)
    return Categories(cfg.paths.categories)


@pytest.fixture
def book(cfg: Config, categories: Categories) -> SupplierBook:
    _write_suppliers_csv(cfg.paths.suppliers)
    return SupplierBook(cfg, categories, client=None)


class FakeClientChoosesFirst:
    """Pretends the model always picks candidate #1."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_json(self, prompt: str, files=()) -> dict:
        self.calls.append(prompt)
        return {"choice": 1, "reason": "looks right"}


class FakeClientChoosesNone:
    def run_json(self, prompt: str, files=()) -> dict:
        return {"choice": 0, "reason": "no good match"}


class FakeClientErrors:
    def __init__(self) -> None:
        self.calls = 0

    def run_json(self, prompt: str, files=()) -> dict:
        self.calls += 1
        raise ClaudeError("boom")


class FakeClientCategory:
    def __init__(self, category):
        self.category = category
        self.calls: list[str] = []

    def run_json(self, prompt: str, files=()) -> dict:
        self.calls.append(prompt)
        return {"category": self.category}


# ---------------------------------------------------------------------------
# normalise()
# ---------------------------------------------------------------------------


def test_normalise_examples() -> None:
    assert normalise("C&C Button Inc") == "c c button"
    assert normalise("T-Mobile") == "t mobile"
    assert normalise("TMOBILE*AUTO PAY") == "tmobile auto pay"
    assert normalise("The Home Depot") == "home depot"
    assert normalise("Mood") == "mood"
    assert normalise("Inc") == "inc"


# ---------------------------------------------------------------------------
# Tier 1/2: alias and exact
# ---------------------------------------------------------------------------


def test_alias_hit(book: SupplierBook) -> None:
    _write_aliases_csv(book.cfg.paths.aliases, [("T Mobile", "T-Mobile")])
    book2 = SupplierBook(book.cfg, book.categories, client=None)
    result = book2.match("T Mobile")
    assert result.method == "alias"
    assert result.canonical == "T-Mobile"
    assert result.score == 100
    assert result.is_new is False


def test_exact_hit_ignores_case_and_punctuation(book: SupplierBook) -> None:
    result = book.match("c&c button inc.")
    assert result.method == "exact"
    assert result.canonical == "C&C Button Inc"
    assert result.score == 100
    assert result.default_category == "Fabric"


# ---------------------------------------------------------------------------
# Tier 3: fuzzy
# ---------------------------------------------------------------------------


def test_fuzzy_hit_above_threshold_records_alias_and_survives_reload(book: SupplierBook) -> None:
    result = book.match("Pacific Trimmings")
    assert result.method == "fuzzy"
    assert result.canonical == "Pacific Trimming"
    assert result.score >= book.cfg.suppliers.auto_match_threshold
    assert result.is_new is False

    # alias was recorded in memory
    assert book.match("Pacific Trimmings").method == "alias"

    book.save()

    reloaded = SupplierBook(book.cfg, book.categories, client=None)
    again = reloaded.match("Pacific Trimmings")
    assert again.method == "alias"
    assert again.canonical == "Pacific Trimming"

    with book.cfg.paths.aliases.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert any(r["Alias"] == "Pacific Trimmings" and r["Supplier"] == "Pacific Trimming" for r in rows)


def test_bank_descriptor_containment_hit(book: SupplierBook) -> None:
    result = book.match("EXTRA SPACE STORAGE 1234 NY")
    assert result.method == "fuzzy"
    assert result.canonical == "Extra Space"
    assert result.score >= 95
    assert result.is_new is False


# ---------------------------------------------------------------------------
# Tier 4: model adjudication (grey zone)
# ---------------------------------------------------------------------------


def test_grey_zone_without_client_becomes_new(book: SupplierBook) -> None:
    result = book.match("Silhouette Thread Co")
    assert result.is_new is True
    assert result.method == "new"
    assert result.canonical is None


def test_grey_zone_with_client_picks_candidate_and_records_alias(cfg: Config, categories: Categories) -> None:
    _write_suppliers_csv(cfg.paths.suppliers)
    fake_client = FakeClientChoosesFirst()
    book = SupplierBook(cfg, categories, client=fake_client)

    result = book.match("Silhouette Thread Co", context="thread supplies")
    assert result.method == "model"
    assert result.canonical == "SIL Thread Inc"
    assert result.is_new is False
    assert len(fake_client.calls) == 1

    # recorded as alias, so repeating doesn't call the model again
    result2 = book.match("Silhouette Thread Co")
    assert result2.method == "alias"
    assert len(fake_client.calls) == 1


def test_grey_zone_model_choice_zero_falls_through_to_new(cfg: Config, categories: Categories) -> None:
    _write_suppliers_csv(cfg.paths.suppliers)
    book = SupplierBook(cfg, categories, client=FakeClientChoosesNone())

    result = book.match("Silhouette Thread Co")
    assert result.method == "new"
    assert result.is_new is True
    assert result.canonical is None


def test_grey_zone_claude_error_falls_through_to_new(cfg: Config, categories: Categories) -> None:
    _write_suppliers_csv(cfg.paths.suppliers)
    fake_client = FakeClientErrors()
    book = SupplierBook(cfg, categories, client=fake_client)

    result = book.match("Silhouette Thread Co")
    assert result.method == "new"
    assert result.is_new is True
    # one call for tier-4 adjudication (errors), one for the new-supplier category suggestion
    # (also errors, so default_category stays None)
    assert fake_client.calls == 2
    assert result.default_category is None


# ---------------------------------------------------------------------------
# New suppliers / pending file
# ---------------------------------------------------------------------------


def test_new_supplier_gets_category_from_client_only_if_valid(cfg: Config, categories: Categories) -> None:
    good_client = FakeClientCategory("Fabric")
    book = SupplierBook(cfg, categories, client=good_client)
    result = book.match("Totally Unrelated Vendor Xyz", context="misc purchase")
    assert result.method == "new"
    assert result.default_category == "Fabric"
    assert len(good_client.calls) == 1

    bad_client = FakeClientCategory("Not A Real Category")
    book2 = SupplierBook(cfg, categories, client=bad_client)
    result2 = book2.match("Another Unknown Vendor Abc")
    assert result2.method == "new"
    assert result2.default_category is None


def test_new_supplier_without_client_has_no_category(book: SupplierBook) -> None:
    result = book.match("Totally Unrelated Vendor Xyz")
    assert result.method == "new"
    assert result.default_category is None
    assert result.canonical is None


def test_new_supplier_seen_twice_bumps_occurrences_and_saves_one_row(book: SupplierBook) -> None:
    first = book.match("Totally Unrelated Vendor Xyz", context="first time", source_file="a.pdf")
    second = book.match("totally unrelated vendor xyz!!", context="second time", source_file="b.pdf")
    assert first.is_new is True
    assert second.is_new is True

    book.save()

    with book.cfg.paths.pending_suppliers.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["Occurrences"] == "2"
    # first-seen context/source_file preserved
    assert row["Context"] == "first time"
    assert row["Source file"] == "a.pdf"


def test_empty_name_is_new_without_touching_pending(book: SupplierBook) -> None:
    result = book.match("   ")
    assert result.is_new is True
    assert result.method == "new"
    assert result.score == 0
    assert result.canonical is None

    book.save()
    with book.cfg.paths.pending_suppliers.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == []


def test_never_calls_model_twice_for_same_normalised_new_name(cfg: Config, categories: Categories) -> None:
    client = FakeClientCategory("Fabric")
    book = SupplierBook(cfg, categories, client=client)
    book.match("Totally Unrelated Vendor Xyz")
    book.match("Totally Unrelated Vendor Xyz")
    book.match("TOTALLY UNRELATED VENDOR XYZ!!")
    assert len(client.calls) == 1


def test_names_and_default_category(book: SupplierBook) -> None:
    assert "Mood" in book.names
    assert book.default_category("Mood") == "Fabric"
    assert book.default_category("Nonexistent Supplier") is None


def test_record_alias_noop_when_same_normalised(book: SupplierBook) -> None:
    before = dict(book._alias_index)
    book.record_alias("mood", "Mood")
    assert book._alias_index == before
