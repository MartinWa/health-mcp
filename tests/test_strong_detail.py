"""Tier B lifting tools: exercise detail, records, raw sets and yearly progress."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from helpers import envelope, strong_client, strong_settings

from health_mcp.sources.strong import StrongSource
from health_mcp.store import SAFE_NAME, CacheStore


def run(coro):
    return asyncio.run(coro)


def make_source(tmp_path, handler, **overrides):
    settings = strong_settings(tmp_path, **overrides)
    cache = CacheStore(tmp_path / "cache", {"strong": 6})
    client = strong_client(handler)
    return StrongSource(settings, cache, client=client), client


def progress_item(day: str, est: float | None, top: float | None, **overrides: Any) -> dict:
    values = {
        "date": day,
        "session_id": 1,
        "sets": 4,
        "best_est_1rm_kg": est,
        "top_weight_kg": top,
        "max_reps": 8,
        "volume_kg": 1000.0,
    }
    values.update(overrides)
    return values


def exercise_record(reps: int, top_weight: float, est: float) -> dict:
    return {
        "reps": reps,
        "set_count": 3,
        "top_weight_kg": top_weight,
        "best_est_1rm_kg": est,
        "volume_kg": 900.0,
        "set_time": "2026-01-05T18:10:00",
    }


def set_item(number: int, **overrides: Any) -> dict:
    values = {
        "id": number,
        "session_id": number,
        "session_date": "2026-01-05",
        "exercise": "Back Squat",
        "muscle_group": "legs",
        "set_order": number,
        "weight_kg": 100.0,
        "reps": 5,
        "rpe": 8.0,
        "distance": None,
        "seconds": None,
        "set_time": "2026-01-05T18:00:00",
        "volume_kg": 500.0,
        "est_1rm_kg": 116.7,
    }
    values.update(overrides)
    return values


def test_exercise_detail_combines_progress_and_records(tmp_path):
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((path, dict(request.url.params)))
        if path == "/api/v1/exercises/Back Squat/progress":
            return httpx.Response(
                200,
                json=envelope(
                    count=3,
                    items=[
                        progress_item("2026-01-05", 120.0, 100.0),
                        progress_item("2026-01-12", 132.0, 110.0),
                        progress_item("2026-01-19", None, None, sets=0, volume_kg=0),
                    ],
                ),
            )
        if path == "/api/v1/exercises/Back Squat/records":
            return httpx.Response(
                200,
                json=envelope(
                    count=2,
                    items=[exercise_record(8, 110.0, 132.0), exercise_record(5, 120.0, 140.0)],
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_exercise_detail(
                "2026-01-01", "2026-01-31", "Back Squat", group_by="session", max_points=50
            )
        finally:
            await client.aclose()

    out = run(go())
    assert out["exercise"] == "Back Squat"
    assert out["total_points"] == 3
    assert out["first"]["date"] == "2026-01-05"
    assert out["last"]["date"] == "2026-01-19"
    assert out["sets"] == 8
    assert out["volume_kg"] == 2000.0
    assert out["best_est_1rm"] == {"kg": 132.0, "date": "2026-01-12"}
    assert out["top_weight"] == {"kg": 110.0, "date": "2026-01-12"}
    assert out["change_pct"] == 10.0
    assert [row["reps"] for row in out["records"]] == [5, 8]
    assert out["records"][0]["best_est_1rm_kg"] == 140.0
    assert "Epley" in out["record_definition"]
    assert out["truncated"] is False
    assert out["period"] == {"start": "2026-01-01", "end": "2026-01-31"}
    assert seen[0][1]["group_by"] == "session"


def test_exercise_detail_caps_points_and_reports_truncation(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/progress"):
            return httpx.Response(
                200,
                json=envelope(
                    count=250,
                    items=[progress_item(f"2026-{index:04d}", 100.0, 80.0) for index in range(250)],
                ),
            )
        return httpx.Response(200, json=envelope(count=0, items=[]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_exercise_detail(
                "2026-01-01", "2026-01-31", "Back Squat", max_points=100
            )
        finally:
            await client.aclose()

    out = run(go())
    assert out["total_points"] == 250
    assert out["truncated"] is True
    assert len(out["points"]) == 100


def test_exercise_detail_returns_an_error_for_an_unknown_exercise(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Unknown exercise"}}
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_exercise_detail("2026-01-01", "2026-01-31", "Nope")
        finally:
            await client.aclose()

    out = run(go())
    assert out["source"] == "strong"
    assert "Unknown exercise" in out["error"]
    assert "cache_age_hours" in out


def test_blank_exercise_is_an_error_not_a_request(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=envelope(count=0, items=[]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_exercise_detail("2026-01-01", "2026-01-31", "   ")
        finally:
            await client.aclose()

    out = run(go())
    assert out["error"] == "exercise must not be blank"
    assert calls["n"] == 0


def test_lifting_records_rank_and_truncate(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/records/est-1rm":
            return httpx.Response(
                200,
                json=envelope(
                    count=3,
                    items=[
                        {
                            "exercise": "Squat",
                            "est_1rm_kg": 100.0,
                            "weight_kg": 80.0,
                            "reps": 8,
                            "set_time": "2026-01-05T18:00:00",
                        },
                        {
                            "exercise": "Deadlift",
                            "est_1rm_kg": 140.0,
                            "weight_kg": 120.0,
                            "reps": 5,
                            "set_time": "2026-01-06T18:00:00",
                        },
                        {
                            "exercise": "Bench",
                            "est_1rm_kg": 120.0,
                            "weight_kg": 95.0,
                            "reps": 8,
                            "set_time": "2026-01-07T18:00:00",
                        },
                    ],
                ),
            )
        if request.url.path == "/api/v1/records/bodyweight":
            return httpx.Response(
                200,
                json=envelope(
                    count=2,
                    items=[
                        {
                            "exercise": "Pull-up",
                            "reps": 12,
                            "seconds": None,
                            "distance": None,
                            "set_time": "2026-01-05T18:00:00",
                        },
                        {
                            "exercise": "Plank",
                            "reps": 1,
                            "seconds": 120.0,
                            "distance": None,
                            "set_time": "2026-01-06T18:00:00",
                        },
                    ],
                ),
            )
        raise AssertionError(request.url.path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            estimate = await source.get_lifting_records(
                "2026-01-01", "2026-01-31", kind="est_1rm", limit=2
            )
            bodyweight = await source.get_lifting_records(
                "2026-01-01", "2026-01-31", kind="bodyweight"
            )
        finally:
            await client.aclose()
        return estimate, bodyweight

    estimate, bodyweight = run(go())
    assert estimate["kind"] == "est_1rm"
    assert estimate["total"] == 3
    assert estimate["returned"] == 2
    assert estimate["truncated"] is True
    assert [row["exercise"] for row in estimate["records"]] == ["Deadlift", "Bench"]
    assert "Epley" in estimate["definition"]
    assert bodyweight["records"][0]["exercise"] == "Pull-up"
    assert bodyweight["records"][0]["reps"] == 12
    assert bodyweight["records"][1]["seconds"] == 120.0
    assert "zero volume" in bodyweight["definition"]


def test_lifting_sets_shape_clamp_and_pagination(tmp_path):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=envelope(
                items=[set_item(1), set_item(2)],
                count=120,
                next="next",
                previous=None,
                has_more=True,
            ),
        )

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_lifting_sets(
                "2026-01-01", "2026-01-31", exercise="Back Squat", limit=500
            )
        finally:
            await client.aclose()

    out = run(go())
    assert seen["limit"] == "200"
    assert seen["exercise"] == "Back Squat"
    assert seen["sort"] == "set_time"
    assert out["limit"] == 200
    assert out["count"] == 120
    assert out["returned"] == 2
    assert out["next_offset"] == 2
    assert out["has_more"] is True
    assert out["sets"][0] == {
        "id": 1,
        "session_id": 1,
        "date": "2026-01-05",
        "exercise": "Back Squat",
        "muscle": "legs",
        "set_order": 1,
        "weight_kg": 100.0,
        "reps": 5,
        "rpe": 8.0,
        "volume_kg": 500.0,
        "est_1rm_kg": 116.7,
    }


def test_yearly_progress_uses_year_end_for_a_past_year(tmp_path):
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(dict(request.url.params))
        if path == "/api/v1/yearly":
            return httpx.Response(
                200,
                json=envelope(
                    count=1,
                    items=[
                        {
                            "year": 2024,
                            "sessions": 120,
                            "sets": 900,
                            "volume_kg": 500000.0,
                            "volume_t": 500.0,
                            "hours": 90.0,
                        }
                    ],
                ),
            )
        if path == "/api/v1/yearly/pace":
            return httpx.Response(
                200,
                json=envelope(
                    year=2024,
                    ytd={"sessions": 120, "sets": 900, "volume_kg": 500000.0, "hours": 90.0},
                    last_year_to_date={
                        "sessions": 110,
                        "sets": 850,
                        "volume_kg": 450000.0,
                        "hours": 85.0,
                    },
                    projected={"sessions": 120, "sets": 900, "volume_kg": 500000.0, "hours": 90.0},
                    needed_per_week={"sessions": 0, "sets": 0, "volume_kg": 0, "hours": 0},
                    pace=[
                        {
                            "year": 2024,
                            "sessions": 120,
                            "volume_kg": 500000.0,
                            "cumulative_sessions": 120,
                            "cumulative_volume_kg": 500000.0,
                        }
                    ],
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_yearly_progress(year=2024)
        finally:
            await client.aclose()

    out = run(go())
    assert seen[-1]["as_of"] == "2024-12-31"
    assert out["year"] == 2024
    assert out["as_of"] == "2024-12-31"
    assert out["totals_by_year"][0]["volume_t"] == 500.0
    assert out["ytd"]["sessions"] == 120
    assert out["pace"][0]["cumulative_sessions"] == 120
    assert "projected" in out["pace_definition"]
    assert out["period"] == {"start": "2024-01-01", "end": "2024-12-31"}


def test_yearly_progress_curve_only_on_request_and_capped(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/yearly":
            return httpx.Response(200, json=envelope(count=0, items=[]))
        if path == "/api/v1/yearly/pace":
            return httpx.Response(
                200,
                json=envelope(
                    year=2026,
                    ytd={"sessions": 1, "sets": 1, "volume_kg": 1.0, "hours": 1.0},
                    last_year_to_date={"sessions": 1, "sets": 1, "volume_kg": 1.0, "hours": 1.0},
                    projected={"sessions": 1, "sets": 1, "volume_kg": 1.0, "hours": 1.0},
                    needed_per_week={"sessions": 1, "sets": 1, "volume_kg": 1.0, "hours": 1.0},
                    pace=[],
                ),
            )
        if path == "/api/v1/yearly/curve":
            assert request.url.params["years"] == "2025,2026"
            return httpx.Response(
                200,
                json=envelope(
                    year=2026,
                    years=[2025, 2026],
                    count=40,
                    items=[
                        {
                            "year": 2026,
                            "date": "2026-01-01",
                            "day_of_year": index + 1,
                            "sessions": 1,
                            "cumulative_sessions": index + 1,
                            "cumulative_volume_kg": 1000.0 * (index + 1),
                        }
                        for index in range(40)
                    ],
                ),
            )
        raise AssertionError(path)

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_yearly_progress(
                year=2026,
                as_of="2026-06-30",
                include_curve=True,
                curve_years="2025,2026",
                max_points=10,
            )
        finally:
            await client.aclose()

    out = run(go())
    assert out["curve"]["total_points"] == 40
    assert out["curve"]["truncated"] is True
    assert len(out["curve"]["points"]) == 10
    assert out["curve"]["years"] == [2025, 2026]


def test_yearly_progress_rejects_a_bad_as_of_or_curve_years(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=envelope(count=0, items=[]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            wrong_year = await source.get_yearly_progress(year=2024, as_of="2025-01-01")
            bad_years = await source.get_yearly_progress(year=2024, curve_years="24,25")
            bad_date = await source.get_yearly_progress(as_of="2026-13-01")
        finally:
            await client.aclose()
        return wrong_year, bad_years, bad_date

    wrong_year, bad_years, bad_date = run(go())
    assert "not in year 2024" in wrong_year["error"]
    assert "curve_years" in bad_years["error"]
    assert "Invalid date" in bad_date["error"]
    assert calls["n"] == 0


def test_cache_keys_stay_safe_for_exercise_names(tmp_path):
    key = StrongSource._key("exercises/Back Squat (barbell)/progress", {"start": "2026-01-01"})
    assert SAFE_NAME.match(key)
    assert " " not in key and "(" not in key


def test_an_empty_exercise_result_suggests_nearby_names(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/exercises":
            return httpx.Response(
                200,
                json=envelope(
                    count=3,
                    items=[{"name": "Squat"}, {"name": "Front Squat"}, {"name": "Bench Press"}],
                ),
            )
        return httpx.Response(200, json=envelope(count=0, items=[]))

    source, client = make_source(tmp_path, handler)

    async def go():
        try:
            return await source.get_exercise_detail("2026-01-01", "2026-01-31", "Squatt")
        finally:
            await client.aclose()

    out = run(go())
    assert out["total_points"] == 0
    assert "Squat" in out["suggestions"]
    assert "suggestion_note" in out
