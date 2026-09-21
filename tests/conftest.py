"""Shared fixtures. Tests never touch the real data/, imports/ or output/ folders."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kosaccounts.config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A throwaway copy of the repo layout: config.toml + empty imports/, data/, output/, logs/."""
    # Copy config.toml but point [intake] kos_root at a throwaway folder: the real value is an
    # absolute path under /mnt/storage and tests must never create state there.
    text = (REPO_ROOT / "config.toml").read_text()
    text = re.sub(r'^kos_root = .*$', f'kos_root = "{(tmp_path / "kos").as_posix()}"', text, flags=re.M)
    (tmp_path / "config.toml").write_text(text)
    for d in ("imports/expenses", "imports/bank", "imports/invoices", "data", "output", "logs"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cfg(tmp_repo: Path) -> Config:
    return load_config(tmp_repo / "config.toml")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
