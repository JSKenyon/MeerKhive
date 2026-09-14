# MeerKhive

A Python client for the [SARAO MeerKAT archive](https://archive.sarao.ac.za). MeerKhive
authenticates via PKCE OAuth2, introspects the live GraphQL schema to build queries
dynamically, and returns results as plain Python dicts. It ships with a CLI that writes
NDJSON to stdout so the output is pipeable to `jq`, `grep`, and similar tools.

## Requirements

- Python ≥ 3.11
- A SARAO archive account, for authentication at runtime
- [uv](https://docs.astral.sh/uv/), only to work on MeerKhive itself

## Installation

MeerKhive is published on [PyPI](https://pypi.org/project/meerkhive/):

```bash
uv pip install meerkhive        # or: pip install meerkhive
```

To add it as a dependency of another project:

```bash
uv add meerkhive
```

Either way the `meerkhive` command is installed alongside the library:

```bash
meerkhive --help
```

### From source

For an unreleased change, or to work on MeerKhive itself:

```bash
git clone https://github.com/JSKenyon/MeerKhive.git
cd MeerKhive
uv sync
source .venv/bin/activate
meerkhive --help
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
meerkhive --fields CaptureBlockId,StartTime,band --limit 10

# Exclude the bulky nested blocks from the default full selection
meerkhive --exclude-fields missingItems,exports,beamformedProducts --limit 20
```

Field names are case-sensitive and come from the live schema, so check them with
`--show-fields` rather than guessing. Note that they are a different namespace from
filter keys: the band of an observation is selected as `band` but filtered on as
`Band`.

### Filtering

Filters use `--filter key=value` syntax and are repeatable. Several keys have special
handling: `Band`, `QA2`, and `NumFreqChannels` accept comma-separated lists; `dateRange`
and `radec` values are parsed as JSON. All other keys are passed through as-is:

```bash
# L-band observations in January 2024
meerkhive --filter Band=L \
  --filter 'dateRange=["2024-01-01T00:00:00.000Z","2024-01-31T23:59:59.999Z"]' \
  --limit 50

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
meerkhive --fields band --limit 500 | jq -r '.band' | sort | uniq -c | sort -rn
```

### Pagination and timeouts

Results are fetched a page at a time. The archive caps a page at 100 records, which is
also the default, and the cost of a request is dominated by a fixed per-request
overhead rather than by the number of records — so smaller pages are slower overall,
not faster.

```bash
# Smaller pages: slower for large queries, but lighter on the archive.
meerkhive --page-size 25 --limit 500
```

`--page-timeout` sets the deadline for a single page request in seconds (default 120);
it bounds each request, not the query as a whole. It applies to both the GraphQL client
and the underlying HTTP session, so values above aiohttp's own 300 s default take effect
rather than being silently capped. Archive latency is highly variable, so each page is
attempted up to three times in total — the initial request plus two retries — with
exponential backoff. Connection failures and 5xx responses are retried on the same
terms. Progress is reported on stderr, leaving stdout clean for `jq`:

```bash
meerkhive --page-timeout 60 --limit 2000 > observations.ndjson
```

### SSL (development only)

```bash
meerkhive --no-verify-ssl --auth-address https://dev.archive.example.com --limit 3
```

## Python API

### Synchronous query

```python
from meerkhive import query_archive

records = query_archive(
    fields="CaptureBlockId,StartTime,band",
    limit=10,
)
for r in records:
    print(r["CaptureBlockId"], r["StartTime"])
```

### Filtering and sorting

```python
from meerkhive import query_archive

records = query_archive(
    fields="CaptureBlockId,StartTime",
    filters=[
        "Band=L",
        'dateRange=["2024-01-01T00:00:00.000Z","2024-03-31T23:59:59.999Z"]',
    ],
    sort=["StartTime:desc"],
    limit=50,
)
```

### Async query

```python
import asyncio
from meerkhive import query_archive_async

async def main() -> None:
    records = await query_archive_async(
        fields="CaptureBlockId,band,IntegrationTime",
        filters=["Band=L,UHF"],
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
| `dateRange` | Value is parsed as JSON: a two-element ISO 8601 array (use `null` for an open end), e.g. `'["2024-01-01T00:00:00.000Z", null]'` |
| `radec` | Value is parsed as JSON: `'{"ra": 83.82, "dec": -5.39}'` |
| `Band`, `QA2`, `NumFreqChannels` | Comma-separated values are split into a list |
| All others | Passed through as-is |

```python
from meerkhive import parse_filters

filters = parse_filters([
    "Band=L,UHF",
    'dateRange=["2024-01-01T00:00:00.000Z","2024-06-30T23:59:59.999Z"]',
])
# [
#   {"field": "Band", "value": ["L", "UHF"]},
#   {"field": "dateRange", "value": ["2024-01-01T00:00:00.000Z", "2024-06-30T23:59:59.999Z"]},
# ]
```

### Advanced: custom transport

For full control over the GraphQL session (e.g. adding custom middleware):

```python
import asyncio

from gql.client import Client

from meerkhive import AuthenticatedTransport, KeycloakAuth, build_ssl_context


async def main() -> None:
    auth = KeycloakAuth.default()
    transport = AuthenticatedTransport(
        url="https://archive.sarao.ac.za/graphql",
        auth=auth,
        ssl_context=build_ssl_context(verify=True),
        # Optional; without it aiohttp caps a request at its own 300 s default.
        request_timeout=120.0,
    )

    async with Client(
        transport=transport,
        fetch_schema_from_transport=True,
        execute_timeout=120.0,
    ) as session:
        # Execute arbitrary GraphQL queries against the archive.
        ...


asyncio.run(main())
```

Going this route means opting out of the retry, page-size and progress handling that
`query_archive` provides; `meerkhive.pagination.fetch_all_pages` can be used against
the session above if you want to keep the pagination but write the query yourself.

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
