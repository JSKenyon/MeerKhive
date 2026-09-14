"""Cursor pagination for the archive's ``observations`` field.

The archive is slow and its latency varies widely, so walking a large result
set is the part of a query most likely to fail. This module owns that walk and
the policy that keeps it alive: how many records to ask for at a time, how long
to wait for one page, which failures are worth retrying, and how to report that
a slow request has not hung. See https://github.com/JSKenyon/MeerKhive/issues/17

Nothing here is part of the public API — :mod:`meerkhive.archive` is the entry
point, and its keyword arguments are how a caller changes any of the defaults
below. The module deliberately knows nothing about authentication, transports
or schema introspection: it is handed an open session and a parsed request, so
it can be exercised against a stub without touching the network.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from gql import GraphQLRequest
from gql.client import AsyncClientSession
from gql.transport.exceptions import TransportConnectionFailed, TransportServerError

logger = logging.getLogger(__name__)


# The archive silently caps the records it returns per request at 100:
# requesting 200, 500 or 1000 all yield exactly 100 records, for the same
# ~6 s of wall-clock as a request for 25. The dominant cost is per-request
# rather than per-record, so fetching 100 at a time is roughly 3.7x faster
# over a large query than the 25 used previously, and exposes the walk to the
# latency tail four times less often.
# See https://github.com/JSKenyon/MeerKhive/issues/17
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE

# gql defaults ``execute_timeout`` to 10 s, which sits barely above the
# archive's observed median page latency (~6 s) and well inside its tail.
# 120 s is far enough out that only a genuinely stuck request trips it.
DEFAULT_PAGE_TIMEOUT = 120.0

# Total attempts per page, including the first. Retrying at the call level
# instead would be near-useless: if one attempt at an N-page walk fails with
# probability p, whole-query retries still fail with probability p**k.
DEFAULT_MAX_ATTEMPTS = 3
INITIAL_RETRY_DELAY_SECONDS = 1.0

# These two defaults set a theoretical ceiling on how long a query can run
# before giving up: pages x max_attempts x page_timeout, so roughly an hour for
# a 1000-record query. That bound is never approached in practice — a page
# takes about 6 s, making the same query about 70 s — because reaching it would
# need every attempt at every page to hang for the full timeout. There is
# deliberately no overall deadline: a caller that needs one can wrap the query
# in asyncio.timeout().

# How often to report that a single request is still in flight. Without this
# a slow page is indistinguishable from a hang for up to ``DEFAULT_PAGE_TIMEOUT``.
HEARTBEAT_INTERVAL_SECONDS = 15.0

# Failure types worth catching. ``TransportConnectionFailed`` covers the whole
# transient-connection class, because gql's aiohttp transport wraps every
# non-``TransportError`` exception — a TCP reset, a dropped connection, a DNS
# blip — in it. ``TransportQueryError`` is deliberately absent: a GraphQL-level
# error is deterministic, so retrying only wastes time. Membership here is
# necessary but not sufficient; :func:`is_retryable` decides per instance.
RETRYABLE_ERRORS = (TimeoutError, TransportServerError, TransportConnectionFailed)


def validate_page_size(page_size: int) -> None:
    """Check a requested page size.

    Deliberately side-effect free, so it is safe to call from more than one
    layer: the CLI validates to report a usage error, and the query entry
    points validate for direct API callers.

    Args:
        page_size: Number of records to request per page.

    Raises:
        ValueError: If ``page_size`` is less than one.
    """
    if page_size < 1:
        raise ValueError(f"Invalid value for 'page_size': {page_size!r}. Must be at least 1.")


def validate_limit(limit: int) -> None:
    """Check a requested record limit.

    Args:
        limit: Maximum records to return across all pages.

    Raises:
        ValueError: If ``limit`` is less than one.
    """
    if limit < 1:
        raise ValueError(f"Invalid value for 'limit': {limit!r}. Must be at least 1.")


def validate_page_timeout(page_timeout: float) -> None:
    """Check a requested per-page timeout.

    Args:
        page_timeout: Seconds allowed for a single page request.

    Raises:
        ValueError: If ``page_timeout`` is not greater than zero.
    """
    if page_timeout <= 0:
        raise ValueError(
            f"Invalid value for 'page_timeout': {page_timeout!r}. Must be greater than 0."
        )


def validate_max_attempts(max_attempts: int) -> None:
    """Check a requested retry depth.

    Args:
        max_attempts: Total attempts per page, including the first.

    Raises:
        ValueError: If ``max_attempts`` is less than one.
    """
    if max_attempts < 1:
        raise ValueError(f"Invalid value for 'max_attempts': {max_attempts!r}. Must be at least 1.")


def warn_if_page_size_exceeds_cap(page_size: int) -> None:
    """Warn when the archive will return fewer records than were asked for.

    Deliberately a warning rather than a clamp: the server is the authority on
    its own cap, and silently rewriting the request would hide a future change
    to it. The walk is correct either way, because it advances by the number of
    records actually returned.

    Args:
        page_size: Number of records requested per page.
    """
    if page_size > MAX_PAGE_SIZE:
        logger.warning(
            f"Requested page_size={page_size} exceeds the archive's cap of {MAX_PAGE_SIZE}; "
            f"the server will return at most {MAX_PAGE_SIZE} records per request."
        )


def compute_retry_delay(attempt: int) -> float:
    """Return the seconds to wait before retrying, after a failed attempt.

    The delay doubles each time, so a burst of failures backs off rather than
    hammering an archive that is already struggling.

    Args:
        attempt: The one-based number of the attempt that just failed.

    Returns:
        Seconds to wait before the next attempt.
    """
    return INITIAL_RETRY_DELAY_SECONDS * 2 ** (attempt - 1)


def is_retryable(error: Exception) -> bool:
    """Report whether a failed request is worth trying again.

    Timeouts and connection failures are transient by nature. A server-side
    (5xx) response may be too. A client-side (4xx) response is deterministic,
    so retrying it only burns the timeout budget. A 401 reaches this point only
    after :class:`AuthenticatedTransport` has already retried it once with a
    freshly minted token, so a second one is an auth failure rather than a
    stale token, and retrying it further would not help.

    Args:
        error: The exception raised by the failed request.

    Returns:
        ``True`` if the request should be retried.
    """
    if isinstance(error, TransportServerError):
        # gql leaves ``code`` unset when the server reported no status, which
        # is not evidence that the failure is deterministic.
        return error.code is None or error.code >= 500

    return isinstance(error, RETRYABLE_ERRORS)


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """Cancel a task and wait for it to finish.

    ``gather(..., return_exceptions=True)`` absorbs the task's own
    ``CancelledError`` while still letting a cancellation aimed at the caller
    propagate. ``contextlib.suppress(asyncio.CancelledError)`` would swallow
    both, silently stranding a caller that wrapped us in a deadline.

    Args:
        task: The task to cancel.
    """
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@contextlib.asynccontextmanager
async def heartbeat(
    task_name: str,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[None, None]:
    """Log periodic progress while the wrapped operation is in flight.

    Args:
        task_name: Human-readable name of the task, e.g. ``"page 3"``.
        interval: Seconds between reports.

    Yields:
        ``None``; the heartbeat runs for the lifetime of the ``async with``.
    """

    async def report_progress() -> None:
        start = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            logger.info(f"Still waiting on {task_name} ({time.monotonic() - start:.0f}s elapsed).")

    reporter_task = asyncio.create_task(report_progress())
    try:
        yield
    finally:
        # Wait for the cancellation so the reporter cannot outlive this block
        # and log against work that has already finished.
        await cancel_and_wait(reporter_task)


async def fetch_page(
    session: AsyncClientSession,
    request: GraphQLRequest,
    *,
    page_number: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Execute a single page, retrying transient failures with backoff.

    Args:
        session: An open gql session against the archive.
        request: The GraphQL request to execute, carrying this page's
            variable values.
        page_number: One-based page number, used only for logging.
        max_attempts: Total attempts, including the first.

    Returns:
        The decoded GraphQL response for this page.

    Raises:
        TransportConnectionFailed: If every attempt fails to reach the
            archive. A page that exceeds its deadline usually arrives this
            way: the aiohttp session's own timeout fires marginally before
            gql's, and gql wraps it because it is not a ``TransportError``.
        TimeoutError: If gql's deadline wins the race instead.
        TransportServerError: On a client-side (4xx) error, or if every
            attempt fails with a server-side error.
        TransportQueryError: If the archive returns GraphQL-level errors.
        ValueError: If ``max_attempts`` is less than one.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            async with heartbeat(f"page {page_number}"):
                return await session.execute(request)
        except RETRYABLE_ERRORS as e:
            if not is_retryable(e):
                raise
            if attempt == max_attempts:
                logger.error(f"Page {page_number} failed after {max_attempts} attempts.")
                raise

            delay = compute_retry_delay(attempt)
            logger.warning(
                f"Page {page_number} failed ({type(e).__name__}); retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{max_attempts})."
            )
            await asyncio.sleep(delay)

    # Reachable only when the loop body never ran, i.e. max_attempts < 1.
    raise ValueError(f"Invalid value for 'max_attempts': {max_attempts!r}. Must be at least 1.")


async def fetch_all_pages(
    session: AsyncClientSession,
    request: GraphQLRequest,
    variables: dict[str, Any],
    *,
    page_size: int,
    limit: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Walk the ``observations`` cursor until the limit or the results run out.

    Args:
        session: An open gql session against the archive.
        request: The parsed GraphQL request to execute.
        variables: Variable values for the request. ``limit`` and ``cursor``
            are added per page.
        page_size: Records to request per page.
        limit: Maximum records to return across all pages.
        max_attempts: Total attempts per page, including the first.

    Returns:
        The accumulated records, at most ``limit`` of them.
    """
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    page_number = 0

    while True:
        page_number += 1
        page_variables = {
            **variables,
            "cursor": cursor,
            # Trim the final request so a large page size cannot overshoot a
            # small limit.
            "limit": min(page_size, limit - len(records)),
        }
        # A fresh request per page: gql 4 deprecates passing variable_values
        # to execute, and its compatibility shim assigns them onto the shared
        # request object, so pages would otherwise contend for one payload.
        result = await fetch_page(
            session,
            GraphQLRequest(request, variable_values=page_variables),
            page_number=page_number,
            max_attempts=max_attempts,
        )

        page_records = result["observations"]["records"]
        page_info = result["observations"]["pageInfo"]
        records.extend(page_records)

        # ``totalCount`` is nullable in the schema, so the denominator is
        # omitted rather than assumed — a log line must never break a query.
        total_count = page_info["totalCount"]
        if total_count is None:
            logger.info(f"Fetched {len(records)} records (page {page_number}).")
        else:
            logger.info(
                f"Fetched {len(records)}/{min(total_count, limit)} records (page {page_number})."
            )

        # Guard against a broken server that reports hasNextPage=True but
        # returns no records — without this the cursor never advances and the
        # loop never terminates.
        if not page_records or not page_info["hasNextPage"] or len(records) >= limit:
            break

        cursor = page_info["endCursor"]

        # A null endCursor would send the next request back to the start of
        # the result set, accumulating duplicates until the limit is reached.
        if cursor is None:
            logger.warning(
                f"Archive reported another page after page {page_number} but returned no "
                "cursor; stopping the walk."
            )
            break

    # The walk asks for no more than it needs, but the archive is known to
    # ignore the requested limit above its own cap, so honour the documented
    # guarantee here rather than trusting the server to. The warning matters:
    # a silent trim would hide a change in the archive's behaviour.
    if len(records) > limit:
        logger.warning(
            f"Archive returned {len(records)} records for a limit of {limit}; truncating."
        )
        return records[:limit]

    return records
