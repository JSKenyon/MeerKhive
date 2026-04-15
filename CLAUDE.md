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

## Architecture

Three modules under `src/meerkhive/`:

- **[auth.py](src/meerkhive/auth.py)** — `KeycloakAuth` dataclass + `get_access_token()`. Tokens persisted at `~/.local/state/meerkhive/tokens.json` (honours `XDG_STATE_HOME`). PKCE loopback + paste fallback; token refresh on 401.
- **[archive.py](src/meerkhive/archive.py)** — `query_archive` / `query_archive_async`, `build_selection_block`, `parse_filters`, `parse_sort`, `AuthenticatedTransport`, `build_ssl_context`.
- **[cli.py](src/meerkhive/cli.py)** — Typer CLI; thin wrapper over `query_archive_async`. Options: `--fields`, `--exclude-fields`, `--filter`, `--sort`, `--url-format`, `--show-fields`, `--verify-ssl`.
- **[\_\_init\_\_.py](src/meerkhive/__init__.py)** — re-exports public API only.

**Data flow**: `main()` → `query_archive_async()` → `AuthenticatedTransport` (injects bearer token, retries on 401) → introspects `Observation` type via `build_selection_block()` → paginates `observations` cursor → prints NDJSON to stdout. Logs go to stderr.

**GraphQL query building**: `build_selection_block()` walks the live `Observation` type to depth 3. Skips fields with required args unless an override exists. `DEFAULT_FIELD_OVERRIDES` handles `rdb(internal: bool)`.

**Filters**: `parse_filters()` maps `key=value` args to `[{field, value}]`. Special cases: `from`/`to` → `dateRange`; `radec` → JSON-parsed dict; `Band`, `QA2`, `NumFreqChannels` → comma-split lists.
