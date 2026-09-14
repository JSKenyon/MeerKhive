"""GraphQL client for the SARAO MeerKAT archive.

This module is intentionally narrow: it builds a GraphQL selection block by
introspecting the live schema, paginates through the ``observations`` root
field, and returns the ``Observation`` records it yields. Authentication is
delegated entirely to :mod:`meerkhive.auth`.

Design notes:

- The selection block is generated dynamically so the client tracks schema
  changes without code edits. Field overrides handle the one schema-specific
  oddity (``rdb`` takes an ``internal`` argument).
- :class:`AuthenticatedTransport` injects the bearer token on every request
  and transparently retries once on HTTP 401, after asking the auth module for
  a fresh token. This means a long pagination run that outlives the 5-minute
  access-token TTL still completes successfully.

.. note::

    The archive GraphQL schema exposes observations under the type name
    ``Observation``. If introspection raises ``RuntimeError`` with a message
    about the type not being found, verify the current schema by running::

        meerkhive --show-fields
"""

import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal

from aiohttp import ClientConnectorCertificateError, ClientConnectorSSLError, ClientTimeout
from gql import GraphQLRequest, gql
from gql.client import AsyncClientSession, Client
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import (
    TransportConnectionFailed,
    TransportQueryError,
    TransportServerError,
)
from graphql import (
    GraphQLObjectType,
    get_named_type,
    is_abstract_type,
    is_leaf_type,
    is_object_type,
    is_required_argument,
)
from requests.exceptions import SSLError

from meerkhive.auth import KeycloakAuth, get_access_token

logger = logging.getLogger(__name__)

# The ``url_format`` argument of the public API is restricted to these two
# string literals. ``Literal`` gives us static-checker support without the
# runtime ceremony of a real ``Enum`` (the previous implementation always
# stringified the enum members anyway).
UrlFormat = Literal["internal", "external"]

# The public API: what a caller using MeerKhive as a library would reasonably
# reach for. Module-level constants are deliberately absent — every one of them
# is the default of a keyword argument, which is how a caller is meant to
# change it. Names not listed here are internal and may change without notice;
# the package does not use a leading-underscore convention to mark them.
__all__ = [
    "AuthenticatedTransport",
    "UrlFormat",
    "build_ssl_context",
    "build_selection_block",
    "fetch_fields",
    "fetch_fields_async",
    "parse_filters",
    "parse_sort",
    "query_archive",
    "query_archive_async",
]


# ---------------------------------------------------------------------------
# Pagination, timeouts and retries
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SSL helpers
# ---------------------------------------------------------------------------


def build_ssl_context(verify: bool) -> ssl.SSLContext:
    """Build an :class:`ssl.SSLContext` for the aiohttp transport.

    Args:
        verify: If ``False``, certificates are not verified. If ``True``, we
            use the system default trust store, optionally overridden by the
            ``REQUESTS_CA_BUNDLE`` environment variable so the same custom
            CA bundle works for both ``requests`` (used by :mod:`meerkhive.auth`)
            and ``aiohttp`` (used here).

    Returns:
        A configured ``ssl.SSLContext`` suitable for passing to the aiohttp
        transport.
    """
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ca_bundle = os.getenv("REQUESTS_CA_BUNDLE")
    return (
        ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    )


# ---------------------------------------------------------------------------
# Filter and sort parsing
# ---------------------------------------------------------------------------

# The root GraphQL object type that represents a single telescope observation.
# Defined here so a schema rename only requires one edit.
OBSERVATION_TYPE: str = "Observation"

# Filter keys whose string value should be parsed as a JSON object (e.g. a
# coordinate dict or a date-range array). Extend this set when the schema adds
# further JSON-valued filter fields.
JSON_FILTER_FIELDS: frozenset[str] = frozenset({"dateRange", "radec"})

# Filter keys whose value is a comma-separated list of alternatives (Solr
# multi-value match). Adding a new list-typed field here is sufficient;
# parse_filters requires no other change.
LIST_FILTER_FIELDS: frozenset[str] = frozenset({"Band", "NumFreqChannels", "QA2"})


