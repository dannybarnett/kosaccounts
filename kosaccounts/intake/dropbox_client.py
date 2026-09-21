"""Real DropboxClient (types.py Protocol) built on the official `dropbox` SDK.

Secrets (app key, app secret, refresh token) are read once, from the environment
(`client_from_env`) or passed in directly, and handed straight to `dropbox.Dropbox`. Nothing
here stores them on `self` beyond what the SDK itself keeps, and `RealDropboxClient.__repr__`
never includes them.

Timeout note: `files_list_folder_longpoll` blocks for up to `timeout_seconds` plus up to ~90s of
server jitter, so the underlying `requests` session's timeout must be comfortably longer than
that or the HTTP call itself times out first. The default `timeout=600` here already covers the
project default `longpoll_timeout_seconds=480` (480 + 120 headroom = 600). If a caller configures
a larger `longpoll_timeout_seconds`, it should construct `RealDropboxClient` with
`timeout=longpoll_timeout_seconds + 120` (or more) to match.

Error translation: every SDK call funnels through `_call`, which maps:
  - `dropbox.exceptions.AuthError`                                -> `AuthFailure` (not transient)
  - `dropbox.exceptions.RateLimitError` (sleeps `.backoff` first) -> TransientError
  - `dropbox.exceptions.InternalServerError` / `HttpError`        -> TransientError
  - `dropbox.exceptions.ApiError` (generic)                       -> TransientError
  - `requests.exceptions.RequestException`                        -> TransientError
`TransientError` lives in `kosaccounts.intake.puller` and is imported lazily inside the
translation function, so this module can be imported even while puller.py is still being written
elsewhere.

`list_changes` is the one exception to blanket translation: a `ListFolderContinueError` whose
`.is_reset()` is true is not an error from the caller's point of view, it means "rebuild the
cursor and reconcile" (`ChangeSet(reset=True)`), so it is intercepted before generic translation.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Mapping

import dropbox
import dropbox.exceptions
import dropbox.files
import requests

from kosaccounts.intake.types import ChangeSet, LongpollResult, RemoteFile

logger = logging.getLogger("kosaccounts.intake.dropbox_client")

_ENV_VAR_NAMES = ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")

_AUTH_FAILURE_MESSAGE = (
    "Dropbox rejected the stored credentials (refresh token revoked or expired, or the app key/"
    "secret changed). Re-run `python -m kosaccounts intake auth-setup` and paste the new refresh "
    "token into /etc/kosaccounts.env."
)


class MissingSecret(ValueError):
    """A required Dropbox secret environment variable is missing or empty.

    The message names the variable but never a value (there is never a value to echo: the
    variable is unset or blank).
    """

    def __init__(self, name: str):
        super().__init__(
            f"{name} is not set (or is empty). Expected in /etc/kosaccounts.env, loaded by "
            f"systemd's EnvironmentFile=, or otherwise present in the process environment."
        )
        self.name = name


class AuthFailure(RuntimeError):
    """Dropbox rejected the credentials. Not transient: retrying will not help."""


def _translate_and_raise(exc: Exception) -> None:
    """Raise the kosaccounts-facing equivalent of `exc`. Always raises; never returns."""
    from kosaccounts.intake.puller import TransientError

    if isinstance(exc, dropbox.exceptions.AuthError):
        raise AuthFailure(_AUTH_FAILURE_MESSAGE) from exc
    if isinstance(exc, dropbox.exceptions.RateLimitError):
        backoff = getattr(exc, "backoff", None)
        if backoff:
            time.sleep(backoff)
        raise TransientError(f"Dropbox rate limited us: {exc}") from exc
    if isinstance(exc, (dropbox.exceptions.InternalServerError, dropbox.exceptions.HttpError)):
        raise TransientError(f"Dropbox HTTP error: {exc}") from exc
    if isinstance(exc, dropbox.exceptions.ApiError):
        raise TransientError(f"Dropbox API error: {exc}") from exc
    if isinstance(exc, requests.exceptions.RequestException):
        raise TransientError(f"Network error talking to Dropbox: {exc}") from exc
    raise exc


def _call(fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)`, translating any SDK/network error it raises."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        _translate_and_raise(exc)
        raise AssertionError("unreachable")  # _translate_and_raise always raises


def _watched_folder_for_entry(path_lower: str | None, watched: set[str]) -> str | None:
    """The watched folder `path_lower` sits directly inside, or None.

    "Directly inside" means exactly one path component after the leading slash and the folder
    name, i.e. `path_lower.count("/") == 2` (e.g. "/expenses/x.pdf", not "/expenses/sub/x.pdf"
    and not "/expenses").
    """
    if not path_lower:
        return None
    if path_lower.count("/") != 2:
        return None
    folder = path_lower.split("/")[1]
    return folder if folder in watched else None


class RealDropboxClient:
    """DropboxClient (types.py) backed by `dropbox.Dropbox`.

    Pass `dbx=` to inject a fake SDK object in tests instead of building a real one.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        refresh_token: str,
        timeout: float = 600,
        dbx=None,
    ):
        self._timeout = timeout
        if dbx is not None:
            self._dbx = dbx
        else:
            self._dbx = dropbox.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret,
                timeout=timeout,
            )

    def __repr__(self) -> str:  # never include secrets
        return f"RealDropboxClient(timeout={self._timeout!r})"

    def list_top_level_files(self, folder: str) -> list[RemoteFile]:
        prefix = f"/{folder}/"
        entries = []
        result = _call(self._dbx.files_list_folder, "/" + folder, recursive=False)
        entries.extend(result.entries)
        while result.has_more:
            result = _call(self._dbx.files_list_folder_continue, result.cursor)
            entries.extend(result.entries)

        out: list[RemoteFile] = []
        for entry in entries:
            if not isinstance(entry, dropbox.files.FileMetadata):
                continue
            path_lower = entry.path_lower or ""
            if not path_lower.startswith(prefix):
                continue
            remainder = path_lower[len(prefix):]
            if not remainder or "/" in remainder:
                continue  # defensive: only exactly one component after "/<folder>/"
            out.append(
                RemoteFile(
                    folder=folder,
                    name=entry.name,
                    path_lower=entry.path_lower,
                    path_display=entry.path_display,
                    dropbox_id=entry.id,
                    size=entry.size,
                    content_hash=entry.content_hash,
                )
            )
        return out

    def latest_cursor(self) -> str:
        result = _call(self._dbx.files_list_folder_get_latest_cursor, "", recursive=True)
        return result.cursor

    def longpoll(self, cursor: str, timeout_seconds: int) -> LongpollResult:
        result = _call(self._dbx.files_list_folder_longpoll, cursor, timeout=timeout_seconds)
        return LongpollResult(changes=bool(result.changes), backoff_seconds=result.backoff or 0)

    def list_changes(self, cursor: str, watched_folders: list[str]) -> ChangeSet:
        watched = set(watched_folders)
        folders_changed: set[str] = set()
        new_cursor = cursor
        while True:
            try:
                result = self._dbx.files_list_folder_continue(new_cursor)
            except dropbox.exceptions.ApiError as exc:
                error = exc.error
                if isinstance(error, dropbox.files.ListFolderContinueError) and error.is_reset():
                    logger.warning("Dropbox cursor reset; caller must rebuild and reconcile")
                    return ChangeSet(reset=True)
                _translate_and_raise(exc)
                raise AssertionError("unreachable")
            except Exception as exc:
                _translate_and_raise(exc)
                raise AssertionError("unreachable")

            for entry in result.entries:
                folder = _watched_folder_for_entry(entry.path_lower, watched)
                if folder:
                    folders_changed.add(folder)

            new_cursor = result.cursor
            if not result.has_more:
                break

        return ChangeSet(folders_changed=folders_changed, new_cursor=new_cursor, reset=False)

    def download(self, remote: RemoteFile, dest: Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _call(self._dbx.files_download_to_file, str(dest), remote.path_lower)


def client_from_env(environ: Mapping[str, str] = os.environ) -> RealDropboxClient:
    """Build a RealDropboxClient from DROPBOX_APP_KEY/DROPBOX_APP_SECRET/DROPBOX_REFRESH_TOKEN.

    Raises MissingSecret(name) if any is absent or empty; never echoes values.
    """

    def _get(name: str) -> str:
        value = environ.get(name, "") or ""
        if not value:
            raise MissingSecret(name)
        return value

    app_key = _get("DROPBOX_APP_KEY")
    app_secret = _get("DROPBOX_APP_SECRET")
    refresh_token = _get("DROPBOX_REFRESH_TOKEN")
    return RealDropboxClient(app_key=app_key, app_secret=app_secret, refresh_token=refresh_token)
