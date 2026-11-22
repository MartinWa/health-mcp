"""Shared synthetic fixtures. No real health data belongs in this repo."""

from __future__ import annotations

from typing import Any

import httpx

from health_mcp.client import OpenWearablesClient
from health_mcp.config import Settings
from health_mcp.strong_client import StrongDataClient


def make_settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "cache_dir": str(tmp_path / "cache"),
        "profile_dir": str(tmp_path / "profile"),
        "cache_ttl_hours": 24,
        "strong_cache_ttl_hours": 6,
        "tz": "Europe/Stockholm",
        "default_max_points": 200,
        "default_page_limit": 50,
    }
    base.update(overrides)
    return Settings(**base)


def strong_settings(tmp_path: Any, **overrides: Any) -> Settings:
    return make_settings(
        tmp_path,
        strong_api_url="https://strong.test",
        strong_api_token="strong-token",
        **overrides,
    )


def ow_settings(tmp_path: Any, **overrides: Any) -> Settings:
    return make_settings(
        tmp_path,
        ow_api_key="ow-key",
        ow_user_id="user-1",
        ow_base_url="https://ow.test",
        **overrides,
    )


def both_settings(tmp_path: Any, **overrides: Any) -> Settings:
    return make_settings(
        tmp_path,
        ow_api_key="ow-key",
        ow_user_id="user-1",
        ow_base_url="https://ow.test",
        strong_api_url="https://strong.test",
        strong_api_token="strong-token",
        **overrides,
    )


def envelope(**values: Any) -> dict[str, Any]:
    return {
        "contract_version": "1.0.0",
        "generated_at": "2026-01-01T00:00:00Z",
        **values,
    }


def page(rows: list[dict[str, Any]], *, has_more: bool = False, cursor: str | None = None):
    pagination: dict[str, Any] = {"has_more": has_more}
    if cursor:
        pagination["next_cursor"] = cursor
    return {"data": rows, "pagination": pagination}


def strong_client(handler: Any, **kwargs: Any) -> StrongDataClient:
    defaults: dict[str, Any] = {
        "transport": httpx.MockTransport(handler),
        "backoff": 0,
    }
    defaults.update(kwargs)
    return StrongDataClient("https://strong.test", "strong-token", **defaults)


def ow_client(handler: Any, **kwargs: Any) -> OpenWearablesClient:
    defaults: dict[str, Any] = {
        "transport": httpx.MockTransport(handler),
        "backoff": 0,
    }
    defaults.update(kwargs)
    return OpenWearablesClient("https://ow.test", "ow-key", **defaults)


class FakeSource:
    """Minimal source double for composites, registry and ASGI tests."""

    cache_namespace = "fake"

    def __init__(
        self,
        name: str,
        *,
        overview: dict[str, Any] | None = None,
        daily: dict[str, Any] | None = None,
        sleep_hours: dict[str, float] | None = None,
        volume: dict[str, float] | None = None,
        status: dict[str, Any] | None = None,
    ):
        self.NAME = name
        self.cache_namespace = name
        self._overview = overview or {}
        self._daily = daily or {}
        self._sleep_hours = sleep_hours or {}
        self._volume = volume or {}
        self._status = status or {"ok": True, "source": name}
        self.refreshed = 0
        self.closed = False

    def tools(self) -> list[Any]:
        return []

    def refresh(self) -> int:
        self.refreshed += 1
        return 1

    async def close(self) -> None:
        self.closed = True

    async def status(self) -> dict[str, Any]:
        return self._status

    async def overview(self, start: str, end: str) -> dict[str, Any]:
        return self._overview

    async def daily(self, day: str) -> dict[str, Any]:
        return self._daily

    async def sleep_hours_by_date(self, start: str, end: str) -> dict[str, float]:
        return self._sleep_hours

    async def volume_by_date(self, start: str, end: str) -> dict[str, float]:
        return self._volume
