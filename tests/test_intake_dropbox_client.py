"""Tests for kosaccounts.intake.dropbox_client.RealDropboxClient / client_from_env.

Uses a hand-written fake Dropbox SDK object (`FakeDbx`) built from the real `dropbox.files` /
`dropbox.exceptions` classes so the shapes match exactly what the SDK returns/raises.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path

import dropbox.exceptions
import dropbox.files
import pytest
import requests

from kosaccounts.intake.types import ChangeSet, LongpollResult, RemoteFile

# ---------------------------------------------------------------------------
# puller.py is being written concurrently by another agent; import it if it is
# currently importable, otherwise fall back to a stub module in sys.modules so
# dropbox_client's lazy `from kosaccounts.intake.puller import TransientError`
# resolves to something stable for the rest of this test run.
# ---------------------------------------------------------------------------
try:
    import kosaccounts.intake.puller as _puller_mod

    TransientError = _puller_mod.TransientError
except Exception:  # pragma: no cover - only exercised while puller.py is mid-edit
    _stub = types.ModuleType("kosaccounts.intake.puller")

    class TransientError(Exception):
        pass

    _stub.TransientError = TransientError
    sys.modules["kosaccounts.intake.puller"] = _stub

from kosaccounts.intake.dropbox_client import (  # noqa: E402
    AuthFailure,
    MissingSecret,
    RealDropboxClient,
    client_from_env,
)
import kosaccounts.intake.dropbox_client as dropbox_client_module  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _resolve(value):
    if isinstance(value, Exception):
        raise value
    return value


class FakeDbx:
    """Stands in for `dropbox.Dropbox`. Any *_result attribute may be set to an Exception
    instance instead of a real result to make the corresponding call raise it."""

    def __init__(self) -> None:
        self.list_folder_result = None
        self.list_folder_continue_queue: list = []
        self.latest_cursor_result = None
        self.longpoll_result = None
        self.download_result = None

        self.list_folder_calls: list = []
        self.list_folder_continue_calls: list = []
        self.download_calls: list = []

    def files_list_folder(self, path, recursive=False, **kwargs):
        self.list_folder_calls.append((path, recursive))
        return _resolve(self.list_folder_result)

    def files_list_folder_continue(self, cursor):
        self.list_folder_continue_calls.append(cursor)
        item = self.list_folder_continue_queue.pop(0)
        return _resolve(item)

    def files_list_folder_get_latest_cursor(self, path, recursive=True):
        return _resolve(self.latest_cursor_result)

    def files_list_folder_longpoll(self, cursor, timeout=30):
        return _resolve(self.longpoll_result)

    def files_download_to_file(self, download_path, path, rev=None):
        self.download_calls.append((download_path, path))
        if isinstance(self.download_result, Exception):
            raise self.download_result
        Path(download_path).write_bytes(b"downloaded-bytes")


def _file(name: str, path_lower: str, size: int = 10, content_hash: str = "ab" * 32, id_: str = "id:1"):
    return dropbox.files.FileMetadata(
        name=name,
        id=id_,
        client_modified=datetime(2026, 1, 1),
        server_modified=datetime(2026, 1, 1),
        rev="0123456789",
        size=size,
        path_lower=path_lower,
        path_display=path_lower,
        content_hash=content_hash,
    )


def _folder(name: str, path_lower: str, id_: str = "id:folder"):
    return dropbox.files.FolderMetadata(name=name, id=id_, path_lower=path_lower, path_display=path_lower)


def _deleted(name: str, path_lower: str):
    return dropbox.files.DeletedMetadata(name=name, path_lower=path_lower, path_display=path_lower)


def _client(fake: FakeDbx) -> RealDropboxClient:
    return RealDropboxClient(app_key="key", app_secret="secret", refresh_token="refresh", dbx=fake)


# ---------------------------------------------------------------------------
# list_top_level_files
# ---------------------------------------------------------------------------


def test_list_top_level_files_filters_folders_nested_and_deleted():
    fake = FakeDbx()
    top_file = _file("a.pdf", "/expenses/a.pdf", size=123, content_hash="a" * 64, id_="id:a")
    fake.list_folder_result = dropbox.files.ListFolderResult(
        entries=[
            top_file,
            _folder("sub", "/expenses/sub"),
            _file("nested.pdf", "/expenses/sub/nested.pdf"),
            _deleted("gone.pdf", "/expenses/gone.pdf"),
        ],
        cursor="c1",
        has_more=False,
    )

    client = _client(fake)
    result = client.list_top_level_files("expenses")

    assert result == [
        RemoteFile(
            folder="expenses",
            name="a.pdf",
            path_lower="/expenses/a.pdf",
            path_display="/expenses/a.pdf",
            dropbox_id="id:a",
            size=123,
            content_hash="a" * 64,
        )
    ]
    assert fake.list_folder_calls == [("/expenses", False)]


def test_list_top_level_files_paginates_via_has_more():
    fake = FakeDbx()
    file1 = _file("a.pdf", "/expenses/a.pdf", id_="id:a")
    file2 = _file("b.pdf", "/expenses/b.pdf", id_="id:b")
    fake.list_folder_result = dropbox.files.ListFolderResult(entries=[file1], cursor="c1", has_more=True)
    fake.list_folder_continue_queue = [
        dropbox.files.ListFolderResult(entries=[file2], cursor="c2", has_more=False)
    ]

    client = _client(fake)
    result = client.list_top_level_files("expenses")

    names = sorted(r.name for r in result)
    assert names == ["a.pdf", "b.pdf"]
    assert fake.list_folder_continue_calls == ["c1"]


# ---------------------------------------------------------------------------
# latest_cursor
# ---------------------------------------------------------------------------


def test_latest_cursor_returns_cursor_string():
    fake = FakeDbx()
    fake.latest_cursor_result = dropbox.files.ListFolderGetLatestCursorResult(cursor="the-cursor")
    client = _client(fake)
    assert client.latest_cursor() == "the-cursor"


# ---------------------------------------------------------------------------
# longpoll
# ---------------------------------------------------------------------------


def test_longpoll_maps_changes_and_backoff():
    fake = FakeDbx()
    fake.longpoll_result = dropbox.files.ListFolderLongpollResult(changes=True, backoff=30)
    client = _client(fake)
    assert client.longpoll("cursor", 480) == LongpollResult(changes=True, backoff_seconds=30)


def test_longpoll_backoff_absent_maps_to_zero():
    fake = FakeDbx()
    fake.longpoll_result = dropbox.files.ListFolderLongpollResult(changes=False, backoff=None)
    client = _client(fake)
    assert client.longpoll("cursor", 480) == LongpollResult(changes=False, backoff_seconds=0)


# ---------------------------------------------------------------------------
# list_changes
# ---------------------------------------------------------------------------


def test_list_changes_detects_watched_folders_and_excludes_nested():
    fake = FakeDbx()
    fake.list_folder_continue_queue = [
        dropbox.files.ListFolderResult(
            entries=[
                _file("a.pdf", "/expenses/a.pdf"),  # top-level in watched folder
                _file("nested.pdf", "/expenses/sub/nested.pdf"),  # nested: ignored
                _deleted("old.pdf", "/bank/old.pdf"),  # deleted top-level counts as a change
                _file("x.pdf", "/invoices/x.pdf"),  # unwatched folder: ignored
                _folder("newsub", "/expenses/newsub"),  # top-level folder metadata also counts
            ],
            cursor="c-final",
            has_more=False,
        )
    ]

    client = _client(fake)
    result = client.list_changes("start-cursor", ["expenses", "bank"])

    assert result == ChangeSet(folders_changed={"expenses", "bank"}, new_cursor="c-final", reset=False)
    assert fake.list_folder_continue_calls == ["start-cursor"]


def test_list_changes_paginates_across_multiple_continue_calls():
    fake = FakeDbx()
    fake.list_folder_continue_queue = [
        dropbox.files.ListFolderResult(
            entries=[_file("a.pdf", "/expenses/a.pdf")], cursor="c2", has_more=True
        ),
        dropbox.files.ListFolderResult(
            entries=[_file("b.pdf", "/bank/b.pdf")], cursor="c3", has_more=False
        ),
    ]

    client = _client(fake)
    result = client.list_changes("c1", ["expenses", "bank"])

    assert result.folders_changed == {"expenses", "bank"}
    assert result.new_cursor == "c3"
    assert fake.list_folder_continue_calls == ["c1", "c2"]


def test_list_changes_reset_detection():
    fake = FakeDbx()
    reset_error = dropbox.exceptions.ApiError(
        "req-1", dropbox.files.ListFolderContinueError.reset, None, None
    )
    fake.list_folder_continue_queue = [reset_error]

    client = _client(fake)
    result = client.list_changes("stale-cursor", ["expenses", "bank"])

    assert result == ChangeSet(reset=True)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_creates_parent_directory_and_writes_file(tmp_path: Path):
    fake = FakeDbx()
    client = _client(fake)
    remote = RemoteFile(
        folder="expenses",
        name="file.pdf",
        path_lower="/expenses/file.pdf",
        path_display="/expenses/file.pdf",
        dropbox_id="id:1",
        size=17,
        content_hash="c" * 64,
    )
    dest = tmp_path / "missing" / "nested" / "file.pdf"
    assert not dest.parent.exists()

    client.download(remote, dest)

    assert dest.parent.exists()
    assert dest.read_bytes() == b"downloaded-bytes"
    assert fake.download_calls == [(str(dest), "/expenses/file.pdf")]


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_auth_error_becomes_auth_failure():
    fake = FakeDbx()
    fake.latest_cursor_result = dropbox.exceptions.AuthError("req", None)
    client = _client(fake)
    with pytest.raises(AuthFailure, match="auth-setup"):
        client.latest_cursor()


def test_rate_limit_error_becomes_transient_and_sleeps_backoff(monkeypatch):
    sleeps: list[float] = []

    class _FakeTime:
        @staticmethod
        def sleep(seconds):
            sleeps.append(seconds)

    monkeypatch.setattr(dropbox_client_module, "time", _FakeTime)

    fake = FakeDbx()
    fake.latest_cursor_result = dropbox.exceptions.RateLimitError("req", None, backoff=7)
    client = _client(fake)
    with pytest.raises(TransientError):
        client.latest_cursor()
    assert sleeps == [7]


def test_http_error_becomes_transient():
    fake = FakeDbx()
    fake.latest_cursor_result = dropbox.exceptions.HttpError("req", 503, "boom")
    client = _client(fake)
    with pytest.raises(TransientError):
        client.latest_cursor()


def test_generic_api_error_becomes_transient():
    fake = FakeDbx()
    fake.latest_cursor_result = dropbox.exceptions.ApiError(
        "req", dropbox.files.ListFolderContinueError.other, None, None
    )
    client = _client(fake)
    with pytest.raises(TransientError):
        client.latest_cursor()


def test_request_exception_becomes_transient():
    fake = FakeDbx()
    fake.latest_cursor_result = requests.exceptions.ConnectionError("network broke")
    client = _client(fake)
    with pytest.raises(TransientError):
        client.latest_cursor()


# ---------------------------------------------------------------------------
# client_from_env
# ---------------------------------------------------------------------------


def test_client_from_env_missing_var_names_var_and_no_value():
    environ = {
        "DROPBOX_APP_KEY": "",
        "DROPBOX_APP_SECRET": "super-secret-value",
        "DROPBOX_REFRESH_TOKEN": "sl.refresh-token-value",
    }
    with pytest.raises(MissingSecret) as excinfo:
        client_from_env(environ)

    message = str(excinfo.value)
    assert excinfo.value.name == "DROPBOX_APP_KEY"
    assert "DROPBOX_APP_KEY" in message
    assert "/etc/kosaccounts.env" in message
    assert "super-secret-value" not in message
    assert "sl.refresh-token-value" not in message


def test_client_from_env_missing_var_entirely_absent():
    environ = {"DROPBOX_APP_SECRET": "s", "DROPBOX_REFRESH_TOKEN": "r"}
    with pytest.raises(MissingSecret) as excinfo:
        client_from_env(environ)
    assert excinfo.value.name == "DROPBOX_APP_KEY"


def test_client_from_env_builds_client_without_secrets_in_repr():
    environ = {
        "DROPBOX_APP_KEY": "app-key-value",
        "DROPBOX_APP_SECRET": "app-secret-value",
        "DROPBOX_REFRESH_TOKEN": "sl.refresh-token-value",
    }
    client = client_from_env(environ)
    assert isinstance(client, RealDropboxClient)
    r = repr(client)
    assert "app-key-value" not in r
    assert "app-secret-value" not in r
    assert "sl.refresh-token-value" not in r
