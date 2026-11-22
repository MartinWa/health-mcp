"""Opt-in contract and parity checks against the live strong-data API.

Skipped unless STRONG_API_URL and STRONG_API_TOKEN are present (env or .env).
Run these before a deploy; they are not part of the offline suite.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from health_mcp.config import Settings
from health_mcp.strong_client import CONTRACT_MAJOR_MINOR, StrongDataClient, parse_major_minor

pytestmark = pytest.mark.live


def live_settings() -> Settings:
    settings = Settings()
    if not (settings.strong_api_url and settings.strong_api_token):
        pytest.skip("needs STRONG_API_URL and STRONG_API_TOKEN")
    return settings


def make_client(settings: Settings) -> StrongDataClient:
    return StrongDataClient(
        settings.strong_api_url or "",
        settings.strong_api_token.get_secret_value() if settings.strong_api_token else "",
        timeout=15.0,
        deadline=30.0,
    )


def run(coro):
    return asyncio.run(coro)


def test_live_identity_and_contract_version():
    settings = live_settings()
    client = make_client(settings)

    async def go():
        try:
            return await client.me(), await client.healthz()
        finally:
            await client.aclose()

    me, health = run(go())
    assert me["contract_version"]
    assert parse_major_minor(me["contract_version"]) == CONTRACT_MAJOR_MINOR
    assert health["contract_version"]
    if settings.strong_owner_id:
        assert str(me["id"]) == settings.strong_owner_id.strip()


def test_live_summary_agrees_with_sets_over_the_same_range():
    settings = live_settings()
    client = make_client(settings)
    end = date.today()
    start = end - timedelta(days=30)

    async def go():
        try:
            summary = await client.summary(start=start.isoformat(), end=end.isoformat())
            volume = 0.0
            counted = 0
            sessions: set[int] = set()
            offset = 0
            while True:
                page = await client.sets(
                    start=start.isoformat(), end=end.isoformat(), limit=1000, offset=offset
                )
                items = page.get("items") or []
                volume += sum(float(item.get("volume_kg") or 0) for item in items)
                counted += len(items)
                sessions.update(
                    int(item["session_id"]) for item in items if item.get("session_id") is not None
                )
                if not page.get("has_more"):
                    break
                offset += len(items)
                if offset >= 20000:
                    pytest.skip("range has more than 20000 sets; narrow the window")
            return summary, volume, counted, sessions
        finally:
            await client.aclose()

    summary, volume, counted, sessions = run(go())
    assert counted == summary["sets"]
    assert len(sessions) == summary["sessions"]
    assert abs(volume - float(summary["volume_kg"])) <= 0.5
