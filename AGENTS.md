# AGENTS.md

Instructions for automated agents working in this repository.

## Ground rules

- Follow `planning/plan.md` phase by phase. `planning/` is private: never commit
  it or copy infrastructure details, hostnames or personal data into tracked
  files.
- All health data is personal. Never commit `.env`, data files, caches, tokens,
  real workout/medicine values or fixtures derived from them. Examples and tests
  must be synthetic.
- The server is read-only: Open Wearables and `strong-data` are read-only APIs,
  and the profile directory is read-only to this service. Do not add write paths
  to the sources.
- Dates crossing the tool boundary are `YYYY-MM-DD` and are validated once in
  `health_mcp.utils`. Dates are installation-local calendar dates (`TZ`), never
  UTC-truncated.
- No comments in code unless they explain a non-obvious decision.

## Commands

Run from the repository root:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
bash scripts/check.sh        # lint + format + types + tests + PII audit
```

Local server:

```bash
uv run python -m health_mcp.server
```

## Layout

- `src/health_mcp/server.py` — FastMCP server factory, composite tools, lifespan
- `src/health_mcp/sources/` — one module per source; a source owns its client,
  cache namespace and tools
- `src/health_mcp/client.py` — Open Wearables API client
- `src/health_mcp/strong_client.py` — `strong-data` API client
- `src/health_mcp/store.py` — cache primitives (paths, TTL, bounded sweep)
- `src/health_mcp/analysis.py` — pure shaping/statistics, no I/O
- `src/health_mcp/utils.py` — date validation, filtering, caps
- `src/health_mcp/tools.py` — tool-name generator for docs and agent config
- `tests/` — unit tests with synthetic payloads
- `planning/` — private notes (gitignored)

## Definition of done

`bash scripts/check.sh` passes, no secrets or personal data are present, and the
affected tool still returns `{"error": ...}` instead of a traceback when a
source is unavailable.
