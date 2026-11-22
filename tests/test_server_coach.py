"""Tier C/D OW accessors and the coach composites, with synthetic doubles."""

from __future__ import annotations

import asyncio

import httpx
from helpers import both_settings, ow_client, page

from health_mcp import sources as registry
from health_mcp import tools as tools_module
from health_mcp.config import Settings
from health_mcp.server import (
    compose_goal_progress,
    compose_medicine_impact,
    compose_readiness,
    compose_workload,
    create_server,
)
from health_mcp.sources.openwearables import OpenWearablesSource
from health_mcp.store import CacheStore

PROFILE = """
updated: 2026-11-20
intent:
  updated: 2026-11-20
  goals:
    - {metric: bodyweight, target: 78, unit: kg, by: 2027-03-01}
    - {metric: workouts, target: 20, by: 2026-12-31}
regimen:
  updated: 2026-11-01
  medications:
    - {substance: example-med, dose: 50, unit: µg, schedule: daily}
"""

EVENTS = """date,time,kind,substance,dose,unit,reason,notes
2026-11-01,08:00,medication,example-med,50,µg,daily,
2026-11-02,08:00,medication,example-med,50,µg,daily,
2026-11-03,08:00,medication,example-med,50,µg,daily,
"""


def run(coro):
    return asyncio.run(coro)


def build(tmp_path, settings: Settings):
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    return registry.build_sources(settings, cache)


def profile_settings(tmp_path, *, events_text: str | None = None) -> Settings:
    directory = tmp_path / "profile"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.yaml").write_text(PROFILE, encoding="utf-8")
    if events_text is not None:
        (directory / "events.csv").write_text(events_text, encoding="utf-8")
    return both_settings(tmp_path, profile_dir=str(directory))


def async_return(value):
    async def accessor(*args, **kwargs):
        return value

    return accessor


def test_ow_client_endpoints_use_the_documented_paths(tmp_path):
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/data-sources"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/summaries/data"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/summaries/body"):
            return httpx.Response(200, json={"slow_changing": {}})
        if request.url.path.endswith("/events/sleep"):
            return httpx.Response(200, json=page([], has_more=False))
        raise AssertionError(request.url.path)

    client = ow_client(handler)

    async def go():
        try:
            await client.data_sources("user-1")
            await client.connections("user-1")
            await client.data_summary("user-1")
            await client.data_summary("user-1", start="2026-11-01", end="2026-11-30")
            await client.body("user-1", average_period=3, latest_window_hours=6)
            await client.sleep_sessions("user-1", "2026-11-01", "2026-11-30")
        finally:
            await client.aclose()

    run(go())
    assert [path for path, _ in seen] == [
        "/api/v1/users/user-1/data-sources",
        "/api/v1/users/user-1/connections",
        "/api/v1/users/user-1/summaries/data",
        "/api/v1/users/user-1/summaries/data",
        "/api/v1/users/user-1/summaries/body",
        "/api/v1/users/user-1/events/sleep",
    ]
    assert seen[2][1] == {}
    assert seen[3][1] == {"start_date": "2026-11-01", "end_date": "2026-11-30"}
    assert seen[4][1] == {"average_period": "3", "latest_window_hours": "6"}
    assert seen[5][1] == {
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
        "include": "stages",
    }


def make_ow_source(tmp_path, handler):
    settings = both_settings(tmp_path)
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    return OpenWearablesSource(settings, cache, client=ow_client(handler))


def test_get_body_uses_slow_changing_and_caches_for_an_hour(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "source": {"provider": "withings", "device_name": "Body+"},
                "slow_changing": {"weight_kg": 72.5, "height_cm": 175.5, "age": 36},
                "averaged": {"period_days": 7, "resting_heart_rate_bpm": 62},
                "latest": {},
            },
        )

    source = make_ow_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_body(), await source.get_body()
        finally:
            await source.close()

    first, second = run(go())
    assert first["static"]["weight_kg"] == 72.5
    assert first["averaged"]["resting_heart_rate_bpm"] == 62
    assert "as_of" in first
    assert second == first
    assert calls["n"] == 1


def test_get_data_sources_reports_a_partial_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/data-sources"):
            return httpx.Response(
                200, json={"items": [{"provider": "apple", "device_name": "Watch"}], "total": 1}
            )
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json=[{"provider": "apple", "status": "active"}])
        return httpx.Response(500, json={"title": "Internal Server Error"})

    source = make_ow_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_data_sources()
        finally:
            await source.close()

    out = run(go())
    assert out["sources"]["providers"] == {"apple": 1}
    assert out["connections"]["items"][0]["status"] == "active"
    assert "Open Wearables returned 500" in out["error"]


