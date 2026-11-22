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


def test_every_line_is_json_even_with_uvicorn_loggers(capsys):
    import json

    configure_logging("INFO")
    logging.getLogger("uvicorn.error").warning("Uvicorn running on http://0.0.0.0:8000")
    logging.getLogger("uvicorn.access").info('127.0.0.1:1 - "GET /healthz HTTP/1.1" 200 OK')

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert [line["message"] for line in lines] == ["Uvicorn running on http://0.0.0.0:8000"]
    assert lines[0]["logger"] == "uvicorn.error"
    assert '"GET /healthz' not in captured.out
