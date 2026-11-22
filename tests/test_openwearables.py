from __future__ import annotations

import asyncio

import httpx
from helpers import ow_client, ow_settings, page

from health_mcp.sources.openwearables import OpenWearablesSource
from health_mcp.store import CacheStore


def run(coro):
    return asyncio.run(coro)


def make_source(tmp_path, handler, suffix="main"):
    settings = ow_settings(tmp_path)
    cache = CacheStore(tmp_path / f"cache-{suffix}", {"openwearables": 24})
    client = ow_client(handler)
    return OpenWearablesSource(settings, cache, client=client), client


def health_score_row(recorded_at: str, value: float, category: str = "sleep") -> dict:
    return {"recorded_at": recorded_at, "category": category, "value": value}


def test_health_scores_include_a_record_after_midnight_on_the_end_date(tmp_path):
    rows = [
        health_score_row("2026-01-30T12:00:00Z", 60),
        health_score_row("2026-01-31T14:00:00Z", 80),
    ]
    source, client = make_source(tmp_path, lambda request: httpx.Response(200, json=page(rows)))

    async def go():
        try:
            return await source.get_health_scores("2026-01-01", "2026-01-31")
        finally:
            await client.aclose()

    out = run(go())
    assert out["points"] == 2
    assert out["by_category"]["sleep"]["count"] == 2
    assert out["by_category"]["sleep"]["latest"] == 80.0


def test_a_record_at_0030_local_is_reported_on_that_local_date(tmp_path):
    rows = [health_score_row("2026-01-30T23:30:00Z", 70)]
    source, client = make_source(tmp_path, lambda request: httpx.Response(200, json=page(rows)))

    async def go():
        try:
            on_31 = await source.get_health_scores("2026-01-31", "2026-01-31")
            on_30 = await source.get_health_scores("2026-01-30", "2026-01-30")
        finally:
            await client.aclose()
        return on_31, on_30

    on_31, on_30 = run(go())
    assert on_31["points"] == 1
    assert "error" in on_30