def parse_filters(raw_filters: list[str]) -> list[dict[str, Any]]:
    """Parse a list of ``key=value`` filter strings into GraphQL filter dicts.

    Handles several special cases beyond a simple key-value mapping:

    - ``dateRange``, ``radec``: value is parsed as JSON (e.g.
      ``'["2024-01-01T00:00:00.000Z", null]'`` for an open-ended date range,
      or ``'{"ra": 1.23, "dec": -4.56}'`` for a coordinate filter).
    - ``Band``, ``QA2``, ``NumFreqChannels``: comma-separated values are split
      into a list for multi-value matching.
    - All other keys: passed through as-is.

    Args:
        raw_filters: Each entry should be of the form ``"key=value"``.

    Returns:
        A list of ``{"field": key, "value": val}`` dicts ready to be passed
        as the ``filters`` variable to the archive GraphQL query.

    Raises:
        ValueError: If an entry cannot be split into a key-value pair.
    """
    filters: list[dict[str, Any]] = []

    for f in raw_filters:
        parts = f.split("=", maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Invalid filter format (expected key=value): {f!r}")

        key, val = parts[0].strip(), parts[1].strip()
        if not key:
            raise ValueError(f"Invalid filter format (empty key): {f!r}")

        if key in JSON_FILTER_FIELDS:
            filters.append({"field": key, "value": json.loads(val)})
        elif key in LIST_FILTER_FIELDS:
            values = [v.strip() for v in val.split(",") if v.strip()]
            if values:
                filters.append({"field": key, "value": values})
        else:
            filters.append({"field": key, "value": val})

    return filters


def parse_sort(sort_args: list[str]) -> list[dict[str, str]]:
    """Parse a list of sort specifiers into GraphQL ``SortColumnInput`` dicts.

    Args:
        sort_args: Each entry should be ``"field:asc"`` or ``"field:desc"``
            (case-insensitive direction).

    Returns:
        A list of ``{"columnKey": field, "direction": "ASC"|"DESC"}`` dicts
        suitable for the GraphQL ``sort`` variable.

    Raises:
        ValueError: If an entry does not contain a ``:`` separator, or if the
            direction is not ``asc`` or ``desc``.
    """
    result: list[dict[str, str]] = []

    for entry in sort_args:
        parts = entry.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid sort format: {entry!r}. Expected 'field:asc' or 'field:desc'."
            )
        field, direction = parts
        if not field.strip():
            raise ValueError(f"Invalid sort format (empty field): {entry!r}.")

        direction = direction.strip().upper()
        if direction not in ("ASC", "DESC"):
            raise ValueError(
                f"Invalid sort direction {direction!r} in {entry!r}. Use 'asc' or 'desc'."
            )

        result.append({"columnKey": field.strip(), "direction": direction})

    return result


# ---------------------------------------------------------------------------
# Schema-driven selection-block builder
# ---------------------------------------------------------------------------


# Field-name -> ``(url_format) -> selection-fragment`` mapping. The default
# carries the only schema-specific knowledge: the ``rdb`` field needs an
# ``internal`` boolean argument that toggles whether the URL is reachable
# from inside SARAO or via the public internet. Lifting this out of the
# generic walker keeps :func:`build_selection_block` schema-agnostic.
DEFAULT_FIELD_OVERRIDES: dict[str, Callable[[UrlFormat], str]] = {
    "rdb": lambda url_format: f"rdb(internal: {'false' if url_format == 'external' else 'true'})",
}


def _check_field_names(names: set[str], gql_type: GraphQLObjectType, param: str) -> None:
    """Raise :class:`ValueError` if any names are absent from *gql_type*'s fields.

    Args:
        names: Field names to validate.
        gql_type: The ``GraphQLObjectType`` whose field map is authoritative.
        param: The public parameter name to include in the error message.

    Raises:
        ValueError: If one or more names are not present in the type's fields.
    """
    unknown = names - set(gql_type.fields)
    if unknown:
        raise ValueError(
            f"Unknown field(s) in {param!r}: {sorted(unknown)}. "
            "Field names are case-sensitive. Run `meerkhive --show-fields` to see available names."
        )


def build_selection_block(
    gql_type: GraphQLObjectType,
    *,
    skip_fields: set[str] | None = None,
    fields: set[str] | None = None,
    url_format: UrlFormat = "external",
    field_overrides: dict[str, Callable[[UrlFormat], str]] | None = None,
) -> str:
    """Build a GraphQL selection block by walking a type's fields.

    Args:
        gql_type: A ``GraphQLObjectType`` to walk.
        skip_fields: Top-level field names to omit. Silently ignored if a
            name is not present in the schema; not propagated into nested
            object types.
        fields: Specific top-level fields to include. ``None`` (the default)
            includes every field the schema exposes. Nested levels always
            include all fields regardless of this setting.
        url_format: Forwarded to field overrides.
        field_overrides: Per-field rendering overrides; falls back to
            :data:`DEFAULT_FIELD_OVERRIDES` when ``None``.

    Returns:
        The selection block as a single multi-line string (no surrounding
        braces — the caller wraps it in the outer query).

    Raises:
        ValueError: If ``fields`` is not ``None`` and any of the named
            fields are not present in the schema.
    """
    overrides = field_overrides if field_overrides is not None else DEFAULT_FIELD_OVERRIDES
    if fields is not None:
        _check_field_names(fields, gql_type, "fields")
    return _walk_selection(
        gql_type,
        depth=0,
        skip_fields=skip_fields or set(),
        fields=fields,
        url_format=url_format,
        overrides=overrides,
        ancestors=frozenset(),
    )


