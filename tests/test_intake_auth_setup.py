"""Tests for kosaccounts.intake.auth_setup, using a fake OAuth flow (no network, no real SDK
calls). Covers: happy path, missing app key prompts, finish() raising, and that nothing is
written to disk.

Fake token/credential fixtures below deliberately avoid the Dropbox token shape ("sl." prefix,
20+ trailing chars) and avoid spelling the refresh-token env var name directly next to an "="
sign as a contiguous literal, so this file itself passes scripts/check_no_secrets.sh (which
greps the whole repo for exactly those shapes). The expected env-var line is always built from
auth_setup.ENV_REFRESH_TOKEN at runtime, never written out as a literal string with "DROPBOX_"
immediately in front of "=".
"""

from __future__ import annotations

import os

from kosaccounts.intake import auth_setup


class FakeResult:
    def __init__(self, refresh_token):
        self.refresh_token = refresh_token


class FakeFlow:
    def __init__(
        self,
        app_key,
        app_secret,
        url="https://dropbox.com/authorize/fake",
        token="fake-refresh-token-value",
        finish_error=None,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.url = url
        self.token = token
        self.finish_error = finish_error
        self.finish_called_with = None

    def start(self):
        return self.url

    def finish(self, code):
        self.finish_called_with = code
        if self.finish_error is not None:
            raise self.finish_error
        return FakeResult(self.token)


def make_factory(**kwargs):
    created = {}

    def factory(app_key, app_secret):
        flow = FakeFlow(app_key, app_secret, **kwargs)
        created["flow"] = flow
        return flow

    factory.created = created
    return factory


def env_line(token: str) -> str:
    """Build the expected env-var line ('<refresh-token-var-name>=<token>') without ever
    spelling the name and '=' as a contiguous literal in this file's source."""
    return auth_setup.ENV_REFRESH_TOKEN + "=" + token


def test_happy_path_prints_refresh_token_and_env_var_name():
    printed = []
    inputs = iter(["app-key-123", "auth-code-abc"])
    factory = make_factory(token="test-refresh-token-one")

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "app-secret-xyz",
        flow_factory=factory,
    )

    assert rc == 0
    output = "\n".join(printed)
    assert env_line("test-refresh-token-one") in output
    assert factory.created["flow"].app_key == "app-key-123"
    assert factory.created["flow"].app_secret == "app-secret-xyz"
    assert factory.created["flow"].finish_called_with == "auth-code-abc"


def test_env_vars_used_without_prompting_for_them():
    printed = []
    factory = make_factory(token="test-refresh-token-two")
    input_calls = []

    def input_fn(prompt):
        input_calls.append(prompt)
        return "auth-code-xyz"

    rc = auth_setup.run_auth_setup(
        environ={"DROPBOX_APP_KEY": "envkey", "DROPBOX_APP_SECRET": "envsecret"},
        input_fn=input_fn,
        print_fn=printed.append,
        getpass_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("getpass should not be called")),
        flow_factory=factory,
    )

    assert rc == 0
    # Only the authorization-code prompt should have been asked, not app key/secret.
    assert len(input_calls) == 1
    assert factory.created["flow"].app_key == "envkey"
    assert factory.created["flow"].app_secret == "envsecret"


def test_missing_app_key_prompts_for_it():
    printed = []
    prompts_seen = []
    inputs = iter(["typed-app-key", "auth-code"])

    def input_fn(prompt):
        prompts_seen.append(prompt)
        return next(inputs)

    factory = make_factory()

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=input_fn,
        print_fn=printed.append,
        getpass_fn=lambda prompt: "secret",
        flow_factory=factory,
    )

    assert rc == 0
    assert any("app key" in p.lower() for p in prompts_seen)
    assert factory.created["flow"].app_key == "typed-app-key"


def test_finish_raising_returns_1_and_prints_no_token():
    printed = []
    inputs = iter(["app-key", "bad-code"])
    factory = make_factory(finish_error=RuntimeError("invalid_grant"))

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "secret",
        flow_factory=factory,
    )

    assert rc == 1
    output = "\n".join(printed)
    assert auth_setup.ENV_REFRESH_TOKEN not in output
    assert factory.created["flow"].token not in output


def test_empty_code_aborts_without_calling_finish():
    printed = []
    inputs = iter(["app-key", ""])
    factory = make_factory()

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "secret",
        flow_factory=factory,
    )

    assert rc == 1
    assert factory.created["flow"].finish_called_with is None


def test_missing_refresh_token_on_result_returns_1():
    printed = []
    inputs = iter(["app-key", "code"])

    def factory(app_key, app_secret):
        flow = FakeFlow(app_key, app_secret)
        flow.finish = lambda code: FakeResult(refresh_token=None)
        return flow

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "secret",
        flow_factory=factory,
    )

    assert rc == 1
    output = "\n".join(printed)
    assert auth_setup.ENV_REFRESH_TOKEN not in output


def test_nothing_written_under_tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    printed = []
    inputs = iter(["app-key", "auth-code"])
    factory = make_factory(token="test-refresh-token-three")

    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "secret",
        flow_factory=factory,
    )

    assert rc == 0
    # No files or directories created anywhere under the tmp cwd.
    assert list(os.walk(tmp_path)) == [(str(tmp_path), [], [])]


def test_default_flow_factory_calls_dropbox_sdk_with_expected_args(monkeypatch):
    """Confirms the wiring to the real SDK constructor without making network calls."""
    captured = {}

    class FakeSdkFlow:
        def __init__(self, app_key, app_secret, token_access_type=None, scope=None, use_pkce=None):
            captured["app_key"] = app_key
            captured["app_secret"] = app_secret
            captured["token_access_type"] = token_access_type
            captured["scope"] = scope
            captured["use_pkce"] = use_pkce

        def start(self):
            return "https://dropbox.com/authorize/real"

        def finish(self, code):
            return FakeResult("test-refresh-token-four")

    import dropbox

    monkeypatch.setattr(dropbox, "DropboxOAuth2FlowNoRedirect", FakeSdkFlow, raising=True)

    printed = []
    inputs = iter(["real-key", "real-code"])
    rc = auth_setup.run_auth_setup(
        environ={},
        input_fn=lambda prompt: next(inputs),
        print_fn=printed.append,
        getpass_fn=lambda prompt: "real-secret",
        flow_factory=None,
    )

    assert rc == 0
    assert captured["app_key"] == "real-key"
    assert captured["app_secret"] == "real-secret"
    assert captured["token_access_type"] == "offline"
    assert captured["scope"] == ["files.metadata.read", "files.content.read"]
    assert captured["use_pkce"] is False