def test_get_sleep_sessions_summarises_and_caps(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=page(
                [
                    {
                        "start_time": "2026-11-01T23:00:00Z",
                        "end_time": "2026-11-02T07:00:00Z",
                        "sleep_duration_seconds": 25200,
                        "duration_seconds": 28800,
                        "efficiency_percent": 90,
                        "is_nap": False,
                        "stages": {"deep_minutes": 60},
                        "source": {"provider": "apple"},
                    },
                    {
                        "start_time": "2026-11-02T14:00:00Z",
                        "end_time": "2026-11-02T14:30:00Z",
                        "sleep_duration_seconds": 1800,
                        "duration_seconds": 1800,
                        "is_nap": True,
                        "source": {"provider": "apple"},
                    },
                ],
                has_more=False,
            ),
        )

    source = make_ow_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_sleep_sessions("2026-11-01", "2026-11-03")
        finally:
            await source.close()

    out = run(go())
    assert out["count"] == 2
    assert out["main_sessions"] == 1
    assert out["naps"] == 1
    assert out["period"] == {"start": "2026-11-01", "end": "2026-11-03"}
    assert out["fetch_truncated"] is False
    assert out["sessions"][-1]["is_nap"] is True


def test_get_consistency_counts_local_days(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/summaries/sleep"):
            return httpx.Response(
                200,
                json=page(
                    [
                        {"date": "2026-11-01", "duration_minutes": 420},
                        {"date": "2026-11-02", "duration_minutes": 400},
                        {"date": "2026-11-03", "duration_minutes": 430},
                    ]
                ),
            )
        if path.endswith("/timeseries"):
            return httpx.Response(
                200,
                json=page(
                    [
                        {
                            "type": "weight",
                            "timestamp": "2026-11-02T07:00:00Z",
                            "value": 80.0,
                        }
                    ]
                ),
            )
        if path.endswith("/events/workouts"):
            return httpx.Response(
                200,
                json=page(
                    [
                        {
                            "start_time": "2026-11-01T18:00:00Z",
                            "duration_seconds": 3600,
                            "type": "strength",
                        },
                        {
                            "start_time": "2026-11-03T18:00:00Z",
                            "duration_seconds": 3600,
                            "type": "strength",
                        },
                    ]
                ),
            )
        raise AssertionError(path)

    source = make_ow_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_consistency("2026-11-01", "2026-11-04")
        finally:
            await source.close()

    out = run(go())
    assert out["period"]["days"] == 4
    assert out["sleep"]["days_logged"] == 3
    assert out["sleep"]["current_streak"] == 0
    assert out["workouts"]["days_logged"] == 2
    assert out["weight"]["days_logged"] == 1
    assert out["workouts"]["pct"] == 50.0


def test_goal_progress_uses_the_profile_goal_and_labels_overrides(tmp_path):
    settings = profile_settings(tmp_path)
    sources = build(tmp_path, settings)
    sources["openwearables"].weight_points = async_return(  # type: ignore[method-assign]
        [("2026-11-01", 80.0), ("2026-11-15", 79.0)]
    )

    out = run(compose_goal_progress(sources, "2026-11-01", "2026-11-30", "weight"))
    assert out["target_kg"] == 78.0
    assert out["target_source"] == "profile"
    assert out["deadline"] == "2027-03-01"
    assert out["profile_metric"] == "bodyweight"
    assert out["projected_date"] == "2026-11-29"

    override = run(
        compose_goal_progress(sources, "2026-11-01", "2026-11-30", "weight", target=77.0)
    )
    assert override["target_kg"] == 77.0
    assert override["target_source"] == "argument_override"


def test_goal_progress_for_workouts_comes_from_the_profile_too(tmp_path):
    settings = profile_settings(tmp_path)
    sources = build(tmp_path, settings)
    sources["openwearables"].workout_count = async_return(10)  # type: ignore[method-assign]
    out = run(compose_goal_progress(sources, "2026-11-01", "2026-11-30", "workouts"))
    assert out["sessions"] == 10
    assert out["target"] == 20
    assert out["progress_pct"] == 50.0
    assert out["deadline"] == "2026-12-31"


def test_goal_progress_without_a_matching_goal_explains_itself(tmp_path):
    settings = profile_settings(tmp_path)
    (tmp_path / "profile" / "profile.yaml").write_text(
        "updated: 2026-11-20\nintent:\n  updated: 2026-11-20\n"
        "  goals: [{metric: squat_1rm, target: 140, unit: kg}]\n",
        encoding="utf-8",
    )
    sources = build(tmp_path, settings)
    out = run(compose_goal_progress(sources, "2026-11-01", "2026-11-30", "weight"))
    assert "no weight goal" in out["error"]
    assert out["available_metrics"] == ["squat_1rm"]
    assert "target=" in out["hint"]


def test_readiness_shows_inputs_and_prior_day_volume(tmp_path):
    settings = profile_settings(tmp_path)
    sources = build(tmp_path, settings)
    sources["openwearables"].recovery_days = async_return(  # type: ignore[method-assign]
        {
            "2026-11-09": {"recovery_score": 55, "resting_heart_rate_bpm": 60},
            "2026-11-10": {"recovery_score": 70, "resting_heart_rate_bpm": 58},
        }
    )
    sources["openwearables"].sleep_hours_by_date = async_return(  # type: ignore[method-assign]
        {"2026-11-09": 6.5, "2026-11-10": 8.0}
    )
    sources["strong"].volume_by_date = async_return(  # type: ignore[method-assign]
        {"2026-11-09": 5000.0, "2026-11-10": 0.0}
    )

    out = run(compose_readiness(sources, "2026-11-10", "2026-11-11"))
    assert out["period"] == {"start": "2026-11-10", "end": "2026-11-11"}
    assert out["sources"] == {"openwearables": True, "strong": True}
    assert out["days"][0]["prior_day_volume_kg"] == 5000.0
    assert out["days"][0]["recovery_score"] == 70
    assert out["days"][0]["sleep_hours"] == 8.0
    assert out["days"][1]["prior_day_volume_kg"] == 0.0
    assert out["averages"]["recovery_score"] == 70.0
    assert "No readiness score" in out["definition"]


def test_workload_trims_weeks_before_the_period(tmp_path):
    settings = profile_settings(tmp_path)
    sources = build(tmp_path, settings)
    sources["strong"].weekly_volume_series = async_return(  # type: ignore[method-assign]
        {
            "items": [
                {"bucket_start": "2026-10-05", "value": 100.0},
                {"bucket_start": "2026-10-12", "value": 100.0},
                {"bucket_start": "2026-10-19", "value": 100.0},
                {"bucket_start": "2026-10-26", "value": 100.0},
                {"bucket_start": "2026-11-02", "value": 140.0},
            ]
        }
    )
    out = run(compose_workload(sources, "2026-11-02", "2026-11-30"))
    assert [row["week_start"] for row in out["weeks"]] == ["2026-11-02"]
    assert out["current"]["flag"] == "spike"
    assert out["current"]["acwr"] == 1.4


def test_medicine_impact_compares_events_and_daily_metrics(tmp_path):
    settings = profile_settings(tmp_path, events_text=EVENTS)
    sources = build(tmp_path, settings)
    sources["openwearables"].daily_metrics = async_return(  # type: ignore[method-assign]
        {
            "recovery_score": {
                "2026-11-01": 40,
                "2026-11-02": 50,
                "2026-11-03": 60,
                "2026-11-04": 70,
                "2026-11-05": 80,
                "2026-11-06": 90,
            },
            "sleep_efficiency_pct": {},
            "resting_heart_rate_bpm": {},
        }
    )
    out = run(compose_medicine_impact(sources, "2026-11-01", "2026-11-06"))
    assert out["n_substances"] == 1
    metric = out["subjects"][0]["metrics"]["recovery_score"]
    assert metric["mean_with"] == 50.0
    assert metric["mean_without"] == 80.0
    assert out["subjects"][0]["metrics"]["sleep_efficiency_pct"]["status"] == "insufficient_days"


def test_medicine_impact_without_logged_doses_says_so(tmp_path):
    header = "date,time,kind,substance,dose,unit,reason,notes\n"
    settings = profile_settings(tmp_path, events_text=header)
    sources = build(tmp_path, settings)
    out = run(compose_medicine_impact(sources, "2026-11-01", "2026-11-06"))
    assert "no medication or supplement rows" in out["error"]


def test_tier_c_and_d_are_registered_on_the_coach_agent_only(tmp_path):
    settings = profile_settings(tmp_path, events_text=EVENTS)
    mcp = create_server(settings)
    names = {tool.name for tool in run(mcp.list_tools())}
    coach = {
        "get_body",
        "get_data_sources",
        "get_sleep_sessions",
        "get_consistency",
        "get_medicine_logs",
        "get_goal_progress",
        "get_readiness",
        "get_workload",
        "get_medicine_impact",
    }
    assert coach <= names
    assert coach <= set(tools_module.all_exposure())
    coach_names = set(tools_module.names(settings, agent="health-coach"))
    health_names = set(tools_module.names(settings, agent="health"))
    assert coach <= coach_names
    assert not coach & health_names


def test_registered_surface_equals_the_generator_for_all_four_sources(tmp_path):
    settings = profile_settings(tmp_path, events_text=EVENTS)
    mcp = create_server(settings)
    names = {tool.name for tool in run(mcp.list_tools())}
    assert names == set(tools_module.names(settings))


def test_medicine_logs_is_callable_through_the_mcp_server(tmp_path):
    from fastmcp import Client

    settings = profile_settings(tmp_path, events_text=EVENTS)
    mcp = create_server(settings)

    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "get_medicine_logs", {"start_date": "2026-11-01", "end_date": "2026-11-06"}
            )
            return result.data

    out = run(go())
    assert out["period"] == {"start": "2026-11-01", "end": "2026-11-06"}
    assert out["doses"][0]["substance"] == "example-med"
    assert out["doses"][0]["count"] == 3
