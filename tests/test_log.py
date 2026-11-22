"""The logging path must never carry health arguments."""

from __future__ import annotations

import asyncio
import logging

import httpx

from health_mcp.log import configure_logging


def run(coro):
    return asyncio.run(coro)


def test_http_client_loggers_are_pinned_above_info(capsys):
    configure_logging("INFO")
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert logger.level == logging.WARNING
        assert not logger.isEnabledFor(logging.INFO)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.AsyncClient(
        base_url="https://ow.test",
        transport=transport,
        headers={"X-Open-Wearables-API-Key": "test"},
    )

    async def go():
        try:
            await client.get("/api/v1/users/user-1/events/workouts?start_date=2026-11-01")
        finally:
            await client.aclose()

    run(go())
    captured = capsys.readouterr()
    assert "HTTP Request" not in captured.out + captured.err
    assert "user-1" not in captured.out + captured.err


def test_uvicorn_loggers_are_taken_off_the_stream(capsys):
    configure_logging("INFO")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.level == logging.WARNING
    assert logging.getLogger("uvicorn.access").propagate is False
    assert logging.getLogger("uvicorn.error").propagate is True


def test_stdio_logging_never_touches_stdout(capsys):
    """stdout carries JSON-RPC in the default transport, so logs go to stderr."""
    import json

    configure_logging("INFO")
    logging.getLogger("uvicorn.error").warning("Uvicorn running on http://0.0.0.0:8000")
    logging.getLogger("uvicorn.access").info('127.0.0.1:1 - "GET /healthz HTTP/1.1" 200 OK')

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [json.loads(line) for line in captured.err.splitlines()]
    assert [line["message"] for line in lines] == ["Uvicorn running on http://0.0.0.0:8000"]
    assert lines[0]["logger"] == "uvicorn.error"
    assert '"GET /healthz' not in captured.err


def test_http_entrypoint_logs_json_to_stdout(tmp_path, capsys):
    import json

    from helpers import FakeSource, make_settings

    from health_mcp.asgi import create_app
    from health_mcp.store import CacheStore

    settings = make_settings(tmp_path, mcp_bearer_token="synthetic", mcp_transport="http")
    cache = CacheStore(tmp_path / "cache", {"fake": 6})
    create_app(settings, sources={"fake": FakeSource("fake")}, cache=cache)

    logging.getLogger("health_mcp.access").info("request")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out.splitlines()[-1])["message"] == "request"


def test_fastmcp_records_reach_the_json_handler(capsys):
    import json

    import fastmcp

    configure_logging("INFO")
    assert fastmcp.settings.log_enabled is False

    logging.getLogger("fastmcp.server").info("Starting MCP server 'health' with transport 'stdio'")

    captured = capsys.readouterr()
    assert captured.out == ""
    line = json.loads(captured.err.splitlines()[-1])
    assert line["logger"] == "fastmcp.server"
    assert line["message"].startswith("Starting MCP server")
