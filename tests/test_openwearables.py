from __future__ import annotations

import asyncio

import httpx
import pandas as pd
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
                        "body_fat_percent": 18.5,
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


def test_timeseries_sends_repeated_type_parameters(tmp_path):
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["types"] = request.url.params.get_list("types")
        return httpx.Response(200, json=page([]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_timeseries(
                ["heart_rate", "steps"], "2026-11-01", "2026-11-02", "1day"
            )
        finally:
            await client.aclose()

    run(go())
    assert seen["types"] == ["heart_rate", "steps"]


def test_consistency_propagates_the_page_ceiling(tmp_path):
    source, client = make_source(tmp_path, lambda request: httpx.Response(200, json=page([])))

    async def weights(start, end):
        return pd.DataFrame({"date": ["2026-11-01"], "weight_kg": [80.0]}), True

    async def sleep(start, end):
        return pd.DataFrame({"date": ["2026-11-01"], "duration_minutes": [420.0]}), False

    async def workouts(start, end):
        return pd.DataFrame({"start_time": ["2026-11-01T18:00:00Z"]}), False

    source._weight_df = weights  # type: ignore[method-assign]
    source._sleep_df = sleep  # type: ignore[method-assign]
    source._workouts_df = workouts  # type: ignore[method-assign]

    async def go():
        try:
            return await source.get_consistency("2026-11-01", "2026-11-03")
        finally:
            await client.aclose()

    out = run(go())
    assert out["fetch_truncated"] == {"weight": True, "sleep": False, "workouts": False}
    assert out["complete"] is False
    assert "page ceiling" in out["warning"]


def test_summary_paging_falls_back_to_date_windows(tmp_path):
    from datetime import date, timedelta

    calls: list[dict] = []

    def day_rows(first: str, last: str) -> list[dict]:
        start, end = date.fromisoformat(first), date.fromisoformat(last)
        return [
            {"date": (start + timedelta(days=offset)).isoformat(), "steps": 1000}
            for offset in range((end - start).days + 1)
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if "cursor" in params:
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        rows = day_rows(params["start_date"], params["end_date"])
        if len(calls) == 1:
            return httpx.Response(200, json=page(rows, has_more=True, cursor="c1"))
        return httpx.Response(200, json=page(rows))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_activity_summary("2026-01-01", "2026-01-10")
        finally:
            await client.aclose()

    out = run(go())
    assert "error" not in out
    assert out["points"] == 10
    assert out["fetch_truncated"] is False
    assert sum("cursor" in params for params in calls) == 1
    assert len(calls) == 4


def test_paging_failure_on_the_first_page_is_an_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "Internal Server Error"})

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_activity_summary("2026-01-01", "2026-01-10")
        finally:
            await client.aclose()

    out = run(go())
    assert out["error"] == "Open Wearables returned 500"


def test_a_single_day_window_keeps_the_first_page_when_paging_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        return httpx.Response(
            200, json=page([{"date": "2026-01-01", "steps": 7000}], has_more=True, cursor="c1")
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_activity_summary("2026-01-01", "2026-01-01")
        finally:
            await client.aclose()

    out = run(go())
    assert out["points"] == 1
    assert out["fetch_truncated"] is True


def test_overview_reports_the_error_of_the_block_that_failed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("summaries/activity"):
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        return httpx.Response(200, json=page([]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.overview("2026-01-01", "2026-01-10")
        finally:
            await client.aclose()

    out = run(go())
    assert out["activity"]["error"] == "Open Wearables returned 500"
    assert out["failed_blocks"] == ["activity"]
    assert out["error"] == "Open Wearables returned 500"
    assert "No data for this period" in out["weight"]["error"]


def activity_rows(first_day: int, count: int) -> list[dict]:
    return [
        {"date": f"2026-01-{day:02d}", "steps": 1000 * day}
        for day in range(first_day, first_day + count)
    ]


def sleep_rows(first_day: int, count: int) -> list[dict]:
    return [
        {"date": f"2026-01-{day:02d}", "duration_minutes": 420, "efficiency_percent": 90}
        for day in range(first_day, first_day + count)
    ]


def test_a_cursor_page_still_retries_a_rate_limit(tmp_path):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return httpx.Response(200, json=page(sleep_rows(1, 1), has_more=True, cursor="c1"))
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(200, json=page(sleep_rows(2, 1)))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_sleep_summary("2026-01-01", "2026-01-02")
        finally:
            await client.aclose()

    out = run(go())
    assert state["calls"] == 3
    assert "error" not in out
    assert out["fetch_truncated"] is False
    assert out["points"] == 2


def test_an_endpoint_without_a_fallback_keeps_its_5xx_retries(tmp_path):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            rows = [{"type": "steps", "timestamp": "2026-01-01T06:00:00Z", "value": 1}]
            return httpx.Response(200, json=page(rows, has_more=True, cursor="c1"))
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(500, json={"detail": "boom"})
        rows = [{"type": "steps", "timestamp": "2026-01-02T06:00:00Z", "value": 2}]
        return httpx.Response(200, json=page(rows))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_timeseries(["steps"], "2026-01-01", "2026-01-02", "1day")
        finally:
            await client.aclose()

    out = run(go())
    assert state["calls"] == 2
    assert "error" not in out
    assert out["total_points"] == 2
    assert out["fetch_truncated"] is False


def test_a_workout_crossing_a_split_boundary_is_kept_once(tmp_path):
    crossing = {
        "start_time": "2026-01-05T23:00:00Z",
        "end_time": "2026-01-06T01:00:00Z",
        "type": "ride",
        "duration_seconds": 7200,
    }
    calls = {"windows": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "cursor" in params:
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        calls["windows"] += 1
        if calls["windows"] == 1:
            return httpx.Response(200, json=page([], has_more=True, cursor="c1"))
        start, end = params["start_date"], params["end_date"]
        kept = [
            row
            for row in [crossing]
            if start <= row["start_time"][:10] and row["end_time"][:10] <= end
        ]
        return httpx.Response(200, json=page(kept))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_workouts("2026-01-01", "2026-01-10")
        finally:
            await client.aclose()

    out = run(go())
    assert "error" not in out
    assert out["count"] == 1
    assert out["fetch_truncated"] is False


def test_a_failed_half_keeps_the_rows_the_other_half_recovered(tmp_path):
    state = {"first": True}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "cursor" in params:
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        if state["first"]:
            state["first"] = False
            return httpx.Response(200, json=page(activity_rows(1, 1), has_more=True, cursor="c1"))
        if params["start_date"] == "2026-01-06":
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        return httpx.Response(200, json=page(activity_rows(2, 4)))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_activity_summary("2026-01-01", "2026-01-10")
        finally:
            await client.aclose()

    out = run(go())
    assert out["fetch_truncated"] is True
    assert out["points"] == 5


def test_overview_and_daily_report_a_truncated_block(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("summaries/activity"):
            if "cursor" in request.url.params:
                return httpx.Response(500, json={"detail": "Internal Server Error"})
            return httpx.Response(200, json=page(activity_rows(1, 1), has_more=True, cursor="c1"))
        if path.endswith("summaries/sleep"):
            return httpx.Response(200, json=page(sleep_rows(1, 1)))
        return httpx.Response(200, json=page([]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            overview = await source.overview("2026-01-01", "2026-01-01")
            daily = await source.daily("2026-01-01")
        finally:
            await client.aclose()
        return overview, daily

    overview, daily = run(go())
    assert overview["fetch_truncated"]["activity"] is True
    assert overview["fetch_truncated"]["sleep"] is False
    assert daily["fetch_truncated"]["sleep"] is False
    assert daily["fetch_truncated"]["weight"] is False


def test_timeseries_maps_friendly_names_to_upstream_series_types(tmp_path):
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["types"] = request.url.params.get_list("types")
        return httpx.Response(200, json=page([]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_timeseries(
                ["hrv", "spo2", "calories", "temperature"], "2026-11-01", "2026-11-02", "1day"
            )
        finally:
            await client.aclose()

    out = run(go())
    assert seen["types"] == [
        "heart_rate_variability_rmssd",
        "oxygen_saturation",
        "active_energy",
        "body_temperature",
    ]
    assert out["types_requested"] == ["hrv", "spo2", "calories", "temperature"]
    assert out["types_resolved"] == seen["types"]


def test_a_local_validation_error_is_not_tagged_as_an_upstream_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a locally rejected range must not reach upstream")

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_weight_trend("2026-02-01", "2026-01-01")
        finally:
            await client.aclose()

    out = run(go())
    assert "is after end_date" in out["error"]
    assert out["source"] == "local"
    assert "cache_age_hours" not in out
    assert "no upstream call" in out["hint"]


def test_workout_count_filters_to_the_declared_types(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=page(
                [
                    {"start_time": "2026-11-01T10:00:00Z", "type": "walking"},
                    {"start_time": "2026-11-02T18:00:00Z", "type": "strength_training"},
                    {"start_time": "2026-11-03T18:00:00Z", "type": "Strength_Training"},
                ]
            ),
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            all_types = await source.workout_count("2026-11-01", "2026-11-03")
            lifting = await source.workout_count("2026-11-01", "2026-11-03", ["strength_training"])
        finally:
            await client.aclose()
        return all_types, lifting

    (everything, truncated), (filtered, filtered_truncated) = run(go())
    assert everything == 3
    assert filtered == 2
    assert truncated is False and filtered_truncated is False


def test_overview_marks_a_capped_workout_page(tmp_path):
    rows = [
        {"start_time": f"2026-11-{day:02d}T{hour:02d}:00:00Z", "type": "strength_training"}
        for day in range(1, 4)
        for hour in range(0, 24)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "workouts" in request.url.path:
            return httpx.Response(200, json=page(rows))
        return httpx.Response(200, json=page([]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.overview("2026-11-01", "2026-11-30")
        finally:
            await client.aclose()

    out = run(go())
    assert out["workouts"]["has_more"] is True
    assert out["workouts"]["returned"] == 50
    assert out["capped"] == ["workouts"]
    assert "capped page" in out["capped_note"]


def test_source_status_flags_an_empty_cache(tmp_path):
    source, client = make_source(
        tmp_path,
        lambda request: httpx.Response(
            200, json={"id": "user-1", "username": "synthetic", "contract_version": "1.0.0"}
        ),
    )

    async def go():
        try:
            return await source.status()
        finally:
            await client.aclose()

    out = run(go())
    assert out["cache_age_hours"] is None
    assert out["cache_empty"] is True
