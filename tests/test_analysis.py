from __future__ import annotations

from helpers import envelope

from health_mcp import analysis as A


def test_sleep_training_correlation_lag_one_uses_the_next_night():
    volume = {"2026-01-01": 100.0, "2026-01-03": 300.0, "2026-01-05": 500.0}
    sleep = {"2026-01-02": 7.0, "2026-01-04": 8.0, "2026-01-06": 9.0}
    out = A.sleep_training_corr(sleep, volume, 1, {"start": "2026-01-01", "end": "2026-01-06"})
    assert out["n"] == 3
    assert out["lag_days"] == 1
    assert out["r"] == 1.0
    assert out["definition"] == "lifting volume on D vs sleep on D+1"
    assert out["period"] == {"start": "2026-01-01", "end": "2026-01-06"}
    assert "positive" in out["interpretation"]


def test_sleep_training_correlation_requires_three_overlapping_days():
    out = A.sleep_training_corr({"2026-01-02": 7.0}, {"2026-01-01": 100.0}, 1)
    assert out["n"] == 1
    assert "error" in out


def test_sleep_training_correlation_handles_zero_variance():
    volume = {"2026-01-01": 100.0, "2026-01-02": 100.0, "2026-01-03": 100.0}
    sleep = {"2026-01-02": 7.0, "2026-01-03": 8.0, "2026-01-04": 9.0}
    out = A.sleep_training_corr(sleep, volume, 1)
    assert "error" in out


def test_consistency_reports_days_and_percentages():
    out = A.consistency(10, {"weight": 5, "sleep": 8})
    assert out == {
        "days": 10,
        "weight_days": 5,
        "weight_pct": 50.0,
        "sleep_days": 8,
        "sleep_pct": 80.0,
    }


def test_strong_summary_marks_an_empty_period():
    out = A.strong_summary(envelope(sessions=0, sets=0, volume_kg=0.0, volume_t=0.0, hours=0.0))
    assert out["sessions"] == 0
    assert out["note"] == "no data in this period"


def test_strong_series_caps_points_without_losing_first_and_last():
    items = [
        {"bucket_start": f"2026-01-{day:02d}", "value": float(day), "groups": None}
        for day in range(1, 31)
    ]
    out = A.strong_series(envelope(bucket="day", group_by=None, count=30, items=items), 10)
    assert out["total_points"] == 30
    assert out["truncated"] is True
    assert len(out["points"]) == 10
    assert out["points"][0]["bucket_start"] == "2026-01-01"


def test_strong_breakdown_computes_share_of_volume():
    payload = envelope(
        count=2,
        items=[
            {"key": "legs", "sessions": 1, "sets": 2, "volume_kg": 300.0},
            {"key": "chest", "sessions": 1, "sets": 2, "volume_kg": 100.0},
        ],
    )
    out = A.strong_breakdown(payload, "muscle")
    assert out["items"][0]["share_pct"] == 75.0
    assert out["items"][1]["share_pct"] == 25.0


def test_schedule_per_day_only_interprets_unambiguous_text():
    assert A.schedule_per_day("daily") == 1.0
    assert A.schedule_per_day("Twice daily") == 2.0
    assert A.schedule_per_day("3x daily") == 3.0
    assert A.schedule_per_day("weekly") == 1 / 7
    assert A.schedule_per_day("as needed") is None
    assert A.schedule_per_day("") is None
    assert A.schedule_per_day(None) is None