def test_workouts_follow_pagination_and_report_totals(tmp_path):
    first = {
        "start_time": "2026-01-05T18:00:00Z",
        "type": "run",
        "duration_seconds": 1800,
    }
    second = {
        "start_time": "2026-01-06T18:00:00Z",
        "type": "ride",
        "duration_seconds": 3600,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return httpx.Response(200, json=page([second]))
        return httpx.Response(200, json=page([first], has_more=True, cursor="c1"))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_workouts("2026-01-01", "2026-01-31")
        finally:
            await client.aclose()

    out = run(go())
    assert out["count"] == 2
    assert out["by_type"] == {"run": 1, "ride": 1}
    assert out["total_hours"] == 1.5
    assert out["returned"] == 2


def test_workouts_can_be_grouped_by_week(tmp_path):
    rows = [
        {"start_time": "2026-01-05T18:00:00Z", "type": "run", "duration_seconds": 600},
        {"start_time": "2026-01-06T18:00:00Z", "type": "run", "duration_seconds": 1200},
        {"start_time": "2026-01-13T18:00:00Z", "type": "run", "duration_seconds": 600},
    ]
    source, client = make_source(tmp_path, lambda request: httpx.Response(200, json=page(rows)))

    async def go():
        try:
            return await source.get_workouts("2026-01-01", "2026-01-31", group_by="week")
        finally:
            await client.aclose()

    out = run(go())
    assert [week["week_start"] for week in out["weeks"]] == ["2026-01-05", "2026-01-12"]
    assert out["weeks"][0]["count"] == 2
    assert out["weeks"][0]["minutes"] == 30.0


def test_weight_is_derived_from_local_dates_in_the_timeseries(tmp_path):
    rows = [
        {"type": "weight", "timestamp": "2026-01-01T06:00:00Z", "value": 80.0},
        {"type": "weight", "timestamp": "2026-01-05T06:00:00Z", "value": 79.0},
        {"type": "heart_rate", "timestamp": "2026-01-05T06:00:00Z", "value": 60},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params)["types"] == "weight"
        return httpx.Response(200, json=page(rows))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_weight_trend(
                "2026-01-01", "2026-01-31", include_series=True, max_points=10
            )
        finally:
            await client.aclose()

    out = run(go())
    assert out["start_kg"] == 80.0
    assert out["end_kg"] == 79.0
    assert out["change_kg"] == -1.0
    assert out["dates"] == ["2026-01-01", "2026-01-05"]
    assert out["series_included"] is True


def test_timeseries_fetch_itself_is_capped(tmp_path):
    rows = [{"type": "steps", "timestamp": "2026-01-01T06:00:00Z", "value": 1}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page(rows, has_more=True, cursor="same"))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_timeseries(["steps"], "2026-01-01", "2026-01-02", "1day")
        finally:
            await client.aclose()

    out = run(go())
    assert out["fetch_truncated"] is True
    assert out["pages"] == 20
    assert out["total_points"] == 20


def test_sleep_summary_attaches_a_capped_series_only_when_asked(tmp_path):
    rows = [
        {"date": "2026-01-01", "duration_minutes": 420, "efficiency_percent": 90},
        {"date": "2026-01-02", "duration_minutes": 480, "efficiency_percent": 88},
    ]
    source, client = make_source(tmp_path, lambda request: httpx.Response(200, json=page(rows)))

    async def go():
        try:
            stats_only = await source.get_sleep_summary("2026-01-01", "2026-01-02")
            with_series = await source.get_sleep_summary(
                "2026-01-01", "2026-01-02", include_series=True, max_points=10
            )
        finally:
            await client.aclose()
        return stats_only, with_series

    stats_only, with_series = run(go())
    assert "dates" not in stats_only
    assert stats_only["series_included"] is False
    assert with_series["dates"] == ["2026-01-01", "2026-01-02"]
    assert with_series["values"] == [7.0, 8.0]


def test_source_errors_become_an_error_dict(tmp_path):
    source, client = make_source(
        tmp_path, lambda request: httpx.Response(500, json={"detail": "boom"})
    )

    async def go():
        try:
            return await source.get_activity_summary("2026-01-01", "2026-01-02")
        finally:
            await client.aclose()

    out = run(go())
    assert out["source"] == "openwearables"
    assert "cache_age_hours" in out
    assert "error" in out


def test_status_reports_user_and_latency(tmp_path):
    source, client = make_source(
        tmp_path,
        lambda request: httpx.Response(200, json={"id": "user-1", "username": "tester"}),
    )

    async def go():
        try:
            return await source.status()
        finally:
            await client.aclose()

    out = run(go())
    assert out["ok"] is True
    assert out["user"] == {"id": "user-1", "username": "tester"}
    assert isinstance(out["latency_ms"], int)


def test_person_merges_personal_record_and_static_body_data(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/personal-record"):
            return httpx.Response(
                200, json={"birth_date": "1990-01-01", "sex": None, "gender": "x"}
            )
        if request.url.path.endswith("/summaries/body"):
            return httpx.Response(
                200,
                json={
                    "static": {
                        "height_cm": 180.0,
                        "body_fat_percentage": 18.5,
                        "muscle_mass_kg": 35.0,
                        "bmi": 24.1,
                        "age": 36,
                    }
                },
            )
        raise AssertionError(request.url.path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.person()
        finally:
            await client.aclose()

    out = run(go())
    assert out["available"] is True
    assert out["birth_date"] == "1990-01-01"
    assert out["height_cm"] == 180.0
    assert out["body_fat_pct"] == 18.5
    assert out["muscle_mass_kg"] == 35.0
    assert out["age"] == 36
    assert out["missing"] == ["sex"]
    assert "Set sex in Open Wearables" in out["note"]


def test_person_reports_an_unavailable_source_without_raising(tmp_path):
    source, client = make_source(
        tmp_path,
        lambda request: httpx.Response(500, json={"detail": "down"}),
    )

    async def go():
        try:
            return await source.person()
        finally:
            await client.aclose()

    out = run(go())
    assert out["available"] is False
    assert "Open Wearables returned 500" in out["error"]
    assert "cache_age_hours" in out


def test_body_static_reads_the_current_slow_changing_block(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"slow_changing": {"height_cm": 175.5, "body_fat_percent": 18.5}}
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source._client.body_static("user-1")
        finally:
            await client.aclose()

    assert run(go()) == {"height_cm": 175.5, "body_fat_percent": 18.5}
