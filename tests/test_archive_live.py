"""Live integration test against the SARAO archive.

Skipped unless ``MEERKHIVE_LIVE_TOKENS`` points to a tokens.json file
containing a valid offline refresh token. This is the end-to-end check that
proves the auth + transport work correctly against the production GraphQL
endpoint.

The test is marked ``slow`` so it stays out of the default ``pytest`` run
and only fires when the user opts in with ``-m slow``.
"""

import os
import shutil
from pathlib import Path

import pytest

from meerkhive.archive import query_archive
from meerkhive.auth import KeycloakAuth

LIVE_TOKEN_PATH = os.environ.get("MEERKHIVE_LIVE_TOKENS")


@pytest.fixture
def live_auth(tmp_path: Path, monkeypatch) -> KeycloakAuth:
    if not LIVE_TOKEN_PATH or not Path(LIVE_TOKEN_PATH).exists():
        pytest.skip("Set MEERKHIVE_LIVE_TOKENS to a tokens.json to run live tests.")
    target = tmp_path / "tokens.json"
    shutil.copy(LIVE_TOKEN_PATH, target)
    real_default = KeycloakAuth.default()
    fixed = KeycloakAuth(
        issuer_url=real_default.issuer_url,
        client_id=real_default.client_id,
        scopes=real_default.scopes,
        token_path=target,
        verify_ssl=real_default.verify_ssl,
    )
    monkeypatch.setattr(KeycloakAuth, "default", classmethod(lambda cls, *, verify_ssl=True: fixed))
    return fixed


@pytest.mark.slow
def test_live_query_returns_records(live_auth):
    records = query_archive(fields="CaptureBlockId", limit=5)
    assert len(records) == 5
    assert all("CaptureBlockId" in r for r in records)