def test_medication_adherence_ignores_symptoms_and_unplanned_doses():
    planned = [
        {
            "kind": "medication",
            "substance": "example-med",
            "dose": 50,
            "unit": "µg",
            "schedule": "daily",
        },
        {
            "kind": "supplement",
            "substance": "creatine",
            "dose": 5,
            "unit": "g",
            "schedule": "twice daily",
        },
    ]
    doses = [
        {"date": "2026-11-20", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-21", "kind": "medication", "substance": "Example-Med"},
        {"date": "2026-11-21", "kind": "symptom", "substance": ""},
        {"date": "2026-11-21", "kind": "supplement", "substance": "creatine"},
        {"date": "2026-11-21", "kind": "supplement", "substance": "unplanned"},
    ]
    out = A.medication_adherence(planned, doses, "2026-11-20", "2026-11-21")
    assert out["period"] == {"start": "2026-11-20", "end": "2026-11-21", "days": 2}
    assert out["n"] == 2
    assert [row["substance"] for row in out["items"]] == ["example-med", "creatine"]
    medication, supplement = out["items"]
    assert (medication["expected"], medication["logged"]) == (2.0, 2)
    assert medication["missing_days"] == 0
    assert (supplement["expected"], supplement["logged"]) == (4.0, 1)
    assert supplement["missing_days"] == 1
    assert out["expected"] == 6.0
    assert out["logged"] == 3
    assert out["adherence_pct"] == 50.0
    assert out["unplanned_logged"] == [
        {"substance": "unplanned", "kind": "supplement", "logged": 1}
    ]
    assert "kind medication or supplement" in out["definition"]


def test_medication_adherence_without_interpretable_schedules():
    planned = [{"kind": "medication", "substance": "example-med", "schedule": "as needed"}]
    out = A.medication_adherence(planned, [], "2026-11-20", "2026-11-21")
    assert out["n"] == 0
    assert out["adherence_pct"] is None
    assert out["unparsed_schedules"][0]["substance"] == "example-med"
    assert "cannot be computed" in out["interpretation"]


def test_adherence_groups_plan_rows_for_one_substance_without_double_counting():
    planned = [
        {
            "kind": "medication",
            "substance": "example-med",
            "dose": 50,
            "unit": "µg",
            "schedule": "daily",
        },
        {
            "kind": "medication",
            "substance": "Example-Med",
            "dose": 25,
            "unit": "µg",
            "schedule": "daily",
        },
    ]
    doses = [
        {"date": "2026-11-01", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-02", "kind": "medication", "substance": "example-med"},
    ]
    out = A.medication_adherence(planned, doses, "2026-11-01", "2026-11-02")
    assert out["n"] == 1
    item = out["items"][0]
    assert item["plan_rows"] == 2
    assert item["per_day"] == 2.0
    assert item["expected"] == 4.0
    assert item["logged"] == 2
    assert out["expected"] == 4.0
    assert out["logged"] == 2
    assert out["adherence_pct"] == 50.0
    assert out["unplanned_logged"] == []


def test_adherence_counts_a_substance_once_even_with_two_kinds():
    planned = [
        {"kind": "medication", "substance": "example-med", "schedule": "daily"},
        {"kind": "supplement", "substance": "example-med", "schedule": "daily"},
    ]
    doses = [
        {"date": "2026-11-01", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-01", "kind": "supplement", "substance": "example-med"},
    ]
    out = A.medication_adherence(planned, doses, "2026-11-01", "2026-11-01")
    assert out["n"] == 2
    assert out["logged"] == 2
    assert out["expected"] == 2.0
    assert out["adherence_pct"] == 100.0


def test_workload_includes_an_intersecting_midweek_bucket_and_flags_it_incomplete():
    series = {
        "items": [
            {"bucket_start": "2026-10-05", "value": 100.0},
            {"bucket_start": "2026-10-12", "value": 100.0},
            {"bucket_start": "2026-10-19", "value": 100.0},
            {"bucket_start": "2026-10-26", "value": 100.0},
            {"bucket_start": "2026-11-02", "value": 140.0},
        ]
    }
    out = A.workload(series, "2026-11-03", "2026-11-05")
    assert [row["week_start"] for row in out["weeks"]] == ["2026-11-02"]
    row = out["weeks"][0]
    assert row["complete"] is False
    assert row["flag"] == "incomplete"
    assert row["baseline_4w"] == 100.0
    assert row["acwr"] == 1.4


def test_workload_keeps_full_week_flags_for_a_complete_monday_to_sunday_range():
    series = {
        "items": [
            {"bucket_start": "2026-10-05", "value": 100.0},
            {"bucket_start": "2026-10-12", "value": 100.0},
            {"bucket_start": "2026-10-19", "value": 100.0},
            {"bucket_start": "2026-10-26", "value": 100.0},
            {"bucket_start": "2026-11-02", "value": 140.0},
        ]
    }
    out = A.workload(series, "2026-11-02", "2026-11-08")
    assert out["weeks"][0]["complete"] is True
    assert out["weeks"][0]["flag"] == "spike"


def test_adherence_excludes_a_mixed_scheduled_and_as_needed_substance():
    planned = [
        {"kind": "medication", "substance": "example-med", "schedule": "daily"},
        {"kind": "medication", "substance": "example-med", "schedule": "as needed"},
    ]
    doses = [
        {"date": "2026-11-01", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-01", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-02", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-02", "kind": "medication", "substance": "example-med"},
    ]
    out = A.medication_adherence(planned, doses, "2026-11-01", "2026-11-02")
    assert out["n"] == 0
    assert out["adherence_pct"] is None
    assert len(out["unparsed_schedules"]) == 1
    entry = out["unparsed_schedules"][0]
    assert entry["substance"] == "example-med"
    assert entry["scheduled_per_day"] == 1.0
    assert entry["expected_scheduled"] == 2.0
    assert entry["logged"] == 4
    assert "no percentage" in entry["note"]
    assert "cannot be computed" in out["interpretation"]


def test_adherence_mixed_substance_does_not_change_a_scheduled_one():
    planned = [
        {"kind": "medication", "substance": "example-med", "schedule": "daily"},
        {"kind": "supplement", "substance": "creatine", "schedule": "daily"},
        {"kind": "supplement", "substance": "creatine", "schedule": "as needed"},
    ]
    doses = [
        {"date": "2026-11-01", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-02", "kind": "medication", "substance": "example-med"},
        {"date": "2026-11-01", "kind": "supplement", "substance": "creatine"},
    ]
    out = A.medication_adherence(planned, doses, "2026-11-01", "2026-11-02")
    assert out["n"] == 1
    assert out["items"][0]["substance"] == "example-med"
    assert out["adherence_pct"] == 100.0
    assert [row["substance"] for row in out["unparsed_schedules"]] == ["creatine"]
