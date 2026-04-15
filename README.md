# MeerKhive

A Python client for the [SARAO MeerKAT archive](https://archive.sarao.ac.za). MeerKhive
authenticates via PKCE OAuth2, introspects the live GraphQL schema to build queries
dynamically, and returns results as plain Python dicts. It ships with a CLI that writes
NDJSON to stdout so the output is pipeable to `jq`, `grep`, and similar tools.

## Requirements

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- A SARAO archive account (required at runtime for authentication)

## Installation

### From source

```bash
git clone https://github.com/JSKenyon/MeerKhive.git
cd MeerKhive
uv sync
```

The CLI entry point is then available inside the project's virtual environment:

```bash
source .venv/bin/activate
meerkhive --help
```

### As a dependency in another project

```bash
uv add git+https://github.com/JSKenyon/MeerKhive.git
```

or, with `uv pip` in an existing environment:

```bash
uv pip install git+https://github.com/JSKenyon/MeerKhive.git
```

## Authentication

MeerKhive uses PKCE OAuth2 against the SARAO Keycloak realm. On first use it opens a
browser window for interactive login and saves the resulting tokens to
`~/.local/state/meerkhive/tokens.json`. Subsequent invocations silently refresh the
access token from that file.

The token file location respects `XDG_STATE_HOME` if set:

```bash
export XDG_STATE_HOME=/custom/state
# tokens will be saved to /custom/state/meerkhive/tokens.json
```

## CLI usage

### Basic query

```bash
# Fetch the 10 most recent observations, all fields
meerkhive --limit 10

# Select specific fields only
meerkhive --fields CaptureBlockId,StartTime,Band --limit 10

# Exclude noisy fields from the default full selection
meerkhive --exclude-fields products,FileSize --limit 20
```

### Filtering

Filters use `--filter key=value` syntax and are repeatable. Date ranges, multi-value
lists, and spatial (RA/Dec) queries are handled automatically:

```bash
# L-band observations in January 2024
meerkhive --filter Band=L --filter from=2024-01-01 --filter to=2024-01-31 --limit 50

# Multiple bands at once
meerkhive --filter Band=L,UHF --limit 20

# Free-text search
meerkhive --search "NGC1234" --limit 10

# RA/Dec cone search (JSON value)
meerkhive --filter 'radec={"ra": 83.82, "dec": -5.39}' --limit 10
```

### Sorting

```bash
# Most recent observations first
meerkhive --sort StartTime:desc --limit 10

# Sort by multiple columns
meerkhive --sort StartTime:desc --sort CaptureBlockId:asc --limit 10
```

### Introspecting the schema

`--show-fields` connects to the archive, introspects the live GraphQL schema, and prints
the full selection block that would be used for `--fields '*'`:

```bash
meerkhive --show-fields
```

### Piping to jq

All observation records are written to stdout as NDJSON (one JSON object per line), so
they compose naturally with `jq`:

```bash
# Extract just the CaptureBlockId and StartTime from the first 5 results
meerkhive --fields CaptureBlockId,StartTime --limit 5 | jq '{id: .CaptureBlockId, start: .StartTime}'

# Count by band
meerkhive --fields Band --limit 500 | jq -r '.Band' | sort | uniq -c | sort -rn
```

### Internal URLs (SARAO network)

By default, URL-valued fields (e.g. `rdb`) are rendered as public internet URLs.
On the SARAO internal network, pass `--url-format internal` to get intranet URLs instead:

```bash
meerkhive --url-format internal --limit 5
```

### SSL (development only)

```bash
meerkhive --no-check-certificate --auth-address https://dev.archive.example.com --limit 3
```

## Python API

### Synchronous query

```python
from meerkhive import query_archive

records = query_archive(
    fields="CaptureBlockId,StartTime,Band",
    limit=10,
)
for r in records:
    print(r["CaptureBlockId"], r["StartTime"])
```

### Filtering and sorting

```python
from meerkhive import query_archive, parse_filters

records = query_archive(
    fields="CaptureBlockId,StartTime",
    filters=parse_filters(["Band=L", "from=2024-01-01", "to=2024-03-31"]),
    sort=["StartTime:desc"],
    limit=50,
)
```

### Async query

```python
import asyncio
from meerkhive import query_archive_async, parse_filters

async def main() -> None:
    records = await query_archive_async(
        fields="CaptureBlockId,Band,IntegrationTime",
        filters=parse_filters(["Band=L,UHF"]),
        sort=["StartTime:desc"],
        limit=100,
    )
    for r in records:
        print(r)

asyncio.run(main())
```

### parse_filters reference

`parse_filters` converts a list of `"key=value"` strings to the GraphQL filter format.
Special-cased keys:

| Key | Behaviour |
|-----|-----------|
| `from` / `to` | Normalised to midnight UTC and combined into a `dateRange` filter |
| `Band`, `QA2`, `NumFreqChannels` | Comma-separated values are split into a list |
| `radec` | Value is parsed as JSON: `'{"ra": 83.82, "dec": -5.39}'` |
| All others | Passed through as-is |

```python
from meerkhive import parse_filters

filters = parse_filters([
    "Band=L,UHF",
    "from=2024-01-01",
    "to=2024-06-30",
])
# [
#   {"field": "Band", "value": ["L", "UHF"]},
#   {"field": "dateRange", "value": ["2024-01-01T00:00:00.000Z", "2024-06-30T00:00:00.000Z"]},
# ]
```

### Advanced: custom transport

For full control over the GraphQL session (e.g. adding custom middleware):

```python
from meerkhive import AuthenticatedTransport, KeycloakAuth, build_ssl_context
from gql.client import Client

auth = KeycloakAuth.default()
transport = AuthenticatedTransport(
    url="https://archive.sarao.ac.za/graphql",
    auth=auth,
    ssl_context=build_ssl_context(verify=True),
)

async with Client(transport=transport, fetch_schema_from_transport=True) as session:
    # Execute arbitrary GraphQL queries against the archive.
    ...
```

## Developer setup

```bash
# Install all dependencies including dev extras
uv sync --all-groups

# Install pre-commit hooks (ruff check + ruff format)
source .venv/bin/activate
pre-commit install
```

### Running tests

```bash
# Fast offline unit tests (no credentials needed)
source .venv/bin/activate && python -m pytest tests/ -v

# Live integration test against the production archive (requires valid tokens)
MEERKHIVE_LIVE_TOKENS=~/.local/state/meerkhive/tokens.json \
  python -m pytest tests/test_archive_live.py -m slow -v
```

### Linting and formatting

```bash
ruff check .
ruff format .
```
