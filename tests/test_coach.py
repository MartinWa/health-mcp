"""Tier C/D analysis helpers, pure functions only."""

from __future__ import annotations

import pandas as pd

from health_mcp import analysis as A


def session_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "start_time": "2026-11-01T23:00:00Z",
                "end_time": "2026-11-02T07:00:00Z",
                "sleep_duration_seconds": 7 * 3600,
                "duration_seconds": 8 * 3600,
                "efficiency_percent": 90,
                "is_nap": False,
                "stages.deep_minutes": 60,
                "stages.rem_minutes": 90,
                "stages.light_minutes": 240,
                "stages.awake_minutes": 30,
                "source.provider": "apple",
            },
            {
                "start_time": "2026-11-02T23:30:00Z",
                "end_time": "2026-11-03T07:30:00Z",
                "sleep_duration_seconds": 6.5 * 3600,
                "duration_seconds": 8 * 3600,
                "efficiency_percent": 80,
                "is_nap": False,
                "stages.deep_minutes": 40,
                "stages.rem_minutes": 70,
                "stages.light_minutes": 250,
                "stages.awake_minutes": 60,
                "source.provider": "apple",
            },
            {
                "start_time": "2026-11-03T14:00:00Z",
                "end_time": "2026-11-03T14:30:00Z",
                "sleep_duration_seconds": 1800,
                "duration_seconds": 1800,
                "efficiency_percent": 70,
                "is_nap": True,
                "stages.deep_minutes": 5,
                "stages.rem_minutes": 5,
                "stages.light_minutes": 20,
                "stages.awake_minutes": 0,
                "source.provider": "garmin",
            },
        ]
    )


def test_body_summary_reads_slow_changing_and_keeps_nulls():
    payload = {
        "source": {"provider": "withings", "device_name": "Body+"},
        "slow_changing": {"weight_kg": 72.5, "height_cm": 175.5, "body_fat_percent": 18.5},
        "averaged": {"period_days": 7, "resting_heart_rate_bpm": 62},
        "latest": {},
    }
    out = A.body_summary(payload)
    assert out["static"]["weight_kg"] == 72.5
    assert out["static"]["body_fat_pct"] == 18.5
    assert out["static"]["muscle_mass_kg"] is None
    assert out["averaged"]["resting_heart_rate_bpm"] == 62
    assert out["latest"]["body_temperature_celsius"] is None
    assert out["source"]["provider"] == "withings"
    assert A.body_summary({}) == {"error": "No body data in Open Wearables"}


def test_data_sources_summary_counts_providers_and_caps():
    sources = {
        "total": 3,
        "items": [
            {"provider": "apple", "device_name": "Watch", "device_type": "watch"},
            {"provider": "apple", "device_name": "iPhone", "device_type": "phone"},
            {"provider": "withings", "device_name": "Body+"},
        ],
    }
    connections = [
        {"provider": "apple", "status": "active", "rest_pull": True, "webhook_stream": False}
    ]
    counts = {
        "total_data_points": 1000,
        "total_workouts": 20,
        "total_sleep_events": 30,
        "series_type_counts": {"heart_rate": 500, "steps": 300, "weight": 5},
        "workout_type_counts": {"running": 12},
        "by_provider": [
            {"provider": "apple", "data_points": 900, "workout_count": 20, "sleep_count": 30}
        ],
    }
    out = A.data_sources_summary(sources, connections, counts, max_sources=2)
    assert out["sources"]["providers"] == {"apple": 2, "withings": 1}
    assert out["sources"]["returned"] == 2
    assert out["sources"]["truncated"] is True
    assert out["connections"]["items"][0]["capabilities"] == {
        "rest_pull": True,
        "webhook_stream": False,
    }
    assert out["counts"]["series_type_counts"] == {"heart_rate": 500, "steps": 300, "weight": 5}
    assert out["counts"]["by_provider"][0]["sleep_count"] == 30


