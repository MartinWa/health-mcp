# Security

Health MCP is a read-only aggregation layer over personal health data. It holds no
database and writes no health records; it reads its configured sources through
read-only interfaces, shapes the results and caches raw responses on local disk. Run
it for a single trusted operator and never expose it without the bearer-auth HTTP
mode behind TLS.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub advisory reporting:

<https://github.com/MartinWa/health-mcp/security/advisories/new>

Do not open a public issue for a security problem. Include the affected version,
the impact and a minimal reproduction with synthetic data. Reports are acknowledged
within 7 days; fixes are coordinated privately and the advisory is published after
disclosure.

## Personal data

The sources are personal health data and this repository is public, so it must contain
none of it:

- no real sleep, weight, activity, recovery, workout or lifting values
- no real wearables elements, medicine values, notes, dates or identifiers
- no goals, program intent, conditions, injuries, allergies, regimen, doses, symptoms or
  other `data/profile/` content (`profile.yaml`, `events.csv`)
- no real names, accounts, e-mail addresses, tokens or token hashes
- no real API endpoints, hostnames, volume paths, usernames or UIDs
- no cache directories, profile exports or diagnostics from a live installation

Fixtures, screenshots, examples and documentation are wholly synthetic; redacting real
data is not sufficient. An optional test may read a live source when credentials are
present and is skipped otherwise; committed tests never do. CI fails if personal data
or a secret value is present (`bash scripts/pii_audit.sh`).

## What the server does and does not protect

- Sources are read-only. Do not add write paths: the upstream tokens are the only
  credentials involved and they must not gain write scope for this service.
- HTTP mode fails closed: with neither `MCP_BEARER_TOKENS` nor `MCP_BEARER_TOKEN` set it
  refuses to start. Tokens are stored as SHA-256 digests, compared in constant time and
  never logged. `GET`/`HEAD /healthz` is the only anonymous route.
- Tool arguments and results are never logged; only name, duration and status are.
- Returning data to a model moves it to that provider. Treat every consumer's logs,
  transcripts and memory as another copy of the same health data.
- The profile directory is read-only to this service and the cache is local. Cache files
  contain raw upstream responses: protect them like the data they came from, and clear
  them with `refresh_cache` or by deleting the directory.
- The image runs as `1000:100` and makes no egress beyond the configured source APIs;
  the FastMCP update check and banner are disabled. Pin base images by digest at
  deploy time.

## Self-hosting

Keep the service on a private network behind a TLS reverse proxy, with one bearer token
per consumer so a token can be revoked alone. Do not run multiple workers: the cache has
no cross-process locking. Report problems with `get_status`, which shows configured
sources without printing credentials.
