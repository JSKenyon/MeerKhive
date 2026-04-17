"""Typer-based CLI for the MeerKAT archive client.

Thin wrapper over :func:`meerkhive.archive.query_archive` that writes
NDJSON to stdout so the output is pipeable to ``jq``, ``grep``, etc.
All logs go to stderr.
"""

import json
import logging
import sys
from typing import Annotated, Literal

import typer

from meerkhive.archive import fetch_fields, query_archive

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="meerkhive",
    help="Query the MeerKAT archive and print NDJSON to stdout.",
    add_completion=False,
)


@app.command()
def main(
    auth_address: Annotated[
        str,
        typer.Option("--auth-address", "-a", help="Archive base URL."),
    ] = "https://archive.sarao.ac.za",
    search: Annotated[
        str,
        typer.Option(help="Free-text search term (default: '*')."),
    ] = "*",
    fields: Annotated[
        str,
        typer.Option(help="Comma-separated fields to include, or '*' for all."),
    ] = "*",
    exclude_fields: Annotated[
        str | None,
        typer.Option(help="Comma-separated fields to omit."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(help="Maximum number of records to fetch."),
    ] = 1000,
    show_fields: Annotated[
        bool,
        typer.Option("--show-fields", help="Print available field names and exit."),
    ] = False,
    url_format: Annotated[
        Literal["internal", "external"],
        typer.Option(help="URL format for link-valued fields: 'internal' or 'external'."),
    ] = "external",
    filter: Annotated[  # noqa: A002 — mirrors the CLI flag name
        list[str] | None,
        typer.Option(
            "--filter",
            help=(
                "key=value filter, repeatable "
                '(e.g. --filter Band=L --filter dateRange=["2024-01-01T00:00:00.000Z",null]).'
            ),
        ),
    ] = None,
    verify_ssl: Annotated[
        bool,
        typer.Option(
            "--verify-ssl/--no-verify-ssl",
            help="Verify SSL certificates (disable for development only).",
        ),
    ] = True,
    sort: Annotated[
        list[str] | None,
        typer.Option(
            "--sort",
            help="Sort specifier, repeatable (e.g. --sort StartTime:desc).",
        ),
    ] = None,
) -> None:
    """Query the MeerKAT archive and print matching observations as NDJSON."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[%(name)s] %(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if show_fields:
        print(fetch_fields(auth_address=auth_address, verify_ssl=verify_ssl))
        return

    records = query_archive(
        auth_address=auth_address,
        fields=fields,
        exclude_fields=exclude_fields,
        search=search,
        limit=limit,
        url_format=url_format,
        filters=filter or [],
        verify_ssl=verify_ssl,
        sort=sort or [],
    )

    for record in records:
        print(json.dumps(record))