def _walk_selection(
    gql_type: GraphQLObjectType,
    *,
    depth: int,
    skip_fields: set[str],
    fields: set[str] | None,
    url_format: UrlFormat,
    overrides: dict[str, Callable[[UrlFormat], str]],
    ancestors: frozenset[str],
) -> str:
    """Recursive helper for :func:`build_selection_block`.

    Args:
        gql_type: The type whose fields are being walked.
        depth: Current recursion depth, used solely for indentation.
        skip_fields: Field names to omit.
        fields: If not ``None``, only include fields in this set.
        url_format: Forwarded to field overrides.
        overrides: Per-field rendering overrides.
        ancestors: Names of object types strictly above ``gql_type`` on
            the current recursion path. A field whose unwrapped type is
            already visited (an ancestor or ``gql_type`` itself) is skipped
            to prevent infinite recursion through a self-referential schema
            (e.g. Keycloak groups with ``subGroups: [KeycloakGroup]``).

    Returns:
        The (possibly empty) selection lines joined by newlines.
    """
    indent = "  " * (depth + 1)
    lines: list[str] = []
    visited = ancestors | {gql_type.name}

    for field_name, field in gql_type.fields.items():
        if field_name in skip_fields:
            continue
        if fields is not None and field_name not in fields:
            continue

        # Skip fields that require arguments (e.g. ``products(type: ProductType!)``)
        # unless an explicit override knows how to render them. Without this
        # guard the generated query fails GraphQL validation whenever the
        # archive schema adds a new field with a required argument.
        if field_name not in overrides and any(
            is_required_argument(arg) for arg in field.args.values()
        ):
            continue

        unwrapped = get_named_type(field.type)

        if is_abstract_type(unwrapped):
            # Abstract types require inline fragments, which this walker does
            # not yet generate. Skip rather than emit invalid GraphQL; log at
            # debug so schema additions using these types remain visible.
            logger.debug(
                f"Skipping field {field_name!r}: abstract GraphQL types "
                "(interface/union) are not supported."
            )
            continue

        override = overrides.get(field_name)
        rendered_name = override(url_format) if override else field_name

        # Scalars and enums are both GraphQL leaf types — emit them as bare
        # field names without a sub-selection.
        if is_leaf_type(unwrapped):
            lines.append(f"{indent}{rendered_name}")
        elif is_object_type(unwrapped):
            # Break cycles in self-referential schemas. Without this guard a
            # type like ``KeycloakGroup`` with a ``subGroups: [KeycloakGroup]``
            # field would recurse forever.
            if unwrapped.name in visited:
                logger.debug(
                    f"Skipping field {field_name!r}: would recurse back into "
                    f"already-visited type {unwrapped.name!r}."
                )
                continue
            nested = _walk_selection(
                unwrapped,
                depth=depth + 1,
                skip_fields=set(),  # skip_fields is a top-level-only concept
                fields=None,  # always include all nested subfields
                url_format=url_format,
                overrides=overrides,
                ancestors=visited,
            )
            # Only emit the sub-selection if it's non-empty; an empty block
            # would produce invalid GraphQL (``field { }``).
            if nested:
                lines.append(f"{indent}{rendered_name} {{\n{nested}\n{indent}}}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Authenticated transport
# ---------------------------------------------------------------------------


class AuthenticatedTransport(AIOHTTPTransport):
    """An :class:`AIOHTTPTransport` that injects a bearer token per request.

    We deliberately avoid putting ``Authorization`` on the underlying
    aiohttp ``ClientSession`` default headers, because that header would be
    frozen at session-creation time. Injecting per-request lets us call
    :func:`get_access_token` lazily (so a still-valid cached token is reused)
    and swap the token on a 401 retry without recreating the session.

    Attributes:
        _auth: The Keycloak configuration used to mint bearer tokens on
            every outbound request.
    """

    def __init__(
        self,
        *,
        url: str,
        auth: KeycloakAuth,
        ssl_context: ssl.SSLContext,
        request_timeout: float | None = None,
    ):
        """Initialise the transport.

        Args:
            url: The GraphQL endpoint URL.
            auth: Keycloak configuration used to obtain bearer tokens.
            ssl_context: The SSL context to use for TLS connections.
            request_timeout: Total seconds allowed for one HTTP request. Left
                unset, aiohttp caps a request at its own 300 s default, which
                would silently override any larger deadline set on the client.
        """
        # Passed via client_session_args rather than the base class's own
        # ``timeout``, which is typed as an int and would truncate fractions.
        # connect() applies client_session_args last, so this wins.
        session_args = (
            {"timeout": ClientTimeout(total=request_timeout)}
            if request_timeout is not None
            else None
        )
        super().__init__(url=url, ssl=ssl_context, client_session_args=session_args)
        self._auth = auth

    async def execute(
        self,
        request: Any,
        *,
        extra_args: dict[str, Any] | None = None,
        upload_files: bool = False,
    ) -> Any:
        """Execute a GraphQL request, injecting a bearer token.

        Retries exactly once with a force-refreshed token if the first
        attempt raises a 401 ``TransportServerError``.

        Args:
            request: The GraphQL request object passed through to the base
                transport.
            extra_args: Extra keyword arguments forwarded to the underlying
                aiohttp request; the ``Authorization`` header is merged in.
            upload_files: Whether the request uploads files (forwarded to
                the base transport).

        Returns:
            The decoded GraphQL response as returned by the base transport.

        Raises:
            TransportServerError: If the server returns a non-401 error, or
                if the single 401 retry also fails.
        """
        merged = dict(extra_args or {})
        headers = dict(merged.get("headers") or {})
        # get_access_token does synchronous file I/O and may make blocking
        # HTTP calls (refresh / login), so offload it to avoid stalling the
        # event loop.
        token = await asyncio.to_thread(get_access_token, self._auth)
        headers["Authorization"] = f"Bearer {token}"
        merged["headers"] = headers

        try:
            return await super().execute(request, extra_args=merged, upload_files=upload_files)
        except TransportServerError as e:
            # Retry exactly once on 401 with a forced refresh. Anything else
            # propagates up so the caller sees the real error.
            if e.code != 401:
                raise
            logger.info("Got 401 from archive; forcing token refresh and retrying once.")
            token = await asyncio.to_thread(get_access_token, self._auth, force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            merged["headers"] = headers
            return await super().execute(request, extra_args=merged, upload_files=upload_files)


# ---------------------------------------------------------------------------
# Query implementation
# ---------------------------------------------------------------------------


async def fetch_fields_async(
    auth_address: str = "https://archive.sarao.ac.za",
    verify_ssl: bool = True,
) -> str:
    """Introspect the live schema and return the ``Observation`` selection block.

    Connects to the archive, fetches the GraphQL schema, and returns the
    selection block that :func:`query_archive_async` would use — suitable for
    printing with ``--show-fields``.

    Args:
        auth_address: Base URL of the archive service. The ``/graphql``
            endpoint is appended automatically.
        verify_ssl: Whether to verify TLS certificates.

    Returns:
        The selection block as a multi-line string.

    Raises:
        RuntimeError: If the ``Observation`` type is not found in the live
            schema.
    """
    auth = KeycloakAuth.default(verify_ssl=verify_ssl)

    # Acquire the token before the gql client opens. get_access_token may drive
    # an interactive browser login that waits minutes for a human, while gql
    # applies execute_timeout to every request the session makes — acquiring it
    # here keeps that login out of any request deadline, where it would be
    # cancelled and then retried into a second browser tab. It is offloaded to
    # a thread for the same reason AuthenticatedTransport.execute offloads it:
    # blocking calls here would stall an embedding application's event loop.
    await asyncio.to_thread(get_access_token, auth)

    transport = AuthenticatedTransport(
        url=f"{auth_address.rstrip('/')}/graphql",
        auth=auth,
        ssl_context=build_ssl_context(verify=verify_ssl),
    )
    async with Client(transport=transport, fetch_schema_from_transport=True) as session:
        observation_type = session.client.schema.get_type(OBSERVATION_TYPE)
        if not isinstance(observation_type, GraphQLObjectType):
            raise RuntimeError(
                f"The archive schema does not define an '{OBSERVATION_TYPE}' object type. "
                "The schema may have changed or failed to load correctly."
            )
        return build_selection_block(observation_type)


def fetch_fields(
    auth_address: str = "https://archive.sarao.ac.za",
    verify_ssl: bool = True,
) -> str:
    """Synchronous wrapper around :func:`fetch_fields_async`.

    All arguments are forwarded unchanged. See :func:`fetch_fields_async` for
    the full parameter and exception documentation.
    """
    return asyncio.run(fetch_fields_async(auth_address=auth_address, verify_ssl=verify_ssl))


async def query_archive_async(
    auth_address: str = "https://archive.sarao.ac.za",
    fields: str = "*",
    exclude_fields: str | None = None,
    search: str = "*",
    limit: int = 1000,
    url_format: UrlFormat = "external",
    filters: list[str] | None = None,
    verify_ssl: bool = True,
    sort: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_timeout: float = DEFAULT_PAGE_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Query the MeerKAT archive and return matching observation records.

    This is the async implementation; use :func:`query_archive` for a
    synchronous entry point that calls ``asyncio.run`` automatically.

    Args:
        auth_address: Base URL of the archive service. The ``/graphql``
            endpoint is appended automatically.
        fields: Comma-separated list of top-level ``Observation`` fields to
            request, or ``"*"`` to request every field the schema exposes.
        exclude_fields: Comma-separated list of field names to omit from
            the selection block.
        search: Free-text search term passed to the GraphQL ``search``
            variable.
        limit: Maximum number of records to return across all pages.
        url_format: Either ``"internal"`` or ``"external"``. Controls
            whether URL-valued fields (e.g. ``rdb``) are rendered for
            in-SARAO or public-internet use.
        filters: List of ``"key=value"`` filter strings. Parsed internally
            via :func:`parse_filters`. Pass ``None`` (or omit) for no
            filtering. Examples: ``["Band=L"]``,
            ``['dateRange=["2024-01-01T00:00:00.000Z", null]']``.
        verify_ssl: Whether to verify TLS certificates when talking to
            the archive. Set to ``False`` only for development against a
            self-signed endpoint.
        sort: List of sort specifiers in ``"field:asc"`` / ``"field:desc"``
            format. Parsed internally via :func:`parse_sort`.
        page_size: Records to request per page. The archive caps this at
            :data:`MAX_PAGE_SIZE`; larger values are warned about, not
            rejected.
        page_timeout: Timeout in seconds for a single page request. Applied
            twice: as the gql client's ``execute_timeout`` (replacing its 10 s
            default, too tight for the archive's observed latency) and as the
            aiohttp session's total timeout, which otherwise caps a request at
            its own 300 s default. Bounds each request, not the whole query.
        max_attempts: Total attempts per page, including the first. Transient
            timeouts and server-side errors are retried with exponential
            backoff.

    Returns:
        The list of observation records returned by the archive, up to
        ``limit`` entries.

    Raises:
        ValueError: If ``url_format`` is not ``"internal"`` or
            ``"external"``, if ``limit``, ``page_size`` or ``max_attempts``
            is less than one, if ``page_timeout`` is not greater than zero,
            or if any filter or sort string is malformed.
        SSLError: If TLS verification fails against the archive endpoint.
        ClientConnectorSSLError: If the aiohttp connector fails TLS
            verification.
        ClientConnectorCertificateError: If the aiohttp connector rejects
            the server certificate.
        ssl.SSLCertVerificationError: If the underlying SSL layer fails
            certificate verification.
        TransportQueryError: If the archive returns GraphQL-level errors.
        TransportConnectionFailed: If a page still fails to reach the archive
            on its final attempt. Exceeding ``page_timeout`` usually surfaces
            this way rather than as ``TimeoutError``; see :func:`fetch_page`.
        TimeoutError: If a page exceeds ``page_timeout`` on its final attempt
            and gql's deadline wins the race against the session's.
        RuntimeError: If the ``Observation`` type is not found in the live
            schema. This may indicate a schema change; verify the current
            schema with ``meerkhive --show-fields``.
    """
    if url_format not in ("internal", "external"):
        raise ValueError(
            f"Invalid value for 'url_format': {url_format!r}. Must be 'internal' or 'external'."
        )

    validate_limit(limit)
    validate_page_size(page_size)
    validate_page_timeout(page_timeout)
    validate_max_attempts(max_attempts)
    warn_if_page_size_exceeds_cap(page_size)

    parsed_filters = parse_filters(filters or [])
    parsed_sort = parse_sort(sort or [])
    skip_fields = {s.strip() for s in (exclude_fields or "").split(",") if s.strip()}
    # None and the literal "*" both mean "include all fields". Normalise to
    # None here so build_selection_block receives an unambiguous sentinel.
    requested_fields = (
        None
        if not fields or fields.strip() == "*"
        else {s.strip() for s in fields.split(",") if s.strip()}
    )

    auth = KeycloakAuth.default(verify_ssl=verify_ssl)
    ssl_context = build_ssl_context(verify=verify_ssl)

    transport = AuthenticatedTransport(
        url=f"{auth_address.rstrip('/')}/graphql",
        auth=auth,
        ssl_context=ssl_context,
        request_timeout=page_timeout,
    )

    try:
        # Acquire the token before the gql client opens. get_access_token may
        # drive an interactive browser login that waits minutes for a human,
        # while gql applies execute_timeout to every request the session makes
        # — acquiring it here keeps that login out of any request deadline,
        # where it would be cancelled and then retried into a second browser
        # tab. It is offloaded to a thread for the same reason
        # AuthenticatedTransport.execute offloads it: blocking calls here would
        # stall an embedding application's event loop. It stays inside this try
        # because Keycloak is reached with requests, making it the likeliest
        # source of the SSLError the handler below explains.
        await asyncio.to_thread(get_access_token, auth)

        async with Client(
            transport=transport,
            fetch_schema_from_transport=True,
            execute_timeout=page_timeout,
        ) as session:
            schema = session.client.schema
            observation_type = schema.get_type(OBSERVATION_TYPE)
            if not isinstance(observation_type, GraphQLObjectType):
                raise RuntimeError(
                    f"The archive schema does not define an '{OBSERVATION_TYPE}' object type. "
                    "The schema may have changed or failed to load correctly."
                )

            selection_block = build_selection_block(
                observation_type,
                skip_fields=skip_fields,
                fields=requested_fields,
                url_format=url_format,
            )

            # NOTE: double braces escape ``{`` / ``}`` in the f-string so the
            # GraphQL braces survive interpolation.
            query_str = f"""
                query (
                    $limit: Int,
                    $cursor: String,
                    $search: String,
                    $filters: [SolrFilterInput!],
                    $sort: [SortColumnInput!]
                )
                {{
                    observations(
                        limit: $limit,
                        cursor: $cursor,
                        search: $search,
                        filters: $filters,
                        sort: $sort
                    )
                    {{
                        pageInfo {{
                            totalCount
                            endCursor
                            hasNextPage
                        }}
                        records {{
                            {selection_block}
                        }}
                    }}
                }}
            """
            request = gql(query_str)

            try:
                return await fetch_all_pages(
                    session,
                    request,
                    {"search": search, "filters": parsed_filters, "sort": parsed_sort},
                    page_size=page_size,
                    limit=limit,
                    max_attempts=max_attempts,
                )
            except TransportQueryError as e:
                logger.error(f"GraphQL errors: {e.errors or []}")
                raise

    except (
        SSLError,
        ClientConnectorSSLError,
        ClientConnectorCertificateError,
        ssl.SSLCertVerificationError,
    ) as e:
        logger.error("SSL verification failed.")
        logger.error(f"Details: {e}")
        logger.error(
            "\n\nIf this is a certificate-trust issue, point `requests` at the "
            "appropriate CA bundle, for example:\n"
            '     export REQUESTS_CA_BUNDLE="/path/to/ca.cert.pem"\n'
            "Do not disable SSL verification against the production archive."
        )
        # Re-raise so callers can distinguish "no results" from "broken TLS".
        raise


def query_archive(
    auth_address: str = "https://archive.sarao.ac.za",
    fields: str = "*",
    exclude_fields: str | None = None,
    search: str = "*",
    limit: int = 1000,
    url_format: UrlFormat = "external",
    filters: list[str] | None = None,
    verify_ssl: bool = True,
    sort: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_timeout: float = DEFAULT_PAGE_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`query_archive_async`.

    All arguments are forwarded unchanged. See :func:`query_archive_async` for
    the full parameter and exception documentation.
    """
    return asyncio.run(
        query_archive_async(
            auth_address=auth_address,
            fields=fields,
            exclude_fields=exclude_fields,
            search=search,
            limit=limit,
            url_format=url_format,
            filters=filters,
            verify_ssl=verify_ssl,
            sort=sort,
            page_size=page_size,
            page_timeout=page_timeout,
            max_attempts=max_attempts,
        )
    )
