"""Shared fixtures and helpers for the test suite."""

import base64
import json
import time
from pathlib import Path

import pytest

from meerkhive.auth import KeycloakAuth


def make_jwt(*, exp: int | None = None) -> str:
    """Build an unsigned JWT with the given ``exp`` claim.

    The signature segment is left as a placeholder; :mod:`meerkhive.auth`
    deliberately does not validate signatures (it only reads ``exp`` to decide
    whether to refresh).
    """
    if exp is None:
        exp = int(time.time()) + 3600
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@pytest.fixture
def auth(tmp_path: Path) -> KeycloakAuth:
    """A ``KeycloakAuth`` whose token file lives in a clean temp dir."""
    return KeycloakAuth(
        issuer_url="https://kc.test/realms/TEST",
        client_id="test-client",
        token_path=tmp_path / "tokens.json",
    )
