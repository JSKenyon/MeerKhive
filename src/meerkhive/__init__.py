#!/usr/bin/env python3
import argparse
import asyncio
import enum
import json
import logging
import os
import re
import ssl
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from aiohttp import ClientConnectorCertificateError, ClientConnectorSSLError
from gql import gql
from gql.client import Client
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError
from graphql import GraphQLList, GraphQLNonNull, GraphQLObjectType, GraphQLScalarType
from requests.exceptions import SSLError

product_type_help = (
    "Product type when selecting observation sub-products (MeerKATReductionProduct or "
    "FITSImageProduct)"
)


def parse_filters(raw_filters: List[str]) -> List[Dict[str, Any]]:
    filters = []
    date_range = {"from": None, "to": None}

    for f in raw_filters:
        try:
            parts = re.split(r"[=:]", f, maxsplit=1)
            if len(parts) == 2:
                key, val = parts
            else:
                # handle malformed input
                raise ValueError(f"Invalid filter format: {f}")
        except ValueError:
            raise ValueError(f"Invalid filter format (expected key=value): {f}")

        key = key.strip()
        val = val.strip()

        if key == "from":
            # Normalize to midnight UTC
            dt = datetime.fromisoformat(val).replace(tzinfo=timezone.utc)
            midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            date_range["from"] = midnight.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        elif key == "to":
            # Normalize to midnight UTC
            dt = datetime.fromisoformat(val).replace(tzinfo=timezone.utc)
            midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            date_range["to"] = midnight.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        elif key == "radec":
            j_val = json.loads(val)
            filters.append({"field": key, "value": j_val})
        elif key in ("NumFreqChannels", "Band", "QA2"):
            if isinstance(val, str):
                values = [v.strip() for v in val.split(",") if v.strip()]
            elif isinstance(val, list):
                values = val
            else:
                values = [str(val)]
            if values:
                filters.append({"field": key, "value": values})

        else:
            filters.append({"field": key, "value": val})

    if date_range["from"] or date_range["to"]:
        filters.append({"field": "dateRange", "value": [date_range["from"], date_range["to"]]})

    return filters


def parse_sort(sort_args: list[str]) -> list[dict[str, str]]:
    """
    Parse a list of --sort arguments like ["field1:asc", "field2=desc"] into
    a list of structured sort specifications.

    Returns:
        [
            {"columnKey": "field1", "direction": "ASC"},
            {"columnKey": "field2", "direction": "DESC"}
        ]
    """
    result = []
    valid_directions = {"asc", "desc"}

    for entry in sort_args:
        # Accept field:asc or field=asc
        if ":" in entry:
            field, direction = entry.split(":", 1)
        elif "=" in entry:
            field, direction = entry.split("=", 1)
        else:
            raise ValueError(
                f"Invalid sort format: '{entry}'. Expected 'field:asc', 'field=desc', etc."
            )

        if direction.lower() not in valid_directions:
            raise ValueError(
                f"Invalid sort direction '{direction}' in '{entry}'. Use 'asc' or 'desc'."
            )

        result.append({"columnKey": field.strip(), "direction": direction.strip().upper()})

    return result


