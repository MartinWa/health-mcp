"""Streamable HTTP entrypoint: bearer auth at the app layer, anonymous /healthz."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .config import Settings
from .log import configure_logging
from .server import create_server
from .sources.base import Source
from .store import CacheStore
from .utils import today_local

log = logging.getLogger("health_mcp.access")


class BearerAuthMiddleware:
    def __init__(self, app: Any, digests: Mapping[str, str], healthz_path: str = "/healthz"):
        self.app = app
        self.digests = tuple(digest.lower() for digest in digests.values())
        self.healthz_path = healthz_path

    def _matches(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = False
        for expected in self.digests:
            matched |= secrets.compare_digest(digest, expected)
        return matched

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path == self.healthz_path:
            if method not in ("GET", "HEAD"):
                await _json_response(send, 405, {"error": "method not allowed"})
                return
            await self.app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if not token or not self._matches(token):
            await _json_response(
                send,
                401,
                {"error": "unauthorized"},
                headers=[(b"www-authenticate", b"Bearer")],
            )
            return

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                extra={
                    "fields": {
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    }
                },
            )


def create_app(
    settings: Settings,
    *,
    sources: Mapping[str, Source] | None = None,
    cache: CacheStore | None = None,
) -> Any:
    configure_logging(settings.log_level)
    digests = settings.bearer_digests()
    if not digests:
        raise RuntimeError(
            "HTTP mode requires MCP_BEARER_TOKENS (name:sha256(token)) or MCP_BEARER_TOKEN; "
            "refusing to start without auth"
        )
    mcp: FastMCP = create_server(settings, sources=sources, cache=cache)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "today": today_local(settings.tz),
            }
        )

    app = mcp.http_app(path=settings.mcp_path, stateless_http=True)
    return BearerAuthMiddleware(app, digests)


class LazyApp:
    """Build the real app on first use so importing the module needs no credentials."""

    def __init__(self) -> None:
        self._app: Any = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = create_app(Settings())
        await self._app(scope, receive, send)


app = LazyApp()


def _bearer_token(scope: Mapping[str, Any]) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"authorization":
            header = value.decode("latin-1").strip()
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return ""
    return ""


async def _json_response(
    send: Any,
    status: int,
    body: dict[str, Any],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode("utf-8")
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        *(headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": payload})
