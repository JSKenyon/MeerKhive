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

`src/meerkhive/` contains four modules:

- **auth.py** — PKCE OAuth2 login, token refresh, and token persistence (`~/.local/state/meerkhive/tokens.json`). Public entry point: `get_access_token()`.
- **archive.py** — GraphQL client. Introspects the live `Observation` schema to build a selection block, paginates results, and handles bearer token injection with 401 retry. Public entry points: `query_archive()` / `query_archive_async()`.
- **cli.py** — Typer CLI wrapping `query_archive`. Options: `--fields`, `--exclude-fields`, `--filter`, `--sort`, `--search`, `--limit`, `--url-format`, `--show-fields`, `--verify-ssl`.
- **\_\_init\_\_.py** — re-exports public API only.

## Key Behaviours

- Schema is introspected at runtime; field names are discovered dynamically. Use `--show-fields` to list them.
- Filters: `key=value` pairs; some keys are JSON-parsed (`dateRange`, `radec`) or comma-split lists (`Band`, `QA2`, `NumFreqChannels`).
- `--url-format internal|external` controls whether URL-valued fields resolve inside SARAO or via the public internet.
- SSL verification can be overridden via `--no-verify-ssl`; the `REQUESTS_CA_BUNDLE` env var applies to both `requests` and `aiohttp`.
