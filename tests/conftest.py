"""Shared fixtures. Tests never touch the real data/, imports/ or output/ folders."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kosaccounts.config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A throwaway copy of the repo layout: config.toml + empty imports/, data/, output/, logs/."""
    shutil.copy(REPO_ROOT / "config.toml", tmp_path / "config.toml")
    for d in ("imports/receipts", "imports/bank", "imports/invoices", "data", "output", "logs"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cfg(tmp_repo: Path) -> Config:
    return load_config(tmp_repo / "config.toml")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
