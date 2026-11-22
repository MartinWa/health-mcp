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
