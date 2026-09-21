"""Tests for the `kosaccounts intake` subcommands (kosaccounts/cli.py). Dropbox and the watcher's
process command are always faked/monkeypatched here; nothing talks to the network or shells out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kosaccounts.cli as cli
from kosaccounts.cli import main
from kosaccounts.config import Config
from kosaccounts.intake.puller import BatchResult
from kosaccounts.intake.watcher import Watcher


def _config_path(cfg: Config) -> str:
    return str(cfg.paths.root / "config.toml")


# ---------------------------------------------------------------------------
# intake: no subcommand
# ---------------------------------------------------------------------------


class TestIntakeNoSubcommand:
    def test_prints_help_and_returns_2(self, cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--config", _config_path(cfg), "intake"])
        assert result == 2
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower()
        assert "listen" in captured.out
        assert "reconcile" in captured.out
        assert "watch" in captured.out
        assert "auth-setup" in captured.out
        assert "sweep" in captured.out


# ---------------------------------------------------------------------------
# intake listen
# ---------------------------------------------------------------------------


class TestIntakeListen:
    def test_missing_secret_returns_2_and_names_variable_only(
        self,
        cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
            monkeypatch.delenv(name, raising=False)

        result = main(["--config", _config_path(cfg), "intake", "listen"])
        assert result == 2

        captured = capsys.readouterr()
        assert captured.err.strip() != ""
        assert "DROPBOX_APP_KEY" in captured.err
        # Never echoes a value: there is no "=" followed by a non-empty secret-looking token.
        assert "is not set" in captured.err


# ---------------------------------------------------------------------------
# intake reconcile
# ---------------------------------------------------------------------------


class TestIntakeReconcile:
    def test_prints_summary_and_returns_0(
        self,
        cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_client = object()
        monkeypatch.setattr(cli, "client_from_env", lambda: fake_client)

        results = [
            BatchResult(folder="expenses", downloaded=[1, 2], failed=[], skipped_settle=False),
            BatchResult(folder="bank", downloaded=[], failed=[], skipped_settle=True),
        ]

        def fake_reconcile_all(client, state, intake_cfg):
            assert client is fake_client
            return results

        monkeypatch.setattr(cli.puller, "reconcile_all", fake_reconcile_all)

        result = main(["--config", _config_path(cfg), "intake", "reconcile"])
        assert result == 0

        captured = capsys.readouterr()
        assert "expenses: downloaded 2, failed 0, skipped_settle=False" in captured.out
        assert "bank: downloaded 0, failed 0, skipped_settle=True" in captured.out

    def test_any_failed_returns_1(
        self,
        cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli, "client_from_env", lambda: object())
        results = [BatchResult(folder="expenses", downloaded=[], failed=[(object(), "boom")])]
        monkeypatch.setattr(cli.puller, "reconcile_all", lambda client, state, intake_cfg: results)

        result = main(["--config", _config_path(cfg), "intake", "reconcile"])
        assert result == 1

    def test_lock_held_prints_skipped_and_returns_0(
        self,
        cfg: Config,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli, "client_from_env", lambda: object())
        monkeypatch.setattr(cli.puller, "reconcile_all", lambda client, state, intake_cfg: [])

        result = main(["--config", _config_path(cfg), "intake", "reconcile"])
        assert result == 0
        captured = capsys.readouterr()
        assert "skipped: lock held" in captured.out


# ---------------------------------------------------------------------------
# intake sweep
# ---------------------------------------------------------------------------


class TestIntakeSweep:
    def test_calls_process_once_with_folder(
        self, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def fake_process_once(self, folder: str) -> None:
            calls.append(folder)

        monkeypatch.setattr(Watcher, "process_once", fake_process_once)

        result = main(["--config", _config_path(cfg), "intake", "sweep", "expenses"])
        assert result == 0
        assert calls == ["expenses"]


# ---------------------------------------------------------------------------
# intake auth-setup
# ---------------------------------------------------------------------------


class TestIntakeAuthSetup:
    def test_delegates_to_run_auth_setup(self, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "run_auth_setup", lambda: 7)

        result = main(["--config", _config_path(cfg), "intake", "auth-setup"])
        assert result == 7
