"""Tests for page fetching, retry, and progress reporting.

These cover the fix for the timeout failures described in
https://github.com/JSKenyon/MeerKhive/issues/17. Every test here drives the
coroutines with ``asyncio.run`` against a stub session, so none of them
touch the network.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from gql import GraphQLRequest, gql
from gql.client import AsyncClientSession
from gql.transport.exceptions import (
    TransportConnectionFailed,
    TransportQueryError,
    TransportServerError,
)

from meerkhive import archive
from meerkhive.archive import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AuthenticatedTransport,
    build_ssl_context,
    cancel_and_wait,
    compute_retry_delay,
    fetch_all_pages,
    fetch_page,
    heartbeat,
    is_retryable,
    query_archive,
    validate_limit,
    validate_max_attempts,
    validate_page_size,
    validate_page_timeout,
    warn_if_page_size_exceeds_cap,
)
from meerkhive.auth import KeycloakAuth

# The shape of the coroutine each stub session answers with.
Responder = Callable[..., Awaitable[dict[str, Any]]]

# Sentinel for make_page: derive totalCount from the records supplied. A real
# None is meaningful here, because the schema declares totalCount as nullable.
DERIVE_TOTAL = 0


class StubSession:
    """Minimal stand-in for gql's ``AsyncClientSession``.

    Only ``execute`` is needed; the production code touches nothing else on
    the session.
    """

    def __init__(self, respond: Responder) -> None:
        self._respond = respond

    async def execute(self, request: GraphQLRequest) -> dict[str, Any]:
        # gql 4 carries the variables on the request itself.
        return await self._respond(request, request.variable_values)


def stub_session(respond: Responder) -> AsyncClientSession:
    """Return a StubSession typed as the session the production code expects."""
    return cast(AsyncClientSession, StubSession(respond))


# A real request, because fetch_all_pages now derives a per-page copy from it.
STUB_REQUEST = gql("{ __typename }")


def make_page(
    records: list[dict[str, Any]],
    *,
    has_next: bool,
    cursor: str | None = "next",
    total: int | None = DERIVE_TOTAL,
) -> dict[str, Any]:
    """Build a GraphQL ``observations`` payload with the given records."""
    return {
        "observations": {
            "pageInfo": {
                "totalCount": len(records) if total == DERIVE_TOTAL else total,
                "endCursor": cursor,
                "hasNextPage": has_next,
            },
            "records": records,
        }
    }


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the retry backoff so tests that retry do not wait in real time."""
    monkeypatch.setattr(archive, "INITIAL_RETRY_DELAY_SECONDS", 0.0)


