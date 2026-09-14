# CLAUDE.md

## Project Overview

MeerKhive is a CLI tool and Python library for querying the MeerKAT radio telescope archive via GraphQL. Authenticates via PKCE OAuth2 against SARAO Keycloak, introspects the live schema at runtime, paginates results, and emits NDJSON to stdout.

## Commands

```bash
uv sync                          # Install dependencies
uv run meerkhive --help          # Run CLI
ruff check . && ruff format .    # Lint and format
pytest                           # Run tests
```

## Modules

`src/meerkhive/` contains five modules:

- **auth.py** — PKCE OAuth2 login, token refresh, and token persistence (`~/.local/state/meerkhive/tokens.json`). Public entry point: `get_access_token()`.
- **archive.py** — GraphQL client. Introspects the live `Observation` schema to build a selection block, opens the session, and handles bearer token injection with 401 retry. Delegates the result walk to `pagination.py`. Public entry points: `query_archive()` / `query_archive_async()`.
- **pagination.py** — Cursor pagination for the `observations` field, plus the policy that keeps a long walk alive: page size, per-page timeout, retry classification, backoff, and the progress heartbeat. Internal; callers reach it through `archive.py`'s keyword arguments. It is handed an open session and a parsed request, so it can be tested against a stub with no network.
- **cli.py** — Typer CLI wrapping `query_archive`. Options: `--fields`, `--exclude-fields`, `--filter`, `--sort`, `--search`, `--limit`, `--page-size`, `--page-timeout`, `--show-fields`, `--verify-ssl`.
- **\_\_init\_\_.py** — re-exports public API only.

## Key Behaviours

- Schema is introspected at runtime; field names are discovered dynamically. Use `--show-fields` to list them.
- Filters: `key=value` pairs; some keys are JSON-parsed (`dateRange`, `radec`) or comma-split lists (`Band`, `QA2`, `NumFreqChannels`).
- Pagination fetches `--page-size` records per request (default and archive cap: 100).
  `--page-timeout` (default 120 s) bounds one request, not the whole query. It is applied
  both as gql's `execute_timeout` (default 10 s) and as the aiohttp session's total
  timeout (default 300 s), so larger values are not silently capped.
- Each page is attempted up to `max_attempts` times in total (default 3: one try plus two
  retries) on timeout, a 5xx, or a connection failure, with exponential backoff.
  `max_attempts` is a library-only parameter; the CLI does not expose it.
- The access token is acquired before the gql client opens, so an interactive browser
  login runs outside gql's per-request deadline rather than being cancelled and retried.
- SSL verification can be overridden via `--no-verify-ssl`; the `REQUESTS_CA_BUNDLE` env var applies to both `requests` and `aiohttp`.
