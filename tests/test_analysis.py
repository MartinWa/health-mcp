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
