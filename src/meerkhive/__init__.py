"""MeerKhive — Python client for the SARAO MeerKAT archive.

The primary entry points are :func:`query_archive` (synchronous) and
:func:`query_archive_async` (async). Authentication is handled automatically
via PKCE OAuth2 against the SARAO Keycloak realm.

Example::

    from meerkhive import query_archive, parse_filters

    records = query_archive(
        fields="CaptureBlockId,StartTime",
        limit=10,
        filters=parse_filters(["Band=L", "from=2024-01-01"]),
    )
    for r in records:
        print(r["CaptureBlockId"])
"""

from meerkhive.archive import (
    AuthenticatedTransport,
    UrlFormat,
    build_selection_block,
    build_ssl_context,
    fetch_fields,
    fetch_fields_async,
    parse_filters,
    parse_sort,
    query_archive,
    query_archive_async,
)
from meerkhive.auth import KeycloakAuth, get_access_token

__all__ = [
    # Archive / query
    "AuthenticatedTransport",
    "UrlFormat",
    "build_selection_block",
    "build_ssl_context",
    "fetch_fields",
    "fetch_fields_async",
    "parse_filters",
    "parse_sort",
    "query_archive",
    "query_archive_async",
    # Auth
    "KeycloakAuth",
    "get_access_token",
]
