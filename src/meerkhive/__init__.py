"""MeerKhive — Python client for the SARAO MeerKAT archive.

The primary entry points are :func:`query_archive` (synchronous) and
:func:`query_archive_async` (async). Authentication is handled automatically
via PKCE OAuth2 against the SARAO Keycloak realm.

Example::

    from meerkhive import query_archive

    records = query_archive(
        fields="CaptureBlockId,StartTime",
        limit=10,
        filters=["Band=L", 'dateRange=["2024-01-01T00:00:00.000Z",null]'],
    )
    for r in records:
        print(r["CaptureBlockId"])
"""

from meerkhive.archive import (
    AuthenticatedTransport,
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
    "AuthenticatedTransport",
    "KeycloakAuth",
    "build_selection_block",
    "build_ssl_context",
    "fetch_fields",
    "fetch_fields_async",
    "get_access_token",
    "parse_filters",
    "parse_sort",
    "query_archive",
    "query_archive_async",
]
