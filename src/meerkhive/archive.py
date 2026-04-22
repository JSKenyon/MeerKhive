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
import json
import logging
import os
import ssl
from collections.abc import Callable
from typing import Any, Literal

from aiohttp import ClientConnectorCertificateError, ClientConnectorSSLError
from gql import gql
from gql.client import Client
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError, TransportServerError
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
    "DEFAULT_FIELD_OVERRIDES",
    "JSON_FILTER_FIELDS",
    "LIST_FILTER_FIELDS",
]


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
    ):
        """Initialise the transport.

        Args:
            url: The GraphQL endpoint URL.
            auth: Keycloak configuration used to obtain bearer tokens.
            ssl_context: The SSL context to use for TLS connections.
        """
        super().__init__(url=url, ssl=ssl_context)
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

    Returns:
        The list of observation records returned by the archive, up to
        ``limit`` entries.

    Raises:
        ValueError: If ``url_format`` is not ``"internal"`` or
            ``"external"``, or if any filter or sort string is malformed.
        SSLError: If TLS verification fails against the archive endpoint.
        ClientConnectorSSLError: If the aiohttp connector fails TLS
            verification.
        ClientConnectorCertificateError: If the aiohttp connector rejects
            the server certificate.
        ssl.SSLCertVerificationError: If the underlying SSL layer fails
            certificate verification.
        TransportQueryError: If the archive returns GraphQL-level errors.
        RuntimeError: If the ``Observation`` type is not found in the live
            schema. This may indicate a schema change; verify the current
            schema with ``meerkhive --show-fields``.
    """
    if url_format not in ("internal", "external"):
        raise ValueError(
            f"Invalid value for 'url_format': {url_format!r}. Must be 'internal' or 'external'."
        )

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
    )

    all_records: list[dict[str, Any]] = []

    try:
        async with Client(transport=transport, fetch_schema_from_transport=True) as session:
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
            query = gql(query_str)

            page_size = 25
            cursor: str | None = None
            fetched = 0

            while True:
                variables = {
                    "limit": min(page_size, limit - fetched),
                    "cursor": cursor,
                    "search": search,
                    "filters": parsed_filters,
                    "sort": parsed_sort,
                }
                try:
                    result = await session.execute(query, variable_values=variables)
                except TransportQueryError as e:
                    logger.error(f"GraphQL errors: {e.errors or []}")
                    raise

                records = result["observations"]["records"]
                page_info = result["observations"]["pageInfo"]
                all_records.extend(records)
                fetched += len(records)

                # Guard against a broken server that reports hasNextPage=True
                # but returns no records — without this the cursor never
                # advances and the loop never terminates.
                if not records or not page_info["hasNextPage"] or fetched >= limit:
                    break

                cursor = page_info["endCursor"]

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

    return all_records


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
        )
    )
