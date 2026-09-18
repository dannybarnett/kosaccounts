"""Supplier matching against realistic bank-statement descriptors, using the real seeded
suppliers/aliases/categories from data/ (copied into the temp repo, never written back)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kosaccounts.categories import Categories
from kosaccounts.suppliers import SupplierBook, strip_processor_prefix

REPO_DATA = Path(__file__).resolve().parent.parent / "data"
SEED_FILES = ["suppliers.csv", "supplier_aliases.csv", "categories.csv"]

pytestmark = pytest.mark.skipif(
    not all((REPO_DATA / f).exists() for f in SEED_FILES), reason="seeded data/ not present"
)


@pytest.fixture
def book(cfg):
    for f in SEED_FILES:
        shutil.copy(REPO_DATA / f, cfg.paths.data / f)
    return SupplierBook(cfg, Categories(cfg.paths.categories), client=None)


@pytest.mark.parametrize(
    "descriptor, expected",
    [
        ("PACIFIC TRIMMING NEW YORK NY", "Pacific Trimming"),
        ("SQ *MOOD FABRICS", "Mood"),
        ("Mood Fabrics", "Mood"),
        ("UBER *TRIP HELP.UBER.COM", "Uber"),
        ("DELTA AIR LINES", "Delta Airlines"),  # master has both "Delta" and "Delta Airlines"
        ("APPLE.COM/BILL", "Apple"),
        ("EXTRA SPACE STORAGE 1234", "Extra Space"),
        ("CAPITAL ONE MEMBERSHIP FEE", "Capital One"),
        ("AMAZON.COM*2K4Y8 AMZN.COM/BILL", "Amazon.com"),  # tie broken by frequency
        ("TMOBILE*AUTO PAY", "T-Mobile"),  # compact form of alias "T Mobile"
        ("T Mobile", "T-Mobile"),
        ("Guide Fabrics Inc.", "Guide Fabrics"),
        ("Pacific Trimmings", "Pacific Trimming"),
        ("GODADDY.COM", "GoDaddy"),
        ("Recur Debit Card Purchase MUNALUCHI LLC ...1272 NJ", "Munaluchi"),
        ("Debit Card Purchase PACIFIC TRIMMING NEW YORK NY", "Pacific Trimming"),
    ],
)
def test_known_descriptors_match(book, descriptor, expected):
    m = book.match(descriptor)
    assert m.canonical == expected, (descriptor, m)
    assert not m.is_new


@pytest.mark.parametrize(
    "descriptor",
    [
        "GOOD MOOD CAFE",  # contains a one-word supplier but does not lead with it
        "JOE'S PIZZA",
        "NYC TAXI",
        "BLUE DELTA COFFEE",
    ],
)
def test_unrelated_descriptors_are_new(book, descriptor):
    m = book.match(descriptor)
    assert m.is_new, (descriptor, m)


def test_strip_processor_prefix():
    assert strip_processor_prefix(["sq", "mood", "fabrics"]) == ["mood", "fabrics"]
    assert strip_processor_prefix(["tst", "sq", "joe"]) == ["joe"]
    assert strip_processor_prefix(["sq"]) == ["sq"]
    assert strip_processor_prefix(["mood"]) == ["mood"]
