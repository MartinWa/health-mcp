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
publishes the multi-platform image, its attestations and the GitHub Release. Existing
version tags are never overwritten and re-running the workflow is safe, and pushing a
tag right after a merge is fine: the workflow waits for the CI run on that commit.

On a clean, committed `main` checkout in PowerShell, create an annotated tag:

```powershell
./tools/tag_release.ps1 -Version 0.1.0 -WhatIf
./tools/tag_release.ps1 -Version 0.1.0          # add -Push to publish
```

The helper refuses a dirty tree or a branch other than `main`, rejects a prefix or a
prerelease suffix, never overwrites an existing tag and only pushes when given `-Push`;
pushing the tag is what starts publication. Omitting `-Version` increments the patch
component of the latest stable tag, and the first release needs an explicit version.
The equivalent by hand:

```bash
git tag -a 0.2.0 -m "Release 0.2.0"
git push --no-follow-tags origin refs/tags/0.2.0
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
