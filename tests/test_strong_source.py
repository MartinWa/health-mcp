from __future__ import annotations

import asyncio

import httpx
from helpers import envelope, strong_client, strong_settings

from health_mcp.sources.strong import StrongSource
from health_mcp.store import CacheStore


def run(coro):
    return asyncio.run(coro)


def summary_payload(**overrides):
    values = {
        "sessions": 2,
        "sets": 10,
        "volume_kg": 1000.0,
        "volume_t": 1.0,
        "hours": 1.5,
        "sessions_per_week": 1.0,
        "avg_volume_per_session": 500.0,
        "avg_session_min": 45.0,
        "days_since_last_session": 1,
    }
    values.update(overrides)
    return envelope(**values)


def series_payload(items):
    return envelope(bucket="week", group_by=None, count=len(items), items=items)


def breakdown_payload(items):
    return envelope(count=len(items), items=items)


def session_item(number: int) -> dict:
    return {
        "id": number,
        "session_date": "2026-01-05",
        "program": "Upper",
        "workout_name": f"Upper #{number}",
        "start_time": "2026-01-05T18:00:00",
        "duration_min": 60,
        "set_count": 1,
        "total_volume_kg": 100.0,
    }


def make_source(tmp_path, handler, **overrides):
    settings = strong_settings(tmp_path, **overrides)
    cache = CacheStore(tmp_path / "cache", {"strong": 6})
    client = strong_client(handler)
    return StrongSource(settings, cache, client=client), client


