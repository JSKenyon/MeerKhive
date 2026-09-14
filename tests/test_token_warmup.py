"""The access token must be acquired before the gql client opens.

``get_access_token`` may drive an interactive browser login that waits up to
five minutes for a human, while gql applies ``execute_timeout`` to every
request the session makes. Acquiring the token inside that scope means the
login is cancelled at the deadline and then retried, opening a fresh browser
tab each time. See https://github.com/JSKenyon/MeerKhive/issues/17
"""

from typing import Any

import pytest

from meerkhive import archive
from meerkhive.archive import fetch_fields, query_archive


class ClientOpened(RuntimeError):
    """Raised by the fake client so the call stops before any network use."""


@pytest.fixture
def call_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the order of token acquisition versus client construction."""
    order: list[str] = []

    def fake_get_access_token(auth: object, *, force_refresh: bool = False) -> str:
        order.append("token")
        return "fake-token"

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            order.append("client")

        async def __aenter__(self) -> Any:
            raise ClientOpened

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(archive, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(archive, "Client", FakeClient)
    return order


def test_query_archive_acquires_the_token_before_opening_the_client(
    call_order: list[str],
) -> None:
    with pytest.raises(ClientOpened):
        query_archive(limit=1)

    assert call_order == ["token", "client"]


def test_fetch_fields_acquires_the_token_before_opening_the_client(
    call_order: list[str],
) -> None:
    with pytest.raises(ClientOpened):
        fetch_fields()

    assert call_order == ["token", "client"]