def test_sleep_session_stats_separates_naps_and_caps_rows():
    out = A.sleep_session_stats(session_frame(), "Europe/Stockholm")
    assert out["count"] == 3
    assert out["main_sessions"] == 2
    assert out["naps"] == 1
    assert out["nights"] == 2
    assert out["avg_efficiency_pct"] == 80.0
    assert out["total_hours"] == 14.0
    assert out["stages"]["avg_deep_min"] == 35.0
    assert out["providers"] == {"apple": 2, "garmin": 1}
    assert out["truncated"] is False
    assert out["sessions"][-1]["is_nap"] is True

    capped = A.sleep_session_stats(session_frame(), "Europe/Stockholm", max_sessions=1)
    assert capped["returned"] == 1
    assert capped["truncated"] is True
    assert A.sleep_session_stats(pd.DataFrame(), "Europe/Stockholm") == {
        "count": 0,
        "note": "no sleep sessions in this period",
    }


def test_logging_stats_reports_pct_missing_days_and_streaks():
    out = A.logging_stats(
        {
            "workouts": ["2026-11-01", "2026-11-02", "2026-11-05"],
            "sleep": [f"2026-11-0{day}" for day in range(1, 8)],
            "weight": ["2026-11-03"],
        },
        "2026-11-01",
        "2026-11-07",
    )
    assert out["period"] == {"start": "2026-11-01", "end": "2026-11-07", "days": 7}
    workouts = out["workouts"]
    assert workouts["days_logged"] == 3
    assert workouts["pct"] == 42.9
    assert workouts["missing_days"] == 4
    assert workouts["current_streak"] == 0
    assert workouts["best_streak"] == 2
    assert workouts["missing_first"] == [
        "2026-11-03",
        "2026-11-04",
        "2026-11-06",
        "2026-11-07",
    ]
    assert out["sleep"]["current_streak"] == 7
    assert out["weight"]["best_streak"] == 1


def test_workload_flags_spikes_drops_and_filters_the_period():
    weeks = [100.0, 100.0, 100.0, 100.0, 140.0, 60.0, 100.0]
    series = {
        "items": [
            {"bucket_start": f"2026-{10 + index // 4}-{(index % 4) * 7 + 5:02d}", "value": value}
            for index, value in enumerate(weeks)
        ]
    }
    out = A.workload(series, "2026-11-02", "2026-11-30")
    starts = [row["week_start"] for row in out["weeks"]]
    assert starts == ["2026-11-05", "2026-11-12", "2026-11-19"]
    assert [row["flag"] for row in out["weeks"]] == ["spike", "drop", "in_range"]
    assert out["current"]["acwr"] == 1.0
    assert out["weeks"][0]["baseline_4w"] == 100.0
    empty = A.workload({"items": []}, "2026-11-01", "2026-11-30")
    assert empty["weeks"] == [] and empty["current"] is None


def test_readiness_inputs_show_values_and_averages():
    days = [
        {"date": "2026-11-01", "recovery_score": 60, "sleep_hours": 7.0, "prior_day_volume_kg": 0},
        {
            "date": "2026-11-02",
            "recovery_score": 70,
            "sleep_hours": 8.0,
            "prior_day_volume_kg": 5000,
        },
        {
            "date": "2026-11-03",
            "recovery_score": None,
            "sleep_hours": None,
            "prior_day_volume_kg": None,
        },
    ]
    out = A.readiness_inputs(days, max_days=2)
    assert out["n"] == 3
    assert out["averages"] == {
        "recovery_score": 65.0,
        "sleep_hours": 7.5,
        "prior_day_volume_kg": 2500.0,
    }
    assert len(out["days"]) == 2
    assert out["truncated"] is True
    assert "No readiness score" in out["definition"]