def test_lifting_summary_shapes_and_caches(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        path = request.url.path
        if path.endswith("/summary"):
            return httpx.Response(200, json=summary_payload())
        if path.endswith("/series/volume"):
            return httpx.Response(
                200,
                json=series_payload(
                    [
                        {"bucket_start": "2025-12-29", "value": 600.0, "groups": None},
                        {"bucket_start": "2026-01-05", "value": 400.0, "groups": None},
                    ]
                ),
            )
        if path.endswith("/breakdown/exercises"):
            return httpx.Response(
                200,
                json=breakdown_payload(
                    [{"key": "Squat", "sessions": 2, "sets": 6, "volume_kg": 800.0}]
                ),
            )
        if path.endswith("/breakdown/muscle"):
            return httpx.Response(
                200,
                json=breakdown_payload(
                    [{"key": "legs", "sessions": 2, "sets": 6, "volume_kg": 800.0}]
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            first = await source.get_lifting_summary("2026-01-01", "2026-01-31", top_n=5)
            second = await source.get_lifting_summary("2026-01-01", "2026-01-31", top_n=5)
        finally:
            await client.aclose()
        return first, second

    first, second = run(go())
    assert first["total_volume_kg"] == 1000.0
    assert first["sessions"] == 2
    assert first["top_exercises"] == [
        {"exercise": "Squat", "sessions": 2, "sets": 6, "volume_kg": 800.0}
    ]
    assert first["muscle_balance"] == [{"muscle": "legs", "volume_kg": 800.0}]
    assert first["weekly_volume"][-1] == {
        "week_start": "2026-01-05",
        "volume_kg": 400.0,
        "moving_avg_4w": None,
        "partial": False,
    }
    assert first["period"] == {"start": "2026-01-01", "end": "2026-01-31"}
    assert second == first
    assert calls["n"] == 4


def test_volume_by_date_returns_a_mapping(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_payload(
                [
                    {"bucket_start": "2026-01-01", "value": 100.0, "groups": None},
                    {"bucket_start": "2026-01-02", "value": 0, "groups": None},
                ]
            ),
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.volume_by_date("2026-01-01", "2026-01-02")
        finally:
            await client.aclose()

    assert run(go()) == {"2026-01-01": 100.0, "2026-01-02": 0.0}


def test_lifting_sessions_expand_sets_up_to_the_cap(tmp_path):
    detail_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json=envelope(
                    items=[session_item(index) for index in range(12)],
                    count=12,
                    next=None,
                    previous=None,
                    has_more=False,
                ),
            )
        if path.startswith("/api/v1/sessions/"):
            detail_calls["n"] += 1
            session_id = int(path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json=envelope(
                    **session_item(session_id),
                    sets=[
                        {
                            "exercise": "Squat",
                            "muscle_group": "legs",
                            "set_order": 1,
                            "weight_kg": 100,
                            "reps": 5,
                            "rpe": None,
                            "volume_kg": 500,
                            "est_1rm_kg": 116.7,
                        }
                    ],
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_lifting_sessions(
                "2026-01-01", "2026-01-31", limit=12, include_sets=True
            )
        finally:
            await client.aclose()

    out = run(go())
    assert len(out["sessions"]) == 12
    assert out["sets_omitted"] == 2
    assert detail_calls["n"] == 10
    assert out["sessions"][0]["sets"][0]["exercise"] == "Squat"
    assert "sets_hint" in out


def test_lifting_sessions_without_sets_reports_offsets(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope(
                items=[session_item(1)],
                count=5,
                next="next",
                previous=None,
                has_more=True,
            ),
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_lifting_sessions("2026-01-01", "2026-01-31", limit=1, offset=2)
        finally:
            await client.aclose()

    out = run(go())
    assert out["count"] == 5
    assert out["has_more"] is True
    assert out["offset"] == 2
    assert out["next_offset"] == 3
    assert "sets" not in out["sessions"][0]


def test_daily_includes_last_session(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/summary":
            return httpx.Response(200, json=summary_payload(sessions=1))
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json=envelope(
                    items=[session_item(3)],
                    count=1,
                    next=None,
                    previous=None,
                    has_more=False,
                ),
            )
        raise AssertionError(request.url.path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.daily("2026-01-05")
        finally:
            await client.aclose()

    out = run(go())
    assert out["lifting_volume_kg"] == 1000.0
    assert out["sessions"] == 1
    assert out["sets"] == 10
    assert out["last_session"]["id"] == 3


def test_status_reports_owner_match_and_mismatch(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(id=7, username="tester"))

    matching, client = make_source(tmp_path, handler, strong_owner_id="7")

    async def go(source):
        try:
            return await source.status()
        finally:
            await source._client.aclose()

    out = run(go(matching))
    assert out["ok"] is True
    assert out["owner_id"] == "7"
    assert out["owner_match"] is True

    mismatching, client = make_source(tmp_path / "other", handler, strong_owner_id="8")
    out = run(go(mismatching))
    assert out["owner_match"] is False


def test_status_does_not_raise_when_the_token_is_rejected(tmp_path):
    source, client = make_source(
        tmp_path, lambda request: httpx.Response(401, json={"error": {"code": "x", "message": "m"}})
    )

    async def go():
        try:
            return await source.status()
        finally:
            await client.aclose()

    out = run(go())
    assert out["ok"] is False
    assert "rejected" in out["error"]


def test_tool_errors_are_returned_with_source_and_cache_age(tmp_path):
    source, client = make_source(
        tmp_path, lambda request: httpx.Response(401, json={"error": {"code": "x", "message": "m"}})
    )

    async def go():
        try:
            return await source.get_lifting_summary("2026-01-01", "2026-01-31")
        finally:
            await client.aclose()

    out = run(go())
    assert out["source"] == "strong"
    assert "cache_age_hours" in out
    assert "error" in out


def test_trend_and_breakdown_shapes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/series/volume":
            return httpx.Response(
                200,
                json=series_payload(
                    [
                        {"bucket_start": "2026-01-05", "value": 100.0, "value_4wma": 90.0},
                        {"bucket_start": "2026-01-12", "value": 200.0, "value_4wma": 150.0},
                    ]
                ),
            )
        if path == "/api/v1/breakdown/muscle":
            return httpx.Response(
                200,
                json=breakdown_payload(
                    [
                        {"key": "legs", "sessions": 1, "sets": 2, "volume_kg": 300.0},
                        {"key": "chest", "sessions": 1, "sets": 2, "volume_kg": 100.0},
                    ]
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            trend = await source.get_lifting_trend(
                "2026-01-01", "2026-01-31", metric="volume", bucket="week", max_points=50
            )
            breakdown = await source.get_lifting_breakdown(
                "2026-01-01", "2026-01-31", dimension="muscle"
            )
            return trend, breakdown
        finally:
            await client.aclose()

    trend, breakdown = run(go())
    assert trend["metric"] == "volume"
    assert trend["points"][1]["moving_avg_4w"] == 150.0
    assert trend["truncated"] is False
    assert breakdown["items"][0]["share_pct"] == 75.0
    assert breakdown["items"][1]["share_pct"] == 25.0
