# Contributing

Thanks for helping. Setup and the command list are in the [README](README.md); this
page covers the rules and the paperwork.

## Before opening a pull request

Run `bash scripts/check.sh` and make sure CI is green. Discuss larger changes in an
issue first.

## Rules

- The server stays read-only. No write paths to Open Wearables, `strong-data` or the
  profile directory, and no new outbound calls beyond the configured sources.
- Dates crossing the tool boundary are `YYYY-MM-DD` and validated once in
  `health_mcp.utils`; installation-local calendar dates (`TZ`), never UTC-truncated.
- New behaviour needs a test, tool names and result shapes stay stable, and code stays
  free of comments unless a change explicitly requires them.
- Keep fixtures, screenshots and examples wholly synthetic. Never commit personal
  health data, `.env` files, caches or secrets; [SECURITY.md](SECURITY.md) says what
  counts as personal data.

## Repository layout

Sources own their client, cache namespace and tools, so enabling a source is a new
module under `src/health_mcp/sources/`. Pure shaping and statistics belong in
`analysis.py`; date validation and caps in `utils.py`. Deployment and release tooling
lives in `tools/`; `scripts/` holds the local checks.

## Releases

Tags are bare `X.Y.Z` on a tested `main` commit; `.github/workflows/release.yml` then
publishes the multi-platform image, its attestations and the GitHub Release. Create one
from `main` after CI is green:

```bash
git tag 0.2.0
git push origin 0.2.0
```

## Contributor agreement

Before your first pull request can be merged, sign the
[Contributor License Agreement](CLA.md). An app comments on your pull request with
a signing link; click it once and the signature covers your future contributions
too. You keep the copyright in your contribution.

The agreement matters because the project is dual licensed: it is published under
the PolyForm Noncommercial License 1.0.0 and also offered commercially, and the
agreement is what lets a contribution be part of both. If you contribute in the
course of employment, read section 6 first: your employer may own the work, and an
authorised person has to sign for the entity. There is no Developer Certificate of
Origin (`Signed-off-by`), because a DCO certifies distribution under the current
licence only and does not permit relicensing.

### For maintainers

Signatures are collected by [CLA Assistant](https://cla-assistant.io), a free GitHub
app, not by a workflow in this repository:

1. Create a public gist containing `CLA.md` and copy its URL.
2. Sign in at <https://cla-assistant.io> with GitHub, add this repository, and paste
   the gist URL.
3. Make **CLA Assistant** a required status check in the `Protect main` ruleset.

Signatures are stored in that gist, so contributor accounts never enter this
repository.

## License

Contributions are licensed under the PolyForm Noncommercial License 1.0.0 (see
[LICENSE](LICENSE) and [NOTICE](NOTICE)) and under the terms of the Contributor
License Agreement.
