"""Unit tests for :mod:`meerkhive.auth`.

These tests stub out Keycloak with the ``responses`` library so they run
fully offline and in well under a second. The browser-login flow is not
exercised end-to-end (it would require a real browser); we instead patch
``_browser_login`` and assert that ``get_access_token`` falls through to it
when refresh fails.
"""

import json
import os
import time
from pathlib import Path

import pytest
import responses

from meerkhive import auth as auth_module
from meerkhive.auth import (
    KeycloakAuth,
    _is_expired,
    _jwt_exp,
    _load_tokens,
    _pkce_pair,
    _refresh,
    _save_tokens,
    get_access_token,
)
from tests.conftest import make_jwt


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Discovery is process-wide cached; clear it between tests."""
    auth_module._DISCOVERY_CACHE.clear()
    yield
    auth_module._DISCOVERY_CACHE.clear()


def _stub_discovery(auth: KeycloakAuth) -> None:
    responses.add(
        method=responses.GET,
        url=auth.issuer_url + "/.well-known/openid-configuration",
        json={
            "authorization_endpoint": "https://kc.test/auth",
            "token_endpoint": "https://kc.test/token",
        },
    )


# ---------------------------------------------------------------------------
# _jwt_exp / _is_expired
# ---------------------------------------------------------------------------


def test_jwt_exp_extracts_exp_claim():
    token = make_jwt(exp=1234567890)
    assert _jwt_exp(token) == 1234567890


def test_jwt_exp_rejects_non_jwt():
    with pytest.raises(ValueError):
        _jwt_exp("not-a-jwt")


def test_is_expired_handles_missing_token():
    assert _is_expired(None) is True
    assert _is_expired("") is True


def test_is_expired_treats_far_future_as_valid():
    token = make_jwt(exp=int(time.time()) + 3600)
    assert _is_expired(token) is False


def test_is_expired_treats_past_as_expired():
    token = make_jwt(exp=int(time.time()) - 10)
    assert _is_expired(token) is True


def test_is_expired_applies_skew():
    # Token expires in 5 seconds; with a 30 s skew, that's already "expired".
    token = make_jwt(exp=int(time.time()) + 5)
    assert _is_expired(token) is True


def test_is_expired_handles_malformed_token():
    assert _is_expired("clearly.not.a.jwt") is True


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path):
    target = tmp_path / "nested" / "tokens.json"
    payload = {"access_token": "abc", "refresh_token": "def"}
    _save_tokens(target, payload)
    assert _load_tokens(target) == payload


def test_save_tokens_writes_with_restrictive_mode(tmp_path: Path):
    target = tmp_path / "tokens.json"
    _save_tokens(target, {"access_token": "x"})
    mode = os.stat(target).st_mode & 0o777
    # mkstemp creates files mode 0600. We never widen it.
    assert mode == 0o600


def test_save_tokens_is_atomic_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "tokens.json"
    _save_tokens(target, {"access_token": "original"})

    # Inject a failure during the write step. The original file must remain
    # untouched and no stray temp files should be left behind.
    real_replace = os.replace

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _save_tokens(target, {"access_token": "new"})

    monkeypatch.setattr(os, "replace", real_replace)
    assert _load_tokens(target) == {"access_token": "original"}
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tokens-")]
    assert leftovers == []


def test_load_tokens_returns_none_for_missing_file(tmp_path: Path):
    assert _load_tokens(tmp_path / "missing.json") is None


def test_load_tokens_returns_none_for_corrupt_file(tmp_path: Path):
    target = tmp_path / "tokens.json"
    target.write_text("not json")
    assert _load_tokens(target) is None


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_lengths_and_distinctness():
    verifier, challenge = _pkce_pair()
    # 32 bytes of entropy → 43 base64url chars (lower bound from RFC 7636).
    assert len(verifier) >= 43
    # SHA-256 → 32 bytes → 43 base64url chars when padding is stripped.
    assert len(challenge) == 43
    # Calling twice should produce different pairs.
    assert _pkce_pair() != (verifier, challenge)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


@responses.activate
def test_refresh_returns_tokens_on_success(auth: KeycloakAuth):
    _stub_discovery(auth)
    new_tokens = {
        "access_token": make_jwt(),
        "refresh_token": "new-refresh",
    }
    responses.add(method=responses.POST, url="https://kc.test/token", json=new_tokens)
    assert _refresh(auth, "old-refresh") == new_tokens


@responses.activate
def test_refresh_returns_none_on_4xx(auth: KeycloakAuth):
    _stub_discovery(auth)
    responses.add(
        method=responses.POST,
        url="https://kc.test/token",
        json={"error": "invalid_grant"},
        status=400,
    )
    assert _refresh(auth, "stale") is None


# ---------------------------------------------------------------------------
# get_access_token decision tree
# ---------------------------------------------------------------------------


def test_get_access_token_uses_cached_when_valid(auth: KeycloakAuth):
    valid = make_jwt(exp=int(time.time()) + 3600)
    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(json.dumps({"access_token": valid, "refresh_token": "r"}))
    # No HTTP stubbed → if anything tried to hit the network, it would fail.
    assert get_access_token(auth) == valid


@responses.activate
def test_get_access_token_refreshes_when_expired(auth: KeycloakAuth):
    _stub_discovery(auth)
    expired = make_jwt(exp=int(time.time()) - 10)
    new = make_jwt(exp=int(time.time()) + 3600)
    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(json.dumps({"access_token": expired, "refresh_token": "r"}))
    responses.add(
        method=responses.POST,
        url="https://kc.test/token",
        json={"access_token": new, "refresh_token": "r2"},
    )

    assert get_access_token(auth) == new
    persisted = json.loads(auth.token_path.read_text())
    assert persisted["access_token"] == new
    assert persisted["refresh_token"] == "r2"


@responses.activate
def test_get_access_token_force_refresh_skips_cache(auth: KeycloakAuth):
    _stub_discovery(auth)
    cached = make_jwt(exp=int(time.time()) + 3600)
    fresh = make_jwt(exp=int(time.time()) + 3600)
    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(json.dumps({"access_token": cached, "refresh_token": "r"}))
    responses.add(
        method=responses.POST,
        url="https://kc.test/token",
        json={"access_token": fresh, "refresh_token": "r"},
    )
    assert get_access_token(auth, force_refresh=True) == fresh


@responses.activate
def test_get_access_token_falls_back_to_browser_login(auth: KeycloakAuth, monkeypatch):
    _stub_discovery(auth)
    # Cached refresh token is rejected; browser login is the fallback.
    responses.add(
        method=responses.POST,
        url="https://kc.test/token",
        status=400,
        json={"error": "invalid_grant"},
    )
    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(json.dumps({"access_token": "x", "refresh_token": "r"}))

    fresh = {"access_token": make_jwt(), "refresh_token": "r2"}
    monkeypatch.setattr(auth_module, "_browser_login", lambda _auth: fresh)

    assert get_access_token(auth) == fresh["access_token"]
    assert json.loads(auth.token_path.read_text()) == fresh


def test_get_access_token_drives_browser_login_when_no_cache(auth: KeycloakAuth, monkeypatch):
    fresh = {"access_token": make_jwt(), "refresh_token": "r"}
    monkeypatch.setattr(auth_module, "_browser_login", lambda _auth: fresh)
    assert get_access_token(auth) == fresh["access_token"]
    assert json.loads(auth.token_path.read_text()) == fresh
