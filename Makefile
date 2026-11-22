.PHONY: check lint format format-check type test audit

check: lint format-check type test audit

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

type:
	uv run pyright

test:
	uv run pytest -q

audit:
	bash scripts/pii_audit.sh
