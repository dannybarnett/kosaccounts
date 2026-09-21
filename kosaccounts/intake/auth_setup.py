"""One-time Dropbox OAuth helper: `python -m kosaccounts intake auth-setup`.

Walks Danny through the offline-access authorization flow and prints a refresh token for him
to paste into `/etc/kosaccounts.env` (owner root, mode 600) himself. This module never writes
anything to disk and never logs; the app key/secret and the resulting refresh token exist only
in memory for the duration of the run and are printed to the terminal once.

Verified against the installed SDK (.venv/lib/python3.12/site-packages/dropbox/oauth.py,
dropbox 12.2.1): `DropboxOAuth2FlowNoRedirect(consumer_key, consumer_secret=None, locale=None,
token_access_type=None, scope=None, include_granted_scopes=None, use_pkce=False, ...)`,
`.start() -> str` (the authorize URL), `.finish(code) -> OAuth2FlowNoRedirectResult` with a
`.refresh_token` attribute (set because `token_access_type="offline"`).
"""

from __future__ import annotations

import getpass
import os
from typing import Callable, Optional

SCOPES = ["files.metadata.read", "files.content.read"]

ENV_APP_KEY = "DROPBOX_APP_KEY"
ENV_APP_SECRET = "DROPBOX_APP_SECRET"
ENV_REFRESH_TOKEN = "DROPBOX_REFRESH_TOKEN"


def _default_flow_factory(app_key: str, app_secret: str):
    import dropbox  # imported lazily so tests never need the real SDK

    return dropbox.DropboxOAuth2FlowNoRedirect(
        app_key,
        app_secret,
        token_access_type="offline",
        scope=SCOPES,
        use_pkce=False,
    )


def run_auth_setup(
    environ: Optional[dict] = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    flow_factory: Optional[Callable[[str, str], object]] = None,
) -> int:
    """Run the interactive OAuth flow. Returns 0 on success, 1 on any failure.

    Writes nothing to disk and logs nothing; the refresh token is returned to the caller only
    via `print_fn`, once, on success.
    """
    environ = os.environ if environ is None else environ
    flow_factory = flow_factory or _default_flow_factory

    print_fn(
        "Before continuing: in the Dropbox App Console, Permissions tab, this app must have\n"
        "  files.metadata.read\n"
        "  files.content.read\n"
        "ticked (and any write scopes left over from rclone unticked), then click Submit.\n"
        "Do this before authorising below -- scopes are baked into the token at authorisation\n"
        "time, so changing them afterwards means re-running this tool."
    )

    app_key = environ.get(ENV_APP_KEY) or ""
    app_key = app_key.strip()
    if not app_key:
        app_key = input_fn("Dropbox app key: ").strip()
    if not app_key:
        print_fn("No app key given; aborting.")
        return 1

    app_secret = environ.get(ENV_APP_SECRET) or ""
    app_secret = app_secret.strip()
    if not app_secret:
        app_secret = getpass_fn("Dropbox app secret: ").strip()
    if not app_secret:
        print_fn("No app secret given; aborting.")
        return 1

    try:
        flow = flow_factory(app_key, app_secret)
    except Exception as exc:  # pragma: no cover - defensive
        print_fn(f"Could not set up the authorization flow: {exc}")
        return 1

    try:
        authorize_url = flow.start()
    except Exception as exc:
        print_fn(f"Could not start the authorization flow: {exc}")
        return 1

    print_fn("")
    print_fn("1. Open this URL in any browser (the Mac is fine) and click Allow:")
    print_fn(f"   {authorize_url}")
    print_fn("2. Copy the authorization code Dropbox shows you.")
    print_fn("")

    code = input_fn("Paste the authorization code here: ").strip()
    if not code:
        print_fn("No code given; aborting.")
        return 1

    try:
        result = flow.finish(code)
    except Exception as exc:
        print_fn(f"Authorization failed: {exc}")
        return 1

    refresh_token = getattr(result, "refresh_token", None)
    if not refresh_token:
        print_fn(
            "Dropbox did not return a refresh token (check that token_access_type=offline "
            "and that this app has offline access enabled)."
        )
        return 1

    print_fn("")
    print_fn(
        "Success. This refresh token is shown once, is not stored anywhere by this tool, and "
        "is never logged. Paste it into /etc/kosaccounts.env (owner root, mode 600) as:"
    )
    print_fn("")
    print_fn(f"{ENV_REFRESH_TOKEN}={refresh_token}")
    print_fn("")
    print_fn("Never commit this value or paste it anywhere other than that file.")
    return 0


def main(argv: Optional[list] = None) -> int:
    return run_auth_setup()
