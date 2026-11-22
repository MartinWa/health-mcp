#!/usr/bin/env bash
# One command for the whole definition of done. Works with or without make.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
bash scripts/pii_audit.sh
