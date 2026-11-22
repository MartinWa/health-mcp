from __future__ import annotations

import asyncio

import pytest
from helpers import FakeSource, both_settings, make_settings, ow_settings, strong_settings

from health_mcp import tools as tools_module
from health_mcp.server import (
    COMPOSITE_EXPOSURE,
    compose_checkin,
    compose_correlation,
    compose_overview,
    compose_refresh,
    compose_status,
    create_server,
)
from health_mcp.store import CacheStore


def run(coro):
    return asyncio.run(coro)


def registered(mcp) -> list[str]:
    return sorted(tool.name for tool in run(mcp.list_tools()))


def test_both_sources_registers_exactly_the_documented_surface(tmp_path):
    settings = both_settings(tmp_path)
    mcp = create_server(settings)
    assert registered(mcp) == tools_module.names(settings)


def test_strong_only_install_registers_no_openwearables_tools(tmp_path):
    mcp = create_server(strong_settings(tmp_path))
    names = registered(mcp)
    assert "get_lifting_summary" in names
    assert "get_weight_trend" not in names
    assert "get_overview" in names
    for name in COMPOSITE_EXPOSURE:
        assert name in names


def test_openwearables_only_install_registers_no_lifting_tools(tmp_path):
    mcp = create_server(ow_settings(tmp_path))
    names = registered(mcp)
    assert "get_weight_trend" in names
    assert "get_lifting_summary" not in names


def test_no_sources_is_a_startup_error(tmp_path):
    with pytest.raises(RuntimeError, match="No sources"):
        create_server(make_settings(tmp_path))


def test_exposure_sets_match_the_registered_surface(tmp_path):
    settings = both_settings(tmp_path)
    registered_names = set(registered(create_server(settings)))
    for agent in ("health", "health-coach"):
        exposed = set(tools_module.names(settings, agent=agent))
        assert exposed <= registered_names
    assert set(COMPOSITE_EXPOSURE) <= registered_names
    assert all(
        agents and agents <= {"health", "health-coach"} for agents in COMPOSITE_EXPOSURE.values()
    )


