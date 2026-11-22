# Health MCP

A small [Model Context Protocol](https://modelcontextprotocol.io) server for
personal health data. It exposes stats-first tools over read-only sources and is
designed to run locally (stdio) or as a container behind a reverse proxy
(streamable HTTP with bearer auth).

## Data sources

| Source | Data | Access |
| --- | --- | --- |
| [Open Wearables](https://openwearables.io) | sleep, weight, activity, recovery, workouts, health scores, time series | `OW_API_KEY`, `OW_USER_ID`, `OW_BASE_URL` |
| [strong-data](https://github.com/MartinWa/strong-data) | lifting sessions, sets, volume, records, yearly progress | `STRONG_API_URL`, `STRONG_API_TOKEN` |

Every source is optional: enable it by setting its credentials, or restrict the
set explicitly with `SOURCES`. With neither configured the server refuses to
start; with one configured the other's tools are simply not registered and
composites report it in `missing`. Enabling a source is a new module under
`sources/` only.

There is no database access and no lifting CSV parsing in the server —
`strong-data` imports the Strong export, and this server only reads its API.

## Setup

```bash
cp .env.example .env   # fill in whichever sources you have
uv sync
```

Dates: every date crossing the tool boundary is `YYYY-MM-DD` and is an
installation-local calendar date (`TZ`, default `Europe/Stockholm`). Open
Wearables UTC timestamps are converted to `TZ` before truncation, never
formatted from UTC.

## Run

Local stdio, for an MCP client such as pi or OpenCode:

```bash
uv run python -m health_mcp.server
```

Streamable HTTP, for the container or a remote client:

```bash
MCP_TRANSPORT=http \
MCP_BEARER_TOKENS="pi:$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)" \
uv run python -m health_mcp.server
```

(`uvicorn health_mcp.asgi:app --host 0.0.0.0 --port 8000 --workers 1` is the
equivalent direct entrypoint; `MCP_TRANSPORT=http` makes `python -m
health_mcp.server` use it, honouring `MCP_HOST`/`MCP_PORT`/`MCP_PATH`.)

- `MCP_BEARER_TOKENS` is `name:sha256(token)` pairs, one per consumer; the
  single-consumer `MCP_BEARER_TOKEN` alias is hashed to `default`. HTTP mode
  fails closed when both are empty.
- The MCP endpoint is `MCP_PATH` (default `/mcp`).
- `GET /healthz` is the only anonymous route; any other method on it is `405`.
- One process, one cache. Do not run multiple workers: the cache has no
  cross-process locking.

## Tools

All range tools take `start_date`/`end_date` as `YYYY-MM-DD`. They return
aggregates by default and include a capped series only with `include_series=true`
(plus `total_points` and `truncated`). `get_timeseries` takes ISO timestamps and
a `resolution` level instead.

| Source | Tools |
| --- | --- |
| Open Wearables | `get_weight_trend`, `get_sleep_summary`, `get_activity_summary`, `get_recovery_summary`, `get_workouts`, `get_health_scores`, `get_timeseries` |
| `strong-data` | `get_lifting_summary`, `get_lifting_trend`, `get_lifting_breakdown`, `get_lifting_sessions` |
| Composite | `get_overview`, `get_daily_checkin`, `get_sleep_training_correlation` |
| Any source | `get_status`, `refresh_cache` |

The composite tools need both sources; without one they return the partial
answer plus `"missing": [...]`. `get_sleep_training_correlation` defaults to
`lag_days=1` (lifting volume on day D vs sleep on D+1) and always reports `n`,
`r`, `lag_days`, `definition` and `period`.

Generate the exact list for a deployment (this is the only sanctioned source for
the README table and for the ZeroClaw `auto_approve` list):

```bash
uv run python -m health_mcp.tools --names --all-sources
uv run python -m health_mcp.tools --names --agent health --prefix health__
```

## Container

```bash
docker build -t health-mcp:dev .
```

The image runs as `1000:100`, needs `PROFILE_DIR` and `CACHE_DIR` mounts, and
exposes port 8000. It disables FastMCP's update check and banner
(`FASTMCP_CHECK_FOR_UPDATES=off`), so the service makes no egress beyond its two
upstream APIs. Pin the base images by digest at deploy time via
`--build-arg PYTHON_IMAGE=... --build-arg UV_IMAGE=...`. The compose stack,
Caddy host and AdGuard rewrite live in the `olympus-stacks` repo, not here.

`.github/workflows/ci.yml` runs the full check on every push and pull request;
`publish.yml` builds `linux/amd64` on `main` and tags and pushes
`ghcr.io/<owner>/health-mcp` with `latest`, `main-<sha>` and semver tags. GHCR
package visibility is independent of the repository; the NAS pulls with a
`read:packages` PAT.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
bash scripts/pii_audit.sh    # PII / secret scan
bash scripts/check.sh        # all of the above
```

`planning/` is private working material and is not tracked. Tests use synthetic
payloads only; never commit `.env`, caches or real health data.

## License

PolyForm Noncommercial License 1.0.0 — **source-available, not OSI open
source**. Noncommercial use is permitted; commercial use is not granted. See
[LICENSE](LICENSE).
