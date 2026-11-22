# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS python-base

# Apply Debian point-release fixes to the inherited packages before building.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

FROM python-base AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_ENABLE_RICH_LOGGING=false \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /app/data/cache \
    && chown -R 1000:100 /app

# The server installs and runs through uv only, so pip and the ensurepip bundled
# wheels are dropped rather than shipped with their vendored dependencies.
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && /usr/local/bin/python -c "import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent / '_bundled')"

USER 1000:100
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "health_mcp.server"]
