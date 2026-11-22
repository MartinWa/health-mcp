#!/usr/bin/env bash
# PII / secret audit for this repository.
#
# Scans every file in the tree except vendored, generated and private paths,
# and fails on any hit that is not justified in scripts/pii_allowlist.txt.
# An allowlist line is "<extended-regex>|<reason>"; the regex is matched
# against whole rg output lines (file:line:match).
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! command -v rg >/dev/null 2>&1; then
  echo "pii_audit: ripgrep (rg) is required" >&2
  exit 2
fi

excludes=(
  --glob '!.git/**'
  --glob '!.venv/**'
  --glob '!planning/**'
  --glob '!data/**'
  --glob '!.ruff_cache/**'
  --glob '!.pytest_cache/**'
  --glob '!uv.lock'
  # The audit itself necessarily contains the patterns it looks for.
  --glob '!scripts/pii_audit.sh'
  --glob '!scripts/pii_allowlist.txt'
)

personal=(
  -e 'C:/Users/'
  -e 'C:\\\\Users'
  -e '/volume[0-9]+/'
  -e '192\.168\.'
  -e 'olympen'
  -e 'martin'
  -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(eu|com|org)'
  -e '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)

secrets=(
  -e '(api[_-]?key|token|secret|password|passwd)[[:space:]]*[:=][[:space:]]*["'"'][^"'"']{16,}["'"']'
  -e '(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)[[:space:]]*=[[:space:]]*[A-Za-z0-9_+/=-]{24,}[[:space:]]*$'
)

hits="$(
  {
    rg -n -i "${excludes[@]}" "${personal[@]}" . 2>/dev/null || true
    rg -n "${excludes[@]}" "${secrets[@]}" . 2>/dev/null || true
  } | sort -u
)"
allowed=0

if [[ -f scripts/pii_allowlist.txt ]]; then
  while IFS='|' read -r pattern reason; do
    [[ -z "${pattern// }" ]] && continue
    [[ "$pattern" == \#* ]] && continue
    [[ -z "$hits" ]] && break
    before="$(printf '%s\n' "$hits" | grep -cE -- "$pattern" || true)"
    if [[ "$before" -gt 0 ]]; then
      allowed=$((allowed + before))
      hits="$(printf '%s\n' "$hits" | grep -vE -- "$pattern" || true)"
    fi
  done < scripts/pii_allowlist.txt
fi

if [[ -n "${hits//[[:space:]]/}" ]]; then
  echo "pii_audit: FAIL — personal data or a secret value is present:" >&2
  printf '%s\n' "$hits" >&2
  echo >&2
  echo "Fix the hit, or add a narrow '<regex>|<reason>' line to" >&2
  echo "scripts/pii_allowlist.txt if it is a deliberate placeholder." >&2
  exit 1
fi

echo "pii_audit: OK (${allowed} allowlisted occurrence(s))"
