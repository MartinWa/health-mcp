<p align="center">
  <a href="https://github.com/MartinWa/health-mcp/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/MartinWa/health-mcp/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/MartinWa/health-mcp/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/MartinWa/health-mcp?label=release"></a>
  <a href="https://github.com/MartinWa/health-mcp/pkgs/container/health-mcp"><img alt="Container image on GHCR" src="https://img.shields.io/badge/ghcr.io-health--mcp%3Alatest-2496ED?logo=docker&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: PolyForm Noncommercial 1.0.0" src="https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue"></a>
</p>

# Health MCP

A small [Model Context Protocol](https://modelcontextprotocol.io) server for
personal health data. It exposes stats-first tools over read-only sources and is
designed to run locally (stdio) or as a container behind a reverse proxy
(streamable HTTP with bearer auth).

## Data sources

| Source | Data | Access |
| --- | --- | --- |
| [Open Wearables](https://openwearables.io) | sleep, weight, activity, recovery, workouts, health scores, time series, identity/body data | `OW_API_KEY`, `OW_USER_ID`, `OW_BASE_URL` |
| [strong-data](https://github.com/MartinWa/strong-data) | lifting sessions, sets, volume, records, yearly progress | `STRONG_API_URL`, `STRONG_API_TOKEN` |
| Profile files | goals, constraints, regimen, event log | `PROFILE_DIR` (`profile.yaml`, `events.csv`), read-only to the server |

Every source is optional: enable it by setting its credentials (or, for a
file-backed source, by providing its files), or restrict the set explicitly with
`SOURCES`. With no source configured the server refuses to start; with one
configured the other's tools are simply not registered and composites report it
in `missing`. Enabling a source is a new module under `sources/` only.

The set is resolved once, at startup. One `INFO` line and `get_status.skipped`
name what is live and why the rest is missing:

```json
{"message": "sources", "enabled": ["openwearables", "strong"],
 "skipped": {"events": "no events.csv at /app/data/profile/events.csv"}}
```

Adding a file the deployment expects therefore needs a restart, not a
`refresh_cache` call; editing an existing file is re-read on the next call. A
source that `SOURCES` lists but that is not configured is logged as a warning.

Everything the server reads is read-only: the upstream APIs and, when mounted,
the profile directory. It has no write path to a source or to the profile and no
database access, and it does no lifting CSV parsing — `strong-data` imports the
Strong export, and this server only reads its API.

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

(`uvicorn health_mcp.asgi:app` also works, but `python -m health_mcp.server` is
the supported entrypoint: JSON logs from the first line, no uvicorn banner or access
log, and `MCP_HOST`/`MCP_PORT`/`MCP_PATH` honoured. Logs go to **stderr** in stdio
mode — stdout stays the JSON-RPC stream — and to stdout in HTTP mode, the only mode
where the stream carries nothing else.)

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
a `resolution` level instead, and translates its type names to Open Wearables'
own: `hrv` is `heart_rate_variability_rmssd`, `spo2` is `oxygen_saturation`,
`calories` is `active_energy`, `temperature` is `body_temperature`; the response
reports `types_requested` and `types_resolved`. **Sleep is not a timeseries
type** in Open Wearables — `sleep` there is a 400, so use `get_sleep_summary` or
`get_sleep_sessions`.

| Source | Tools |
| --- | --- |
| Open Wearables | `get_weight_trend`, `get_sleep_summary`, `get_activity_summary`, `get_recovery_summary`, `get_workouts`, `get_health_scores`, `get_timeseries`, `get_body`, `get_data_sources`, `get_sleep_sessions`, `get_consistency` |
| `strong-data` | `get_lifting_summary`, `get_lifting_trend`, `get_lifting_breakdown`, `get_lifting_sessions`, `get_exercise_detail`, `get_lifting_records`, `get_lifting_sets`, `get_yearly_progress` |
| Profile files | `get_medicine_logs` |
| Composite | `get_overview`, `get_daily_checkin`, `get_sleep_training_correlation`, `get_profile`, `get_medication_adherence`, `get_goal_progress`, `get_readiness`, `get_workload`, `get_medicine_impact` |
| Any source | `get_status`, `refresh_cache` |

Open Wearables tools need the `OW_*` variables, the lifting tools need
`STRONG_API_*`, and the composites use both. `get_status` and `refresh_cache` are
registered whenever the server starts.

`get_overview` and `get_daily_checkin` draw on both sources; without one they
return the partial answer plus `"missing": [...]`, while
`get_sleep_training_correlation` returns an error naming the missing source.
A locally rejected argument (a reversed range, a range over 1830 days, an unknown
`section`) answers `source: "local"` with a hint that no upstream call was made,
and its message names both the requested span and the cap — an upstream failure
keeps `source: openwearables`/`strong` and its `cache_age_hours`. In
`get_status`, `cache_age_hours: null` means nothing is cached yet and the source
block says so with `cache_empty: true`.
A single Open Wearables block that fails (for example an upstream 500 on activity)
is named in `missing` as `openwearables.<block>` and carries the upstream message
in its `error` field, so a broken source never reads as "no data". A block whose
page is capped is listed in `capped` (with `returned`/`has_more` and a note), which
is a display limit, not a fetch ceiling — `fetch_truncated` keeps that meaning.
`get_sleep_training_correlation` defaults to `lag_days=1` (lifting volume on day
D vs sleep on D+1) and always reports `n`, `lag_days`, `definition` and
`period`; `r` is added once there are at least three overlapping days.

The lifting detail tools stay opt-in: `get_exercise_detail` returns one exercise's
progress plus its best sets by rep count (an empty result also carries
`suggestions` from the exercise list, so `Squatt` is told apart from a genuinely
untrained exercise), `get_lifting_records` ranks estimated
1RM (Epley) or bodyweight sets, `get_lifting_sets` returns raw sets capped at 200
rows, and `get_yearly_progress` takes `year`/`as_of` instead of a date range and
only fetches the cumulative curve with `include_curve=true`.

The coaching tools (Tier C/D) are stateless wrappers over the same data and say
what they compute: `get_body` splits latest values from averaged vitals and says
that the static fields carry no per-field timestamp upstream (`static_dating`;
`get_weight_trend` has a dated series),
`get_data_sources` merges devices, connections and per-type counts,
`get_sleep_sessions` summarises sessions and naps (at most 25 rows),
`get_consistency` reports logging percentages, missing days and streaks,
`get_medication_adherence` compares the planned regimen with logged doses —
expected doses are counted on each plan row's own grid (its `started` date, else
the period start), so a weekly course expects the same days its event rows count;
`per_day` is `expected` spread over the requested period while
`scheduled_per_day` is the regimen's own rate — and `get_goal_progress` reads its
target from `profile.yaml` (an explicit `target` is labelled as an override),
`get_readiness` returns per-day inputs and no score,
`get_workload` flags weekly volume against its 4-week ACWR baseline, and
`get_medicine_impact` compares same-day metrics — an association, not causality.
Goal targets come from `profile.yaml`: `kg` and `lb` are converted and any other
weight unit is rejected. `get_activity_summary` reports totals and averages for
active minutes, distance and floors alongside steps and calories; `weekly_volume`
in `get_lifting_summary` marks weeks the requested range clipped (`partial`), so
a short first or last week is not read as a real drop. When a local file is
unreadable or missing, or an upstream fetch hits the page ceiling, the affected answer returns an error or an
`exact: false` warning instead of a quiet subset.

Open Wearables answers a repeated cursor with `500` on some summary endpoints
(activity among them), so a cursor page that 5xxes is not retried; the client
re-reads the same rows as disjoint date windows instead. `429` and network
errors keep their retries, and endpoints without a window fallback (timeseries,
health scores, sleep sessions) keep every retry. A range longer than the page
size therefore still returns every day; only a window that fails on its own first
page is reported as incomplete.

Incompleteness is never silent: the per-source tools report `fetch_truncated`,
`get_overview` and `get_daily_checkin` report it per block
(`{"activity": true, ...}`), and `get_sleep_training_correlation` reports
`fetch_truncated: {"sleep": true}` when its sleep series was incomplete. Workout
windows overlap by a day when a split happens, so a session crossing midnight is
fetched once and never dropped. Recovery, readiness and stress scores exist only
if the wearables that produce them are connected — without one, those blocks
answer `No data for this period`, which is data availability, not a failure.

Generate the exact list for a deployment (this is the only sanctioned source for
the README table and for the ZeroClaw `auto_approve` list):

```bash
uv run python -m health_mcp.tools --names --all-sources
uv run python -m health_mcp.tools --names --agent health --prefix health__
uv run python -m health_mcp.tools --names --agent health-coach --prefix health__
```

`--agent` selects the `health` or `health-coach` exposure set. Tier A, the core
composites and `get_profile` are on both; Tier B (lifting detail/records) is
`health` only, and Tier C/D (body, data sources, sleep sessions, consistency,
medicine, coaching composites) is `health-coach` only. The generated names, not
this table, are authoritative.

## Profile and event log

Goals, constraints and the regimen have no upstream source, so they are authored
by hand as files in `PROFILE_DIR` (`data/profile/` locally). The server mounts
that directory read-only and never writes it; `python -m health_mcp.profile` is
the only writer, and it writes only there.

```text
data/profile/
  profile.yaml   # updated:, plus intent / constraints / regimen / notes sections
  events.csv     # date,time,kind,substance,dose,unit,reason,notes,end_date,every_days
```

Each section carries its own `updated:` so `get_status` can report staleness
(`oldest_section`). Birth date, sex, gender, height and body composition are
**not** in the file: Open Wearables owns them, and `get_profile` reads them from
there instead of asking.

`intent.program` takes `name`, and optionally `phase`, `started`,
`deload_every_weeks` (1–52), `sessions_per_week` (1–14), `progression` (free
text) and `workout_types` (normalized Open Wearables types such as
`strength_training`). Frequency and progression are authored intent, not something the tools
compute: compare `sessions_per_week` with the observed rate from
`get_lifting_summary.sessions_per_week`. (`get_consistency` counts distinct
workout **days** per source, so two sessions on one day count once — it answers
"how regular was I", not "how many sessions per week".)

`workout_types` scopes a `workouts` goal: without it, `get_goal_progress` counts
**every** workout type, so a 76-session lifting target is compared against walks
as well. With it, only those types count and the response echoes `counted_types`.
A goal that carries `by:` also reports `target_period` and `target_applies`, so a
2025 window against a 2026 target says it does not measure that goal instead of
returning 882%.

`events.csv` rows are typed by `kind`:

| kind | Needs | Meaning |
| --- | --- | --- |
| `medication` | `substance` | a dose; counted by adherence and medicine impact |
| `supplement` | `substance` | a dose; same counting as medication |
| `vaccination` | `substance` | a one-off; listed separately, never a dose |
| `symptom` | — | free text, listed separately |
| `note` | — | free text, listed separately |
| `measurement` | — | a reading the sources do not carry |

`dose` is a number and `unit` a separate string, so `50,µg` is a dose and a dose
written as `50 µg` is a row error. The last two columns let one row stand for a
regimen instead of one row per day:

| column | Meaning |
| --- | --- |
| `end_date` | the row also covers up to this date, inclusive; empty means the row's own day, unless `every_days` says it is ongoing |
| `every_days` | one dose every N days from `date` (1–366). With `end_date` it stops there; **without** one it is ongoing and covers through the end of whatever range you query. Only `medication`/`supplement` rows may be ongoing |

```
2024-09-05,08:00,medication,example-drug,50,µg,,,2026-09-30,1   # one dose daily, 757 days, then stopped
2026-04-06,,medication,example-drug,1,dose,,,2026-09-28,7      # one dose weekly, 26 doses
2025-01-01,,medication,example-drug,1,dose,,,,1               # ongoing: daily from 2025-01-01 on
2025-01-01,,medication,example-drug,1,dose,,,,14              # ongoing: every 14 days from 2025-01-01 on```

An ongoing row has no `end_date` and is never "closed" by the data: the dose-days
it covers stop at the queried `end_date`, and `get_medicine_logs`, adherence and
impact all treat the latest query window as its extent. There is no "today" in the
file, so the same row means "still taken" until an `end_date` is added.

A row with a window counts as one dose per covered day, not one dose: `count`
in `get_medicine_logs` becomes a number of dose-days, `dose_total` sums
dose × dose-days, adherence counts the covered days as logged, and medicine
impact uses them as exposed days. The grid is anchored to the row's own `date`,
so querying a window that starts mid-interval never shifts which days count.
The header is exactly the ten columns listed above; a file missing one of them is
rejected rather than guessed at.

In `regimen`, `medications` and `supplements` accept optional `started` and
`stopped` dates; adherence only expects doses between them, and a row whose
interval misses the requested period is reported with `expected: 0` and a note
instead of being counted as fully missed. `schedule` is read conservatively:
`daily`, `weekly`, `Nx daily` and `every N days` are interpreted, and
`every N weeks` is accepted as the same rate as `every 7N days` — the two are one
schedule, so write whichever form your data already uses rather than duplicating
a row.

One invalid row makes the whole events source unusable, not just that row:
`get_status.sources.events` drops to `ok: false` with the
first message in `error` and the first five in `errors`, and the coaching tools
that consume the log refuse their answers, returning the same block under
`events` (which is also where `valid` appears — the per-source status block has
`ok`, not `valid`). So check `get_status.sources.events.ok` after hand-editing
the file.

```bash
uv run python -m health_mcp.profile validate            # dotted field paths, non-zero exit
uv run python -m health_mcp.profile show                # print the parsed profile
```

If `profile.yaml` is missing or invalid the server still starts: the profile
tools report `present: false` or `valid: false` plus the error path, `get_status`
shows the same, and the API tools keep working. A broken file never loses the
last valid copy, but answers that depend on it (adherence, goal progress,
medicine impact) say so instead of guessing. Diagnostics carry field paths and
positions only, never the file's content. The MCP instructions carry the
profile's `intent` only — never `constraints` or `regimen`, which would be
re-sent on every unrelated question.

Creating either file after startup needs a restart; editing one is picked up on
the next call.

## Container

```bash
docker build -t health-mcp:dev .
```

The image runs as `1000:100`, exposes port 8000, writes only under the
`CACHE_DIR` mount and treats a `PROFILE_DIR` mount as read-only. It disables
FastMCP's update check and banner (`FASTMCP_CHECK_FOR_UPDATES=off`), so the
service makes no egress beyond its configured upstream APIs. The build applies
Debian point-release fixes and drops pip and the ensurepip wheels, because the
server installs and runs through uv alone. Pin the base images by digest at
deploy time via `--build-arg PYTHON_IMAGE=... --build-arg UV_IMAGE=...`. The
compose stack, reverse-proxy host and DNS rewrite live in a private deployment
repository, not here.

## Continuous integration and releases

`.github/workflows/ci.yml` runs the full check on every push, pull request, weekly
schedule and manual dispatch: lockfile, lint, formatting, types, tests, the PII audit,
a dependency audit, workflow linting, plus a production image build whose container is
started with synthetic configuration to verify the `/healthz` and bearer-auth contract
and scanned for known vulnerabilities. `dependency-review` blocks new high-severity
dependencies, zizmor audits the workflows and Scorecard publishes the OpenSSF result.

`main` is protected: squash-only pull requests, resolved review threads and the
`Checks (Linux)`, `Container (Linux)` and `Dependency review` checks; `X.Y.Z` tags are
protected against moving or deletion. Dependabot updates the Python, action and Docker
dependencies, and policy-compliant patch updates merge automatically.

`.github/workflows/release.yml` publishes a release: pushing a bare `X.Y.Z` tag on a
tested `main` commit builds `linux/amd64` and `linux/arm64`, checks both images,
publishes the multi-platform index to `ghcr.io/<owner>/health-mcp` as `<version>` and
`latest`, and attests provenance and a per-platform SBOM before creating the GitHub
Release. GHCR package visibility is independent of the repository, and the release job
ends by verifying that an anonymous client can pull the package. Visibility itself is a
package setting, not an API call (the packages API has no endpoint for it), so a first
release whose package GitHub created private needs it flipped once under Packages →
health-mcp → Package settings. The `latest` tag only moves for the current stable
release.

The reported version is the tag: the release build passes it as the
`HEALTH_MCP_VERSION` build arg, and the container check fails if `/healthz` and the
`org.opencontainers.image.version` label disagree. `pyproject.toml`'s
`project.version` only feeds package metadata, so bumping it is optional. A run takes
~10 minutes because arm64 is emulated, and `latest` only moves on success — wait for
green or deploy the version tag, then confirm what runs:

```bash
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  ghcr.io/<owner>/health-mcp:<version>
```

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
payloads only; never commit `.env`, caches or real health data. [SECURITY.md](SECURITY.md)
says what counts as personal data and how to report a vulnerability privately;
[CONTRIBUTING.md](CONTRIBUTING.md) covers the rules, the release process and the
contributor agreement.

## License

PolyForm Noncommercial License 1.0.0 — **source-available, not OSI open
source**. Noncommercial use is permitted; commercial use needs a separate
licence. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
