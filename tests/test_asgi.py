from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from helpers import FakeSource, both_settings

from health_mcp.asgi import BearerAuthMiddleware, create_app
from health_mcp.config import hash_token


def run(coro):
    return asyncio.run(coro)


async def _dummy_app(scope, receive, send):
    if scope["type"] == "lifespan":
        await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})
        return
    body = b'{"forwarded":true}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def make_middleware():
    return BearerAuthMiddleware(_dummy_app, {"test": hash_token("good-token")})


def request(app, method: str, path: str, token: str | None = None) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=app)
        headers = {"authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers)

    return run(go())


def test_healthz_is_anonymous():
    response = request(make_middleware(), "GET", "/healthz")
    assert response.status_code == 200
    assert response.json() == {"forwarded": True}


def test_non_get_on_healthz_is_405_not_an_auth_bypass():
    response = request(make_middleware(), "POST", "/healthz")
    assert response.status_code == 405


def test_mcp_requires_a_bearer_token():
    response = request(make_middleware(), "GET", "/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": "unauthorized"}


def test_wrong_token_is_rejected_without_detail():
    response = request(make_middleware(), "GET", "/mcp", token="wrong")
    assert response.status_code == 401


def test_valid_token_is_forwarded():
    response = request(make_middleware(), "GET", "/mcp", token="good-token")
    assert response.status_code == 200
    assert response.json() == {"forwarded": True}


def test_malformed_authorization_header_is_rejected():
    assert request(make_middleware(), "GET", "/mcp", token="  ").status_code == 401


def test_lifespan_scope_is_forwarded():
    messages: list[str] = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        messages.append(message["type"])

    run(make_middleware()({"type": "lifespan"}, receive, send))
    assert messages == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_create_app_refuses_to_start_without_a_bearer_token(tmp_path):
    settings = both_settings(tmp_path)
    with pytest.raises(RuntimeError, match="requires MCP_BEARER_TOKENS"):
        create_app(settings, sources={"strong": FakeSource("strong")})


def test_http_endpoint_401_without_token_and_healthz_200(tmp_path):
    settings = both_settings(tmp_path, mcp_bearer_tokens=f"test:{hash_token('good-token')}")
    app = create_app(settings, sources={"strong": FakeSource("strong")})

    health = request(app, "GET", "/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    unauthorized = request(app, "POST", settings.mcp_path)
    assert unauthorized.status_code == 401


def test_initialize_succeeds_with_a_valid_token(tmp_path):
    settings = both_settings(tmp_path, mcp_bearer_tokens=f"test:{hash_token('good-token')}")
    app = create_app(settings, sources={"strong": FakeSource("strong")})
    inner = app.app
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }

    async def go():
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    settings.mcp_path,
                    json=payload,
                    headers={
                        "authorization": "Bearer good-token",
                        "accept": "application/json, text/event-stream",
                    },
                )
                return response

    response = run(go())
    assert response.status_code == 200
    assert "jsonrpc" in json.dumps(response.text)