def test_tools_cli_prefix_and_json(capsys, tmp_path, monkeypatch):
    settings = both_settings(tmp_path)
    monkeypatch.setattr(tools_module, "Settings", lambda: settings)
    assert tools_module.main(["--names", "--agent", "health", "--prefix", "health__"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "health__get_overview" in lines
    assert all(line.startswith("health__") for line in lines)

    assert tools_module.main(["--names", "--json", "--all-sources"]) == 0
    assert '"get_lifting_summary"' in capsys.readouterr().out


def test_overview_composite_merges_sources_and_marks_missing(tmp_path):
    ow = FakeSource(
        "openwearables",
        overview={
            "weight": {"start_kg": 80.0},
            "sleep": {"avg_hours": 7.5},
            "activity": {"avg_steps": 9000},
            "recovery": {"avg_score": 70},
            "workouts": {"count": 2},
            "logging": {"days": 31, "sleep_days": 20},
        },
    )
    strong = FakeSource("strong", overview={"total_volume_kg": 1000.0, "sessions": 3})
    out = run(compose_overview({"openwearables": ow, "strong": strong}, "2026-01-01", "2026-01-31"))
    assert out["period"] == {"start": "2026-01-01", "end": "2026-01-31", "days": 31}
    assert out["weight"] == {"start_kg": 80.0}
    assert out["lifting"]["total_volume_kg"] == 1000.0
    assert out["missing"] == []

    ow_only = run(compose_overview({"openwearables": ow}, "2026-01-01", "2026-01-31"))
    assert ow_only["missing"] == ["strong"]
    assert ow_only["weight"] == {"start_kg": 80.0}


def test_overview_composite_marks_a_failed_block_in_missing(tmp_path):
    ow = FakeSource(
        "openwearables",
        overview={
            "weight": {"start_kg": 80.0},
            "activity": {"error": "Open Wearables returned 500"},
            "failed_blocks": ["activity"],
            "error": "Open Wearables returned 500",
        },
    )
    out = run(compose_overview({"openwearables": ow}, "2026-01-01", "2026-01-31"))
    assert out["missing"] == ["openwearables.activity", "strong"]
    assert out["activity"] == {"error": "Open Wearables returned 500"}
    assert out["openwearables_error"] == "Open Wearables returned 500"
    assert out["weight"] == {"start_kg": 80.0}


def test_overview_composite_carries_the_truncation_flags():
    ow = FakeSource(
        "openwearables",
        overview={
            "weight": {"start_kg": 80.0},
            "activity": {"avg_steps": 9000},
            "fetch_truncated": {"weight": False, "activity": True},
        },
    )
    out = run(compose_overview({"openwearables": ow}, "2026-01-01", "2026-01-31"))
    assert out["fetch_truncated"] == {"weight": False, "activity": True}
    assert out["activity"] == {"avg_steps": 9000}


def test_correlation_composite_reports_truncated_sleep():
    ow = FakeSource(
        "openwearables",
        sleep_hours={"2026-01-02": 7.0, "2026-01-04": 8.0, "2026-01-06": 9.0},
        sleep_truncated=True,
    )
    strong = FakeSource(
        "strong", volume={"2026-01-01": 100.0, "2026-01-03": 300.0, "2026-01-05": 500.0}
    )
    out = run(
        compose_correlation({"openwearables": ow, "strong": strong}, "2026-01-01", "2026-01-06", 1)
    )
    assert out["n"] == 3
    assert out["fetch_truncated"] == {"sleep": True}


def test_overview_composite_rejects_a_bad_range():
    out = run(compose_overview({}, "2026-02-01", "2026-01-01"))
    assert "after end_date" in out["error"]


def test_checkin_composite_adds_lifting_fields(tmp_path):
    ow = FakeSource(
        "openwearables",
        daily={
            "weight_kg": 80.1,
            "sleep_hours": 7.2,
            "workout_minutes": 45.0,
            "recovery_score": 68,
        },
    )
    strong = FakeSource(
        "strong",
        daily={
            "lifting_volume_kg": 2500.0,
            "sets": 18,
            "last_session": {"id": 1, "date": "2026-01-05"},
        },
    )
    out = run(compose_checkin({"openwearables": ow, "strong": strong}, "2026-01-05"))
    assert out["weight_kg"] == 80.1
    assert out["lifting_volume_kg"] == 2500.0
    assert out["lifting_sets"] == 18
    assert out["last_session"]["id"] == 1
    assert out["missing"] == []


def test_correlation_composite_returns_n_r_and_period():
    ow = FakeSource(
        "openwearables",
        sleep_hours={"2026-01-02": 7.0, "2026-01-04": 8.0, "2026-01-06": 9.0},
    )
    strong = FakeSource(
        "strong", volume={"2026-01-01": 100.0, "2026-01-03": 300.0, "2026-01-05": 500.0}
    )
    out = run(
        compose_correlation({"openwearables": ow, "strong": strong}, "2026-01-01", "2026-01-06", 1)
    )
    assert out["n"] == 3
    assert out["r"] == 1.0
    assert out["period"] == {"start": "2026-01-01", "end": "2026-01-06"}


def test_correlation_composite_needs_both_sources():
    out = run(compose_correlation({"strong": FakeSource("strong")}, "2026-01-01", "2026-01-31", 1))
    assert out["missing"] == ["openwearables"]
    assert "error" in out


def test_status_composite_reports_sources_cache_and_today(tmp_path):
    settings = both_settings(tmp_path, tz="Europe/Stockholm")
    cache = CacheStore(tmp_path / "cache", {"strong": 6})
    out = run(
        compose_status(
            {
                "openwearables": FakeSource("openwearables"),
                "strong": FakeSource("strong", status={"ok": False, "error": "down"}),
            },
            cache,
            settings,
            16,
        )
    )
    assert out["tool_count"] == 16
    assert out["sources"]["strong"] == {"ok": False, "error": "down"}
    assert out["cache"]["files"] == 0
    assert out["profile"]["present"] is False
    assert len(out["today"]) == 10


def test_refresh_composite_only_clears_the_named_source():
    ow = FakeSource("openwearables")
    strong = FakeSource("strong")
    out = compose_refresh({"openwearables": ow, "strong": strong}, "strong")
    assert out == {"cleared_files": 1, "source": "strong"}
    assert strong.refreshed == 1
    assert ow.refreshed == 0
    assert compose_refresh({"strong": strong}, "nope")["allowed"] == ["all", "strong"]


def test_instructions_name_only_live_sources(tmp_path):
    from health_mcp.server import instructions_for

    assert "Live sources in this deployment: strong." in instructions_for(["strong"])
    assert "Live sources in this deployment: none." in instructions_for([])


def test_in_memory_client_lists_tools_and_calls_one_per_source(tmp_path):
    import httpx
    from fastmcp import Client
    from helpers import envelope, strong_client, strong_settings

    from health_mcp.sources.strong import StrongSource

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/me":
            return httpx.Response(200, json=envelope(id=1, username="tester"))
        if path == "/api/v1/summary":
            return httpx.Response(
                200,
                json=envelope(
                    sessions=1,
                    sets=2,
                    volume_kg=500.0,
                    volume_t=0.5,
                    hours=1.0,
                    sessions_per_week=1.0,
                    avg_volume_per_session=500.0,
                    avg_session_min=60.0,
                    days_since_last_session=0,
                ),
            )
        if path == "/api/v1/series/volume":
            return httpx.Response(
                200,
                json=envelope(
                    bucket="week",
                    group_by=None,
                    count=1,
                    items=[{"bucket_start": "2026-01-05", "value": 500.0, "groups": None}],
                ),
            )
        if path.startswith("/api/v1/breakdown/"):
            return httpx.Response(
                200,
                json=envelope(
                    count=1,
                    items=[{"key": "Squat", "sessions": 1, "sets": 2, "volume_kg": 500.0}],
                ),
            )
        raise AssertionError(path)

    settings = strong_settings(tmp_path)
    cache = CacheStore(tmp_path / "cache", {"strong": 6})
    source = StrongSource(settings, cache, client=strong_client(handler))
    mcp = create_server(settings, sources={"strong": source})

    async def go():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            lifting = await client.call_tool(
                "get_lifting_summary", {"start_date": "2026-01-01", "end_date": "2026-01-31"}
            )
            status = await client.call_tool("get_status", {})
            return tools, lifting, status

    tools, lifting, status = run(go())
    assert any(tool.name == "get_lifting_summary" for tool in tools)
    assert lifting.data["total_volume_kg"] == 500.0
    assert status.data["sources"]["strong"]["ok"] is True
    assert status.data["tool_count"] == len(tools)


def test_tier_b_tools_are_exposed_to_the_health_agent_only(tmp_path):
    settings = both_settings(tmp_path)
    health = set(tools_module.names(settings, agent="health"))
    coach = set(tools_module.names(settings, agent="health-coach"))
    tier_b = {
        "get_exercise_detail",
        "get_lifting_records",
        "get_lifting_sets",
        "get_yearly_progress",
    }
    assert tier_b <= health
    assert not tier_b & coach
    assert {"get_lifting_summary", "get_overview", "get_status"} <= health & coach
