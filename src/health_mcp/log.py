"""Structured logging: JSON lines to stdout, never arguments or payloads."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, default=str)


# HTTP clients log full request URLs at INFO: dates, user ids and exercise names.
# Those are health arguments, so the dependency loggers stay at WARNING and only
# our own records (tool name, duration, status) reach the log.
QUIET_LOGGERS = ("httpx", "httpcore")

# uvicorn installs plain-text handlers on these loggers. The access log is
# redundant (health_mcp.access already writes one JSON line per request with
# method, path, status and duration) and its startup banner is INFO, so all four
# stay at WARNING with no handlers of their own; warnings and errors still reach
# the root JSON handler. `uvicorn.access` does not propagate, so its records are
# dropped rather than duplicated.
UVICORN_LOGGERS: dict[str, bool] = {
    "uvicorn": True,
    "uvicorn.error": True,
    "uvicorn.asgi": True,
    "uvicorn.access": False,
}


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    quiet_uvicorn()


def quiet_uvicorn() -> None:
    """Keep uvicorn off the stream so every line stays the same JSON shape.

    `python -m health_mcp.server` also passes log_config=None, so this is the
    only place uvicorn's levels are set. A raw `uvicorn health_mcp.asgi:app` run
    configures itself first and is corrected here when the app is created.
    """
    for name, propagate in UVICORN_LOGGERS.items():
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = propagate
        logger.setLevel(logging.WARNING)