def run_fetch_page(
    respond: Responder,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Drive ``fetch_page`` to completion."""
    return asyncio.run(
        fetch_page(
            stub_session(respond),
            STUB_REQUEST,
            page_number=1,
            max_attempts=max_attempts,
        )
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), True),
        (TransportConnectionFailed("connection reset"), True),
        (TransportServerError("service unavailable", code=503), True),
        (TransportServerError("no status reported", code=None), True),
        (TransportServerError("bad request", code=400), False),
        (TransportServerError("unauthorized", code=401), False),
        (TransportQueryError("no such field"), False),
        (ValueError("not a transport failure"), False),
    ],
)
def test_is_retryable_classifies_failures(error: Exception, expected: bool) -> None:
    assert is_retryable(error) is expected


# --- fetch_page: retry behaviour -----------------------------------------


def test_fetch_page_returns_result_on_the_first_attempt() -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return make_page([{"id": 1}], has_next=False)

    result = run_fetch_page(respond)

    assert attempts == 1
    assert result["observations"]["records"] == [{"id": 1}]


def test_fetch_page_retries_after_timeout_then_succeeds(instant_backoff: None) -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError
        return make_page([{"id": 1}], has_next=False)

    result = run_fetch_page(respond)

    assert attempts == 3
    assert result["observations"]["records"] == [{"id": 1}]


def test_fetch_page_reraises_timeout_once_attempts_are_exhausted(instant_backoff: None) -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise TimeoutError

    with pytest.raises(TimeoutError):
        run_fetch_page(respond, max_attempts=3)

    assert attempts == 3


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0)],
)
def test_compute_retry_delay_doubles_with_each_attempt(attempt: int, expected: float) -> None:
    assert compute_retry_delay(attempt) == pytest.approx(expected)


def test_fetch_page_retries_server_side_errors(instant_backoff: None) -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise TransportServerError("bad gateway", code=502)
        return make_page([{"id": 1}], has_next=False)

    result = run_fetch_page(respond)

    assert attempts == 2
    assert result["observations"]["records"] == [{"id": 1}]


def test_fetch_page_does_not_retry_client_side_errors() -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise TransportServerError("bad request", code=400)

    with pytest.raises(TransportServerError):
        run_fetch_page(respond)

    # A 4xx is deterministic; retrying only burns the timeout budget.
    assert attempts == 1


def test_fetch_page_does_not_retry_graphql_errors() -> None:
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise TransportQueryError("no such field", errors=[{"message": "no such field"}])

    with pytest.raises(TransportQueryError):
        run_fetch_page(respond)

    assert attempts == 1


def test_fetch_page_warns_about_each_retry(
    caplog: pytest.LogCaptureFixture, instant_backoff: None
) -> None:
    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        raise TimeoutError

    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        with pytest.raises(TimeoutError):
            run_fetch_page(respond, max_attempts=2)

    assert any("retrying" in r.message for r in caplog.records)


def test_fetch_page_retries_connection_failures(instant_backoff: None) -> None:
    # gql wraps every non-TransportError - a TCP reset, a dropped connection,
    # a DNS blip - as TransportConnectionFailed, so this is the transient
    # class a long walk is most likely to hit.
    attempts = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise TransportConnectionFailed("connection reset by peer")
        return make_page([{"id": 1}], has_next=False)

    result = run_fetch_page(respond)

    assert attempts == 2
    assert result["observations"]["records"] == [{"id": 1}]


def test_fetch_page_rejects_non_positive_max_attempts() -> None:
    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        return make_page([{"id": 1}], has_next=False)

    with pytest.raises(ValueError, match="max_attempts"):
        run_fetch_page(respond, max_attempts=0)


# --- fetch_all_pages: the pagination walk ----------------------------------


def run_fetch(
    respond: Responder,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    return asyncio.run(
        fetch_all_pages(
            stub_session(respond),
            STUB_REQUEST,
            {"search": "*"},
            page_size=page_size,
            limit=limit,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )
    )


def test_fetch_all_pages_walks_the_cursor_until_exhausted() -> None:
    pages = [
        make_page([{"id": 1}, {"id": 2}], has_next=True, cursor="c1", total=3),
        make_page([{"id": 3}], has_next=False, total=3),
    ]
    seen_cursors: list[str | None] = []

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        seen_cursors.append(variable_values["cursor"])
        return pages[len(seen_cursors) - 1]

    records = run_fetch(respond, page_size=2)

    assert records == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert seen_cursors == [None, "c1"]


def test_fetch_all_pages_requests_the_configured_page_size() -> None:
    requested: list[int] = []

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        requested.append(variable_values["limit"])
        return make_page([{"id": 1}], has_next=False)

    run_fetch(respond, page_size=100, limit=1000)

    assert requested == [100]


def test_fetch_all_pages_trims_the_final_request_to_the_remaining_limit() -> None:
    requested: list[int] = []

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        requested.append(variable_values["limit"])
        n = variable_values["limit"]
        return make_page([{"id": i} for i in range(n)], has_next=True, total=100)

    records = run_fetch(respond, page_size=10, limit=25)

    # Two full pages then a short one; never over-fetch past the limit.
    assert requested == [10, 10, 5]
    assert len(records) == 25


def test_fetch_all_pages_stops_when_server_reports_next_page_but_sends_none() -> None:
    calls = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return make_page([], has_next=True, total=10)

    records = run_fetch(respond, page_size=10, limit=100)

    # Without this guard the cursor never advances and the loop never ends.
    assert records == []
    assert calls == 1


def test_fetch_all_pages_survives_a_null_total_count() -> None:
    # totalCount is declared `Int` (nullable) in the live schema, so the
    # progress log must not assume it is a number.
    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        return make_page([{"id": 1}], has_next=False, total=None)

    records = run_fetch(respond, page_size=10, limit=100)

    assert records == [{"id": 1}]


def test_fetch_all_pages_truncates_when_the_server_over_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The archive already ignores the requested limit above its own cap, so
    # the documented "at most `limit`" guarantee cannot rest on the server.
    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        return make_page([{"id": i} for i in range(100)], has_next=False)

    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        records = run_fetch(respond, page_size=10, limit=25)

    assert len(records) == 25
    assert any("truncating" in r.message.lower() for r in caplog.records)


def test_fetch_all_pages_does_not_warn_when_the_server_honours_the_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        return make_page([{"id": 1}], has_next=False)

    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        records = run_fetch(respond, page_size=10, limit=25)

    assert records == [{"id": 1}]
    assert not any("truncating" in r.message.lower() for r in caplog.records)


def test_fetch_all_pages_logs_progress_against_the_total(caplog: pytest.LogCaptureFixture) -> None:
    pages = [
        make_page([{"id": 1}], has_next=True, cursor="c1", total=2),
        make_page([{"id": 2}], has_next=False, total=2),
    ]
    calls = 0

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return pages[calls - 1]

    with caplog.at_level(logging.INFO, logger="meerkhive.archive"):
        run_fetch(respond, page_size=1, limit=100)

    assert any("1/2" in r.message for r in caplog.records)
    assert any("2/2" in r.message for r in caplog.records)


# --- page size validation ---------------------------------------------------


def test_validate_page_size_does_not_warn_about_the_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Validation must stay side-effect free: the CLI validates too, so a
    # warning here would be emitted twice for one command.
    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        validate_page_size(500)

    assert caplog.records == []


def test_warn_if_page_size_exceeds_cap_warns_above_the_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        warn_if_page_size_exceeds_cap(500)

    assert any("100" in r.message for r in caplog.records)


def test_warn_if_page_size_exceeds_cap_is_quiet_at_the_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        warn_if_page_size_exceeds_cap(MAX_PAGE_SIZE)

    assert caplog.records == []


@pytest.mark.parametrize("page_size", [0, -1])
def test_validate_page_size_rejects_non_positive(page_size: int) -> None:
    with pytest.raises(ValueError, match="page_size"):
        validate_page_size(page_size)


@pytest.mark.parametrize("max_attempts", [0, -1])
def test_validate_max_attempts_rejects_non_positive(max_attempts: int) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        validate_max_attempts(max_attempts)


@pytest.mark.parametrize("page_timeout", [0, -1.0])
def test_validate_page_timeout_rejects_non_positive(page_timeout: float) -> None:
    with pytest.raises(ValueError, match="page_timeout"):
        validate_page_timeout(page_timeout)


# --- heartbeat --------------------------------------------------------------


def test_heartbeat_reports_while_an_operation_is_still_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def slow_operation() -> None:
        async with heartbeat("page 1", interval=0.01):
            await asyncio.sleep(0.05)

    with caplog.at_level(logging.INFO, logger="meerkhive.archive"):
        asyncio.run(slow_operation())

    assert any("still waiting" in r.message.lower() for r in caplog.records)


def test_heartbeat_stays_quiet_when_the_operation_is_fast(caplog: pytest.LogCaptureFixture) -> None:
    async def fast_operation() -> None:
        async with heartbeat("page 1", interval=10):
            return None

    with caplog.at_level(logging.INFO, logger="meerkhive.archive"):
        asyncio.run(fast_operation())

    assert not any("still waiting" in r.message.lower() for r in caplog.records)


def test_heartbeat_does_not_swallow_cancellation_of_the_host_task() -> None:
    # The teardown must not suppress a CancelledError aimed at the caller:
    # the docs invite wrapping a query in asyncio.timeout(), and a swallowed
    # cancellation would let the walk continue past its deadline.
    async def watched_work() -> str:
        async with heartbeat("page 1", interval=100):
            await asyncio.sleep(5)
        return "completed"

    async def cancel_mid_flight() -> str:
        task = asyncio.create_task(watched_work())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(cancel_mid_flight()) == "cancelled"


def test_cancel_and_wait_absorbs_the_cancelled_task_s_own_error() -> None:
    async def forever() -> None:
        await asyncio.sleep(100)

    async def stop_it() -> str:
        task = asyncio.create_task(forever())
        await asyncio.sleep(0.01)
        await cancel_and_wait(task)
        return "completed"

    assert asyncio.run(stop_it()) == "completed"


def test_cancel_and_wait_still_propagates_cancellation_of_the_caller() -> None:
    # A task that is slow to finish cancelling widens the window in which a
    # cancellation aimed at the caller lands inside the teardown itself. With
    # contextlib.suppress(CancelledError) in place of gather, that outer
    # cancellation is swallowed and the caller wrongly reports success.
    async def slow_to_cancel() -> None:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    async def caller() -> str:
        task = asyncio.create_task(slow_to_cancel())
        await asyncio.sleep(0.01)
        await cancel_and_wait(task)
        return "completed"

    async def cancel_during_teardown() -> str:
        task = asyncio.create_task(caller())
        # Land the cancellation while `caller` is inside cancel_and_wait.
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(cancel_during_teardown()) == "cancelled"


@pytest.mark.parametrize("limit", [0, -1])
def test_validate_limit_rejects_non_positive(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        validate_limit(limit)


def test_fetch_all_pages_stops_when_a_next_page_has_no_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # endCursor is declared `String` (nullable). Reverting to None would
    # restart the walk from the beginning and accumulate duplicates.
    cursors: list[str | None] = []

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        assert variable_values is not None
        cursors.append(variable_values["cursor"])
        return make_page([{"id": len(cursors)}], has_next=True, cursor=None, total=100)

    with caplog.at_level(logging.WARNING, logger="meerkhive.archive"):
        records = run_fetch(respond, page_size=1, limit=5)

    assert cursors == [None]
    assert records == [{"id": 1}]
    assert any("cursor" in r.message.lower() for r in caplog.records)


def test_fetch_all_pages_sends_a_distinct_request_per_page() -> None:
    # gql 4 deprecates passing variable_values to execute, and the shim
    # mutates the shared request, so each page needs its own.
    # The request objects themselves are retained rather than their id()s: a
    # per-page request is garbage by the time the next one is built, and CPython
    # is free to hand the second one the same address.
    seen: list[tuple[GraphQLRequest, dict[str, Any] | None]] = []
    pages = [
        make_page([{"id": 1}], has_next=True, cursor="c1", total=2),
        make_page([{"id": 2}], has_next=False, total=2),
    ]

    async def respond(
        request: GraphQLRequest, variable_values: dict[str, Any] | None
    ) -> dict[str, Any]:
        seen.append((request, variable_values))
        return pages[len(seen) - 1]

    run_fetch(respond, page_size=1, limit=100)

    assert seen[0][0] is not seen[1][0], "each page must get its own request object"
    assert seen[0][0] is not STUB_REQUEST, "the shared request must not be executed directly"
    assert seen[0][1] is not None and seen[0][1]["cursor"] is None
    assert seen[1][1] is not None and seen[1][1]["cursor"] == "c1"
    assert STUB_REQUEST.variable_values is None, "the shared request must not be mutated"


# --- transport timeout wiring -----------------------------------------------


def make_transport(**kwargs: Any) -> AuthenticatedTransport:
    return AuthenticatedTransport(
        url="https://example.invalid/graphql",
        auth=KeycloakAuth.default(),
        ssl_context=build_ssl_context(verify=True),
        **kwargs,
    )


def test_transport_applies_the_request_timeout_to_the_session() -> None:
    # aiohttp's ClientSession caps a request at 300 s by default, so a larger
    # page_timeout would be silently ineffective unless it is forwarded.
    transport = make_transport(request_timeout=420.5)

    assert transport.client_session_args is not None
    assert transport.client_session_args["timeout"].total == pytest.approx(420.5)


def test_transport_leaves_the_session_default_when_no_timeout_is_given() -> None:
    transport = make_transport()

    assert "timeout" not in (transport.client_session_args or {})


def test_query_archive_forwards_the_page_timeout_to_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class RecordingTransport:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class ClientOpened(RuntimeError):
        pass

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            raise ClientOpened

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(archive, "get_access_token", lambda *a, **k: "token")
    monkeypatch.setattr(archive, "AuthenticatedTransport", RecordingTransport)
    monkeypatch.setattr(archive, "Client", FakeClient)

    with pytest.raises(ClientOpened):
        query_archive(page_timeout=420.5, limit=1)

    assert captured["request_timeout"] == pytest.approx(420.5)