def get_logger(name: str, level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "[%(name)s] %(asctime)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    return logger


logger = get_logger("login")


def save_tokens(path, data, refreshed=False):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    if not refreshed:
        logger.info(f"✅ Tokens saved to {path}")


def load_token(path):
    with open(path) as f:
        tokens = json.load(f)
    return tokens["access_token"]


def try_refresh(config):
    token_path = config["token_path"]
    refresh_url = config["refresh_url"]

    if not os.path.exists(token_path):
        return None

    try:
        with open(token_path) as f:
            tokens = json.load(f)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return None

        logger.info("🔄 Attempting to refresh token...")
        response = requests.post(
            refresh_url,
            json={"refresh_token": refresh_token},
            verify=config.get("no_check_certificate"),
        )
        if response.status_code == 200:
            tokens = response.json()
            save_tokens(token_path, tokens, refreshed=True)
            logger.info("✅ Token refreshed successfully.")
            return tokens
        else:
            logger.info("⚠️ Refresh failed: %s", response.text)
            return None
    except Exception as e:
        logger.info(f"⚠️ Refresh error: {e}")
        return None


def full_login(config):
    token_path = config["token_path"]
    init_url = config["init_url"]
    exchange_url = config["exchange_url"]

    logger.info("🔑 Requesting authentication URL...")
    response = requests.get(init_url, verify=config.get("no_check_certificate"))
    response.raise_for_status()
    auth_info = response.json()
    auth_url = auth_info["auth_url"]
    state = auth_info["state"]

    logger.info(f"🌐 Please log in via your browser:\n\n{auth_url}\n")

    logger.info("🔗 After completing login, paste the full redirect URL here: ")
    sys.stderr.flush()
    redirect_url = input().strip()

    parsed = urllib.parse.urlparse(redirect_url)
    query = urllib.parse.parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    state_returned = query.get("state", [None])[0]

    if not code or state_returned != state:
        logger.info("❌ Invalid redirect URL or mismatched state")
        return None

    logger.info("🔄 Exchanging code for tokens...")
    query["manual"] = "true"
    response = requests.get(
        f"{exchange_url}?{urllib.parse.urlencode(query, doseq=True)}",
        verify=config.get("no_check_certificate"),
        params={"code": code, "state": state},
    )
    response.raise_for_status()
    tokens = response.json()
    save_tokens(token_path, tokens)
    logger.info("🎉 Login complete.")
    return tokens


def login(config):
    return try_refresh(config) or full_login(config)


def configure_auth(base_url: str, no_check_certificate: bool = False) -> dict:
    base = base_url.rstrip("/")
    config = {
        "base_url": base,
        "token_path": "tokens.json",
        "refresh_url": f"{base}/_auth/pkce-cli-refresh",
        "exchange_url": f"{base}/_auth/pkce-cli-auth-complete",
        "init_url": f"{base}/_auth/pkce-cli-auth-url",
        "no_check_certificate": not no_check_certificate,
    }

    return config


def build_ssl_context(no_check_certificate: bool) -> ssl.SSLContext:
    if no_check_certificate:
        return ssl._create_unverified_context()

    ca_bundle = os.getenv("REQUESTS_CA_BUNDLE")
    return (
        ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    )


class URLFormat(str, enum.Enum):
    internal = "internal"
    external = "external"


logger = get_logger("data")


def unwrap_type(gql_type):
    """Remove GraphQLNonNull and GraphQLList wrappers."""
    while isinstance(gql_type, (GraphQLNonNull, GraphQLList)):
        gql_type = gql_type.of_type
    return gql_type


def build_selection_block(
    gql_type,
    depth=0,
    max_depth=2,
    skip_fields=None,
    fields=None,
    url_format=None,
    product_type=None,
):
    indent = "  " * (depth + 1)
    lines = []
    skip_fields = skip_fields or set()
    fields = {"*"} if not fields or "*" in fields else fields
    include_all = fields == {"*"}

    for field_name, field in gql_type.fields.items():
        if field_name in skip_fields:
            continue
        if not include_all and field_name not in fields:
            continue

        unwrapped = unwrap_type(field.type)
        if isinstance(unwrapped, GraphQLScalarType) or depth >= max_depth:
            if field_name == "FileSize":
                lines.append(f"{indent}{field_name}")
            elif field_name == "rdb":
                internal = "false" if url_format == URLFormat.external.value else "true"
                lines.append(f"{indent}{field_name}(internal: {internal})")
            else:
                lines.append(f"{indent}{field_name}")
        elif isinstance(unwrapped, GraphQLObjectType):
            nested = build_selection_block(
                unwrapped,
                depth + 1,
                max_depth,
                skip_fields,
                fields={"*"},  # always include all nested subfields
                url_format=url_format,
                product_type=product_type,
            )
            if field_name == "products":
                if not product_type:
                    print(
                        (
                            "Missing required product type for 'products' field.\n"
                            "Either specify a type, for example:\n"
                            "  --product-type='FITSImageProduct'\n"
                            "or exclude this field entirely:\n"
                            "  --exclude-fields='products,field2,field3,etc..'"
                        )
                    )
                    sys.exit(1)
                lines.append(f"{indent}{field_name}(type: {product_type}) {{\n{nested}\n{indent}}}")
            else:
                lines.append(f"{indent}{field_name} {{\n{nested}\n{indent}}}")

    return "\n".join(lines)


async def data(
    auth_address: str = "https://archive.sarao.ac.za",
    fields: str = "*",
    exclude_fields: str = None,
    search: str = "*",
    limit: int = 1000,
    show_fields: bool = False,
    url_format: URLFormat = URLFormat.external.value,
    filters: List[str] = [],
    no_check_certificate: bool = False,
    sort: List[str] = [],
    product_type: str = None,
):
    filters = filters or []
    sort = sort or []

    try:
        url_format = url_format.lower().strip()
        if url_format not in {"internal", "external"}:
            raise ValueError(
                f"Invalid value for 'url_format': {url_format!r}. Must be 'internal' or 'external'."
            )

        # Build login configuration object
        config = configure_auth(auth_address, no_check_certificate=no_check_certificate)

        # Force login
        login(config)

        # Create HTTP client
        async with Client(
            transport=AIOHTTPTransport(
                url=f"{config.get('base_url')}/graphql",
                headers={"Authorization": f"Bearer {load_token(config.get('token_path'))}"},
                ssl=build_ssl_context(no_check_certificate),
            ),
            fetch_schema_from_transport=True,
        ) as session:
            await session.fetch_schema()
            schema = session.client.schema
            capture_block_type = schema.get_type("CaptureBlock")

            if show_fields:
                logger.info("Available fields:")
                sys.stdout.write(
                    build_selection_block(
                        capture_block_type,
                        max_depth=2,
                        skip_fields={},
                        fields={"*"},
                        url_format=url_format,
                        product_type=product_type,
                    )
                )
                return

            selection_block = build_selection_block(
                capture_block_type,
                max_depth=2,
                skip_fields=set([s.strip() for s in (exclude_fields or "").split(",")]),
                fields=set([s.strip() for s in (fields or "*").split(",")]),
                url_format=url_format,
                product_type=product_type,
            )

            query_str = f"""
                query (
                    $limit: Int,
                    $cursor: String,
                    $search: String,
                    $filters: [SolrFilterInput!],
                    $sort: [SortColumnInput!]
                )
                {{
                    captureBlocks(
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


            # Define GraphQL query
            query = gql(query_str)
            filters = parse_filters(filters)
            sort = parse_sort(sort)
            pageSize = 25
            cursor = None

            try:
                fetched = 0
                while True:
                    variables = {
                        "limit": min(pageSize, (limit - fetched)),
                        "cursor": cursor,
                        "search": search,
                        "filters": filters,
                        "sort": sort,
                    }
                    result = await session.execute(query, variable_values=variables)
                    records = result["captureBlocks"]["records"]
                    page_info = result["captureBlocks"]["pageInfo"]

                    for record in records:
                        sys.stdout.write(json.dumps(record) + "\n")
                    fetched += len(records)

                    if not page_info["hasNextPage"] or fetched >= limit:
                        break

                    cursor = page_info["endCursor"]

            except TransportQueryError as e:
                errors = e.errors or []
                logger.error(errors)
                raise

            except Exception:
                raise
    except (
        SSLError,
        ClientConnectorSSLError,
        ClientConnectorCertificateError,
        ssl.SSLCertVerificationError,
    ) as e:
        logger.error("❌ SSL verification failed.")
        logger.error(f"Details: {e}")
        logger.error(
            "\n\nFor development environments, you can resolve this SSL issue by either:\n"
            "  1. Setting the CA bundle for `requests`:\n"
            '     export REQUESTS_CA_BUNDLE="/path/to/repo-root/certs/ca.cert.pem"\n'
            "  2. Disabling SSL verification with --no-check-certificate"
        )
    except Exception:
        logger.error("An unexpected error occurred.")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Fetch data from the archive API and print NDJSON to stdout."
    )

    parser.add_argument(
        "--auth-address",
        "-a",
        default="https://archive.sarao.ac.za",
        help="Authentication server URL",
    )
    parser.add_argument(
        "--search",
        default="*",
        help="Search term (default: '*')",
    )
    parser.add_argument(
        "--fields",
        default="*",
        help="Comma-separated list of fields to include",
    )
    parser.add_argument(
        "--product-type",
        default=None,
        help=product_type_help,
    )
    parser.add_argument(
        "--exclude-fields",
        default=None,
        help="Comma-separated list of fields to exclude",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Limit results to N records (default: 1000)",
    )
    parser.add_argument(
        "--show-fields",
        action="store_true",
        help="Print available fields and exit",
    )
    parser.add_argument(
        "--url-format",
        default="external",
        choices=[e.value for e in URLFormat],
        help="Format of generated URLs: 'internal' or 'external' (default: external)",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="Key=value filter (can be specified multiple times)",
    )
    parser.add_argument(
        "--no-check-certificate",
        action="store_true",
        help="Disable SSL certificate verification",
    )
    parser.add_argument(
        "--sort",
        action="append",
        default=[],
        help="Key=value sort (can be specified multiple times)",
    )

    args = parser.parse_args()

    asyncio.run(
        data(
            auth_address=args.auth_address,
            fields=args.fields,
            exclude_fields=args.exclude_fields,
            search=args.search,
            limit=args.limit,
            show_fields=args.show_fields,
            url_format=URLFormat(args.url_format).value,
            filters=args.filter,
            no_check_certificate=args.no_check_certificate,
            sort=args.sort,
            product_type=args.product_type,
        )
    )


if __name__ == "__main__":
    main()