def test_weight_goal_progress_projects_from_the_period_rate():
    out = A.weight_goal_progress(
        [("2026-11-01", 80.0), ("2026-11-15", 79.0)],
        78.0,
        target_source="profile",
        deadline="2027-01-01",
    )
    assert out["remaining_kg"] == 1.0
    assert out["change_kg"] == -1.0
    assert out["rate_kg_per_week"] == -0.5
    assert out["progress_pct"] == 50.0
    assert out["projected_date"] == "2026-11-29"
    assert out["on_track"] is True
    flat = A.weight_goal_progress(
        [("2026-11-01", 80.0), ("2026-11-08", 80.0)], 78.0, target_source="argument_override"
    )
    assert flat["progress_pct"] == 0.0
    assert flat["projected_date"] is None
    assert flat["on_track"] is None
    assert "error" in A.weight_goal_progress([("2026-11-01", 80.0)], 78.0, target_source="profile")


def test_workout_goal_progress_labels_the_override():
    out = A.workout_goal_progress(10, 20, target_source="argument_override")
    assert out["progress_pct"] == 50.0
    assert out["remaining"] == 10.0
    assert out["target_source"] == "argument_override"


def test_medicine_logs_separates_doses_symptoms_and_notes():
    rows = [
        {
            "date": "2026-11-20",
            "time": "08:00",
            "kind": "medication",
            "substance": "a",
            "dose": 50,
            "unit": "µg",
        },
        {
            "date": "2026-11-21",
            "time": "08:10",
            "kind": "medication",
            "substance": "a",
            "dose": 50,
            "unit": "µg",
        },
        {
            "date": "2026-11-21",
            "time": "21:00",
            "kind": "supplement",
            "substance": "b",
            "dose": 5,
            "unit": "g",
        },
        {"date": "2026-11-21", "time": "22:00", "kind": "symptom", "notes": "knee ache"},
        {"date": "2026-11-22", "time": "09:00", "kind": "symptom", "notes": "headache"},
        {"date": "2026-11-22", "time": "10:00", "kind": "note", "notes": "felt good"},
    ]
    out = A.medicine_logs(rows, "2026-11-20", "2026-11-22")
    assert out["n"] == 6
    assert [row["substance"] for row in out["doses"]] == ["a", "b"]
    assert out["doses"][0]["count"] == 2
    assert out["doses"][0]["dose_total"] == 100.0
    assert out["doses"][0]["days_logged"] == 2
    assert out["doses"][0]["times"] == ["08:00", "08:10"]
    assert out["symptoms_count"] == 2
    assert out["symptoms"][0]["notes"] == "knee ache"
    assert out["notes_count"] == 1
    assert "never" not in "".join(row["substance"] for row in out["doses"])


def test_medicine_impact_compares_days_and_reports_insufficient_data():
    doses = [
        {"date": "2026-11-01", "kind": "medication", "substance": "Example-Med"},
        {"date": "2026-11-02", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-03", "kind": "medication", "substance": "Example-Med"},
    ]
    series = {
        "recovery_score": {
            "2026-11-01": 40,
            "2026-11-02": 50,
            "2026-11-03": 60,
            "2026-11-04": 70,
            "2026-11-05": 80,
            "2026-11-06": 90,
        },
        "resting_heart_rate_bpm": {
            "2026-11-01": 60,
            "2026-11-02": 61,
            "2026-11-04": 65,
            "2026-11-05": 66,
        },
    }
    out = A.medicine_impact(doses, series, "2026-11-01", "2026-11-06")
    assert out["n_substances"] == 1
    subject = out["subjects"][0]
    assert subject["substance"] == "example-med"
    assert subject["days_with"] == 3
    assert subject["metrics"]["recovery_score"]["mean_with"] == 50.0
    assert subject["metrics"]["recovery_score"]["mean_without"] == 80.0
    assert subject["metrics"]["recovery_score"]["difference"] == -30.0
    assert subject["metrics"]["resting_heart_rate_bpm"]["status"] == "insufficient_days"
    assert A.medicine_impact([], series, "2026-11-01", "2026-11-06")["n_substances"] == 0
