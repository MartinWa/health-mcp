from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from health_mcp.utils import (
    event_days,
    filter_range,
    parse_date,
    to_local_dates,
    truncate_series,
    validate_range,
)


def test_parse_date_is_strict():
    assert parse_date("2026-01-31") == date(2026, 1, 31)
    for bad in ("2026-1-31", "31-01-2026", "2026/01/31", "not a date", ""):
        with pytest.raises(ValueError):
            parse_date(bad)


def test_validate_range_rejects_reversed_and_too_long():
    with pytest.raises(ValueError, match="after end_date"):
        validate_range("2026-02-01", "2026-01-01")
    with pytest.raises(ValueError, match="too large"):
        validate_range("2019-01-01", "2026-01-02")


def test_date_only_values_are_not_shifted_by_tz():
    values = pd.Series(["2026-01-01", "2026-01-02"])
    local = to_local_dates(values, "Europe/Stockholm")
    assert [str(value)[:10] for value in local] == ["2026-01-01", "2026-01-02"]


def test_utc_timestamp_is_truncated_after_tz_conversion():
    # 23:30 UTC on Jan 30 is 00:30 local on Jan 31 (D13).
    local = to_local_dates(pd.Series(["2026-01-30T23:30:00Z"]), "Europe/Stockholm")
    assert str(local.iloc[0])[:10] == "2026-01-31"


def test_filter_range_keeps_a_record_after_midnight_on_the_end_date():
    frame = pd.DataFrame(
        {
            "recorded_at": [
                "2026-01-30T14:00:00Z",
                "2026-01-31T14:00:00Z",
                "2026-02-01T00:00:00Z",
            ],
            "value": [1, 2, 3],
        }
    )
    filtered = filter_range(frame, "recorded_at", "2026-01-01", "2026-01-31")
    assert filtered["value"].tolist() == [1, 2]


def test_filter_range_uses_local_date_for_late_utc_records():
    frame = pd.DataFrame(
        {"recorded_at": ["2026-01-30T23:30:00Z"], "value": [7]},
    )
    assert filter_range(frame, "recorded_at", "2026-01-31", "2026-01-31")["value"].tolist() == [7]
    assert filter_range(frame, "recorded_at", "2026-01-30", "2026-01-30").empty


def test_filter_range_returns_empty_frame_with_same_columns():
    frame = pd.DataFrame({"date": ["2026-01-01"], "value": [1]})
    out = filter_range(frame, "date", "2026-06-01", "2026-06-02")
    assert out.empty
    assert list(out.columns)[:2] == ["date", "value"]


def test_truncate_series_evenly_downsamples():
    dates = [f"2026-01-{day:02d}" for day in range(1, 31)]
    values = list(range(30))
    capped_dates, capped_values, total, truncated = truncate_series(dates, values, 10)
    assert total == 30
    assert truncated is True
    assert len(capped_dates) == len(capped_values) == 10
    assert capped_dates[0] == "2026-01-01"
    assert capped_dates[-1] == "2026-01-28"


def test_truncate_series_respects_hard_cap():
    dates = [str(index) for index in range(5000)]
    values = list(range(5000))
    capped_dates, _, total, truncated = truncate_series(dates, values, 99999)
    assert total == 5000
    assert truncated is True
    assert len(capped_dates) == 1000


def test_event_days_covers_a_daily_range_clipped_to_the_query():
    days = event_days("2026-01-01", "2026-01-10", 1, "2026-01-05", "2026-01-07")
    assert days == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert event_days("2026-01-01", "2026-01-10", None, "2025-12-01", "2026-01-02") == [
        "2026-01-01",
        "2026-01-02",
    ]


def test_event_days_keeps_the_grid_anchored_to_the_row_start():
    days = event_days("2026-01-01", "2026-01-29", 7, "2026-01-05", "2026-01-29")
    assert days == ["2026-01-08", "2026-01-15", "2026-01-22", "2026-01-29"]


def test_event_days_is_empty_outside_the_query_and_for_a_bad_date():
    assert event_days("2026-01-01", None, None, "2026-02-01", "2026-02-28") == []
    assert event_days("2025-01-01", "2025-01-02", None, "2026-02-01", "2026-02-28") == []
    assert event_days("not-a-date", None, None, "2026-01-01", "2026-02-28") == []


def test_event_days_treats_a_missing_end_as_a_single_day():
    assert event_days("2026-01-01", None, None, "2026-01-01", "2026-12-31") == ["2026-01-01"]
    assert event_days("2026-01-01", "2025-12-31", None, "2026-01-01", "2026-12-31") == [
        "2026-01-01"
    ]


def test_event_days_with_a_step_and_no_end_is_ongoing():
    assert event_days("2026-01-01", None, 7, "2026-01-01", "2026-01-31") == [
        "2026-01-01",
        "2026-01-08",
        "2026-01-15",
        "2026-01-22",
        "2026-01-29",
    ]
    assert event_days("2026-01-01", None, 1, "2026-01-30", "2026-02-01") == [
        "2026-01-30",
        "2026-01-31",
        "2026-02-01",
    ]
    assert event_days("2026-01-01", "", 14, "2026-03-01", "2026-03-31") == [
        "2026-03-12",
        "2026-03-26",
    ]
    assert event_days("2026-01-01", None, 7, "2025-12-01", "2025-12-31") == []
