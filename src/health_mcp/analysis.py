"""Pure analysis: DataFrame/JSON in, JSON-serializable dict out. No I/O.

Every function is defensive about missing columns (returns ``{"error": ...}``)
instead of raising — a source returning a partial payload must degrade an answer,
not the server.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pandas as pd

from .utils import (
    HARD_MAX_POINTS,
    parse_date,
    safe_mean,
    to_local_dates,
    truncate_series,
)

WEEKS_IN_YEAR = 52


def attach_series(
    stats: dict[str, Any],
    include_series: bool,
    max_points: int,
    date_key: str = "dates",
    value_key: str = "values",
) -> dict[str, Any]:
    """Attach truncation metadata; strip the series unless it was requested."""
    if "error" in stats:
        return stats
    dates = stats.pop(date_key, []) or []
    values = stats.pop(value_key, []) or []
    stats["total_points"] = len(dates)
    if not include_series:
        stats["series_included"] = False
        return stats
    capped_dates, capped_values, _, truncated = truncate_series(
        list(dates), list(values), max_points
    )
    stats.update(
        series_included=True,
        dates=capped_dates,
        values=capped_values,
        truncated=truncated,
    )
    return stats


def _need(df: pd.DataFrame, cols: Sequence[str]) -> str | None:
    if df is None or len(df) == 0:
        return "No data for this period"
    missing = [col for col in cols if col not in df.columns]
    if missing:
        return f"Data missing columns: {missing}"
    return None


def _dates(df: pd.DataFrame, col: str = "_local_date") -> list[str]:
    if col in df.columns:
        return [str(value)[:10] for value in df[col].tolist()]
    return []


def weight_stats(df: pd.DataFrame) -> dict[str, Any]:
    err = _need(df, ["date", "weight_kg"])
    if err:
        return {"error": err}
    d = df.dropna(subset=["weight_kg"]).sort_values("date")
    if len(d) < 2:
        return {"error": "Need at least 2 weight points"}
    values: Any = d["weight_kg"]
    first, last = float(values.iloc[0]), float(values.iloc[-1])
    days = max((d["_local_date"].iloc[-1] - d["_local_date"].iloc[0]).days, 1)
    return {
        "start_kg": round(first, 2),
        "end_kg": round(last, 2),
        "change_kg": round(last - first, 2),
        "rate_kg_per_week": round((last - first) / days * 7, 3),
        "min_kg": round(float(values.min()), 2),
        "max_kg": round(float(values.max()), 2),
        "points": len(d),
        "dates": _dates(d, "date"),
        "values": [round(float(value), 2) for value in values.tolist()],
    }


def sleep_stats(df: pd.DataFrame) -> dict[str, Any]:
    err = _need(df, ["date", "duration_minutes"])
    if err:
        return {"error": err}
    durations: Any = df["duration_minutes"]
    out: dict[str, Any] = {
        "avg_hours": round(float(durations.mean()) / 60, 2),
        "avg_efficiency": safe_mean(df["efficiency_percent"])
        if "efficiency_percent" in df.columns
        else None,
        "points": len(df),
        "dates": _dates(df, "date"),
        "hours": [round(float(value) / 60, 2) for value in durations.tolist()],
    }
    for column, key in [
        ("stages.deep_minutes", "avg_deep_min"),
        ("stages.rem_minutes", "avg_rem_min"),
        ("stages.light_minutes", "avg_light_min"),
    ]:
        if column in df.columns:
            out[key] = safe_mean(df[column])
    return out


def activity_stats(df: pd.DataFrame) -> dict[str, Any]:
    err = _need(df, ["date", "steps"])
    if err:
        return {"error": err}
    steps: Any = df["steps"]
    return {
        "avg_steps": int(steps.mean()),
        "avg_active_kcal": safe_mean(df["active_calories_kcal"])
        if "active_calories_kcal" in df.columns
        else None,
        "avg_total_kcal": safe_mean(df["total_calories_kcal"])
        if "total_calories_kcal" in df.columns
        else None,
        "points": len(df),
        "dates": _dates(df, "date"),
        "steps": [int(value) if pd.notna(value) else None for value in steps.tolist()],
    }


def recovery_stats(df: pd.DataFrame) -> dict[str, Any]:
    err = _need(df, ["date", "recovery_score"])
    if err:
        return {"error": err}
    scores: Any = df["recovery_score"]
    return {
        "avg_score": safe_mean(scores),
        "avg_resting_hr": safe_mean(df["resting_heart_rate_bpm"])
        if "resting_heart_rate_bpm" in df.columns
        else None,
        "avg_hrv_ms": safe_mean(df["avg_hrv_sdnn_ms"]) if "avg_hrv_sdnn_ms" in df.columns else None,
        "avg_spo2": safe_mean(df["avg_spo2_percent"]) if "avg_spo2_percent" in df.columns else None,
        "points": len(df),
        "dates": _dates(df, "date"),
        "scores": [int(value) if pd.notna(value) else None for value in scores.tolist()],
    }


def workout_stats(
    df: pd.DataFrame,
    tz: str,
    *,
    group_by: str = "session",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    err = _need(df, ["start_time"])
    if err:
        return {"error": err}
    d: Any = df.copy()
    d["_day"] = to_local_dates(d["start_time"], tz)
    durations: Any = (
        pd.to_numeric(d["duration_seconds"], errors="coerce")
        if "duration_seconds" in d.columns
        else None
    )
    minutes: Any = (
        durations.fillna(0) / 60 if durations is not None else pd.Series(0.0, index=d.index)
    )
    d["_minutes"] = minutes
    total: dict[str, Any] = {
        "count": len(d),
        "total_hours": round(float(minutes.sum()) / 60, 2),
    }
    if "type" in d.columns:
        counts: Any = d["type"].value_counts()
        total["by_type"] = {str(key): int(value) for key, value in counts.items()}
    if "calories_kcal" in d.columns:
        calories: Any = pd.to_numeric(d["calories_kcal"], errors="coerce")
        total["total_kcal"] = round(float(calories.fillna(0).sum()), 1)
    if group_by == "week":
        grouped: Any = d.groupby(d["_day"] - pd.to_timedelta(d["_day"].dt.weekday, unit="D"))
        weeks = []
        for week_start, group in grouped:
            group_any: Any = group
            entry: dict[str, Any] = {
                "week_start": str(week_start)[:10],
                "count": len(group_any),
                "minutes": round(float(group_any["_minutes"].sum()), 1),
            }
            if "type" in group_any.columns:
                week_counts: Any = group_any["type"].value_counts()
                entry["by_type"] = {str(key): int(value) for key, value in week_counts.items()}
            weeks.append(entry)
        weeks.sort(key=lambda entry: entry["week_start"])
        total["returned"] = len(weeks[offset : offset + limit])
        total["offset"] = offset
        total["has_more"] = offset + limit < len(weeks)
        total["weeks"] = weeks[offset : offset + limit]
        return total
    ordered: Any = d.sort_values("start_time")
    page: Any = ordered.iloc[offset : offset + limit]
    rows = []
    for _, row in page.iterrows():
        rows.append(
            {
                "date": str(row["_day"])[:10],
                "start": str(row.get("start_time")),
                "type": str(row.get("type", "?")),
                "minutes": round(float(row.get("_minutes") or 0), 1),
            }
        )
    total.update(returned=len(rows), offset=offset, has_more=offset + limit < len(ordered))
    total["sessions"] = rows
    return total


def health_score_stats(df: pd.DataFrame) -> dict[str, Any]:
    err = _need(df, ["recorded_at", "category", "value"])
    if err:
        return {"error": err}
    order = "_local_date" if "_local_date" in df.columns else "recorded_at"
    ordered: Any = df.sort_values(order)
    by_category: dict[str, Any] = {}
    for category, group in ordered.groupby("category"):
        values: Any = pd.to_numeric(group["value"], errors="coerce")
        by_category[str(category)] = {
            "avg": round(float(values.mean()), 1),
            "latest": round(float(values.iloc[-1]), 1),
            "count": len(group),
        }
    return {"points": len(df), "by_category": by_category}


def timeseries_stats(df: pd.DataFrame, sample_values: int = 20) -> dict[str, Any]:
    err = _need(df, ["timestamp", "type", "value"])
    if err:
        return {"error": err}
    out: dict[str, Any] = {"total_points": len(df), "by_type": {}}
    for series_type, group in df.groupby("type"):
        ordered: Any = group.sort_values("timestamp")
        numeric: Any = pd.to_numeric(ordered["value"], errors="coerce")
        values: Any = numeric.dropna()
        out["by_type"][str(series_type)] = {
            "count": len(ordered),
            "min": round(float(values.min()), 2) if len(values) else None,
            "max": round(float(values.max()), 2) if len(values) else None,
            "avg": round(float(values.mean()), 2) if len(values) else None,
            "sample_values": [round(float(value), 2) for value in values.head(sample_values)],
        }
    return out


def sleep_training_corr(
    sleep_hours: Mapping[str, float],
    volume_kg: Mapping[str, float],
    lag_days: int,
    period: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Correlate lifting volume on day D with sleep recorded on D + lag_days."""
    pairs: list[tuple[float, float]] = []
    for day, volume in volume_kg.items():
        try:
            target = (parse_date(day) + timedelta(days=int(lag_days))).isoformat()
        except ValueError:
            continue
        if target in sleep_hours:
            pairs.append((float(sleep_hours[target]), float(volume)))
    out: dict[str, Any] = {
        "n": len(pairs),
        "lag_days": int(lag_days),
        "definition": f"lifting volume on D vs sleep on D+{int(lag_days)}",
        "period": dict(period or {}),
    }
    if len(pairs) < 3:
        out["error"] = f"Need >=3 overlapping days, got {len(pairs)}"
        return out
    sleep_values = pd.Series([pair[0] for pair in pairs])
    volume_values = pd.Series([pair[1] for pair in pairs])
    sleep_std: Any = sleep_values.std()
    volume_std: Any = volume_values.std()
    if float(sleep_std) == 0 or float(volume_std) == 0:
        out["error"] = "No variance in the overlapping data"
        return out
    correlation = sleep_values.corr(volume_values)
    if pd.isna(correlation):
        out["error"] = "No variance in the overlapping data"
        return out
    r = float(correlation)
    strength = (
        "negligible"
        if abs(r) < 0.1
        else "weak"
        if abs(r) < 0.3
        else "moderate"
        if abs(r) < 0.5
        else "strong"
        if abs(r) < 0.7
        else "very strong"
    )
    out["r"] = round(r, 3)
    out["interpretation"] = f"{strength} {'positive' if r > 0 else 'negative'} correlation"
    return out


def consistency(total_days: int, logged: Mapping[str, int]) -> dict[str, Any]:
    total = max(int(total_days), 1)
    out: dict[str, Any] = {"days": total_days}
    for name, count in logged.items():
        out[f"{name}_days"] = int(count)
        out[f"{name}_pct"] = round(100 * int(count) / total, 1)
    return out


# -- strong-data shapes ---------------------------------------------------


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _number(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _downsample(rows: list[Any], max_points: int) -> tuple[list[Any], bool]:
    total = len(rows)
    cap = max(1, min(int(max_points), HARD_MAX_POINTS))
    if total <= cap:
        return rows, False
    step = total / cap
    return [rows[int(index * step)] for index in range(cap)], True


def strong_compact(summary: Any, weekly: Any = None) -> dict[str, Any]:
    """A small lifting block for composite overviews."""
    if not isinstance(summary, dict) or "volume_kg" not in summary:
        return {"error": "No lifting data in this period", "sessions": 0}
    sessions = int(summary.get("sessions") or 0)
    weeks = [
        {
            "week_start": item.get("bucket_start"),
            "volume_kg": round(float(item.get("value") or 0), 1),
        }
        for item in _items(weekly)
    ][-12:]
    out: dict[str, Any] = {
        "total_volume_kg": round(float(summary.get("volume_kg") or 0), 1),
        "sessions": sessions,
        "sets": int(summary.get("sets") or 0),
        "hours": round(float(summary.get("hours") or 0), 2),
        "sessions_per_week": summary.get("sessions_per_week"),
        "days_since_last_session": summary.get("days_since_last_session"),
        "weekly_volume": weeks,
    }
    if sessions == 0:
        out["note"] = "no data in this period"
    return out


def strong_summary(
    summary: Any,
    weekly: Any = None,
    exercises: Any = None,
    muscle: Any = None,
) -> dict[str, Any]:
    if not isinstance(summary, dict) or "volume_kg" not in summary:
        return {"error": "No lifting data in this period", "sessions": 0}
    sessions = int(summary.get("sessions") or 0)
    out: dict[str, Any] = {
        "total_volume_kg": round(float(summary.get("volume_kg") or 0), 1),
        "total_volume_t": summary.get("volume_t"),
        "sessions": sessions,
        "sets": int(summary.get("sets") or 0),
        "hours": round(float(summary.get("hours") or 0), 2),
        "sessions_per_week": summary.get("sessions_per_week"),
        "avg_volume_per_session": summary.get("avg_volume_per_session"),
        "avg_session_min": summary.get("avg_session_min"),
        "days_since_last_session": summary.get("days_since_last_session"),
    }
    if sessions == 0:
        out["note"] = "no data in this period"
    out["top_exercises"] = [
        {
            "exercise": item.get("key"),
            "sessions": item.get("sessions"),
            "sets": item.get("sets"),
            "volume_kg": round(float(item.get("volume_kg") or 0), 1),
        }
        for item in _items(exercises)
    ]
    out["muscle_balance"] = [
        {"muscle": item.get("key"), "volume_kg": round(float(item.get("volume_kg") or 0), 1)}
        for item in _items(muscle)
    ]
    weeks = [
        {
            "week_start": item.get("bucket_start"),
            "volume_kg": round(float(item.get("value") or 0), 1),
            "moving_avg_4w": item.get("value_4wma"),
        }
        for item in _items(weekly)
    ]
    out["weekly_truncated"] = len(weeks) > WEEKS_IN_YEAR
    out["weekly_volume"] = weeks[-WEEKS_IN_YEAR:]
    out["contract_version"] = summary.get("contract_version")
    out["generated_at"] = summary.get("generated_at")
    return out


def strong_series(series: Any, max_points: int) -> dict[str, Any]:
    items = _items(series)
    points = [
        {
            "bucket_start": item.get("bucket_start"),
            "value": round(float(item.get("value") or 0), 2),
            "moving_avg_4w": item.get("value_4wma"),
            "groups": item.get("groups"),
        }
        for item in items
    ]
    total = len(points)
    points, truncated = _downsample(points, max_points)
    return {
        "bucket": (series or {}).get("bucket") if isinstance(series, dict) else None,
        "group_by": (series or {}).get("group_by") if isinstance(series, dict) else None,
        "total_points": total,
        "truncated": truncated,
        "points": points,
    }


def strong_breakdown(breakdown: Any, dimension: str) -> dict[str, Any]:
    items = _items(breakdown)
    volumes = [float(item.get("volume_kg") or 0) for item in items]
    total_volume = sum(volumes)
    rows = []
    for item, volume in zip(items, volumes, strict=False):
        rows.append(
            {
                "key": item.get("key"),
                "sessions": item.get("sessions"),
                "sets": item.get("sets"),
                "volume_kg": round(volume, 1),
                "share_pct": round(100 * volume / total_volume, 1) if total_volume else 0.0,
            }
        )
    return {"dimension": dimension, "count": len(rows), "items": rows}


def strong_sessions(page: Any) -> dict[str, Any]:
    if not isinstance(page, dict):
        return {"error": "No lifting sessions in this period"}
    return {
        "count": page.get("count"),
        "has_more": bool(page.get("has_more")),
        "sessions": [
            {
                "id": item.get("id"),
                "date": item.get("session_date"),
                "program": item.get("program"),
                "workout_name": item.get("workout_name"),
                "start": item.get("start_time"),
                "duration_min": item.get("duration_min"),
                "set_count": item.get("set_count"),
                "volume_kg": round(float(item.get("total_volume_kg") or 0), 1),
            }
            for item in _items(page)
        ],
    }


def strong_session_detail(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {"error": "Session not found"}
    sets = []
    for item in detail.get("sets") or []:
        if not isinstance(item, dict):
            continue
        sets.append(
            {
                "exercise": item.get("exercise"),
                "muscle_group": item.get("muscle_group"),
                "order": item.get("set_order"),
                "weight_kg": item.get("weight_kg"),
                "reps": item.get("reps"),
                "rpe": item.get("rpe"),
                "volume_kg": item.get("volume_kg"),
                "est_1rm_kg": item.get("est_1rm_kg"),
            }
        )
    return {
        "id": detail.get("id"),
        "date": detail.get("session_date"),
        "program": detail.get("program"),
        "workout_name": detail.get("workout_name"),
        "start": detail.get("start_time"),
        "duration_min": detail.get("duration_min"),
        "volume_kg": round(float(detail.get("total_volume_kg") or 0), 1),
        "sets": sets,
    }


RECORD_DEFINITIONS = {
    "est_1rm": (
        "Best estimated 1RM per exercise, using Epley: weight_kg x (1 + reps/30). "
        "There is no 12-rep cap, so high-rep sets are extrapolated and are less reliable."
    ),
    "bodyweight": (
        "Best bodyweight set per exercise, ranked by reps. Bodyweight sets have zero "
        "volume, so they never appear in volume totals; seconds and distance are "
        "reported when the set has them."
    ),
}


def _record_rank(row: Mapping[str, Any]) -> tuple[float, ...]:
    if "est_1rm_kg" in row:
        return (row.get("est_1rm_kg") or 0.0, row.get("weight_kg") or 0.0)
    return (row.get("reps") or 0.0, row.get("seconds") or 0.0, row.get("distance") or 0.0)


def strong_exercise_detail(
    progress: Any,
    records: Any,
    exercise: str,
    max_points: int,
    record_cap: int,
) -> dict[str, Any]:
    points = [
        {
            "date": item.get("date"),
            "sets": int(item.get("sets") or 0),
            "best_est_1rm_kg": _number(item.get("best_est_1rm_kg")),
            "top_weight_kg": _number(item.get("top_weight_kg")),
            "max_reps": item.get("max_reps"),
            "volume_kg": _number(item.get("volume_kg")),
        }
        for item in _items(progress)
    ]
    total_points = len(points)
    out: dict[str, Any] = {"exercise": exercise, "total_points": total_points}
    if not points:
        out["note"] = "no sets logged for this exercise in the period"
    else:
        out["first"] = points[0]
        out["last"] = points[-1]
        out["sets"] = sum(point["sets"] for point in points)
        out["volume_kg"] = round(sum(point["volume_kg"] or 0.0 for point in points), 1)
        estimated = [point for point in points if point["best_est_1rm_kg"] is not None]
        if estimated:
            best = max(estimated, key=lambda point: point["best_est_1rm_kg"] or 0.0)
            out["best_est_1rm"] = {"kg": best["best_est_1rm_kg"], "date": best["date"]}
            first = estimated[0]["best_est_1rm_kg"] or 0.0
            last = estimated[-1]["best_est_1rm_kg"] or 0.0
            if first > 0:
                out["change_pct"] = round(100 * (last - first) / first, 1)
        weighted = [point for point in points if point["top_weight_kg"] is not None]
        if weighted:
            heaviest = max(weighted, key=lambda point: point["top_weight_kg"] or 0.0)
            out["top_weight"] = {"kg": heaviest["top_weight_kg"], "date": heaviest["date"]}
    capped, truncated = _downsample(points, max_points)
    out["truncated"] = truncated
    out["points"] = capped
    record_rows = [
        {
            "reps": item.get("reps"),
            "set_count": item.get("set_count"),
            "top_weight_kg": _number(item.get("top_weight_kg")),
            "best_est_1rm_kg": _number(item.get("best_est_1rm_kg")),
            "volume_kg": _number(item.get("volume_kg")),
            "set_time": item.get("set_time"),
        }
        for item in _items(records)
    ]
    record_rows.sort(key=lambda row: row.get("reps") or 0)
    out["record_rows"] = len(record_rows)
    out["records_truncated"] = len(record_rows) > record_cap
    out["records"] = record_rows[:record_cap]
    out["record_definition"] = (
        "Best set per rep count. top_weight_kg is the heaviest weight at that rep count; "
        "best_est_1rm_kg uses Epley (weight x (1 + reps/30))."
    )
    return out


def strong_records(payload: Any, kind: str, cap: int) -> dict[str, Any]:
    if kind == "bodyweight":
        rows: list[dict[str, Any]] = [
            {
                "exercise": item.get("exercise"),
                "reps": item.get("reps"),
                "seconds": _number(item.get("seconds")),
                "distance": _number(item.get("distance")),
                "set_time": item.get("set_time"),
            }
            for item in _items(payload)
        ]
    else:
        rows = [
            {
                "exercise": item.get("exercise"),
                "est_1rm_kg": _number(item.get("est_1rm_kg")),
                "weight_kg": _number(item.get("weight_kg")),
                "reps": item.get("reps"),
                "set_time": item.get("set_time"),
            }
            for item in _items(payload)
        ]
    rows.sort(key=_record_rank, reverse=True)
    shown = rows[:cap]
    return {
        "kind": kind,
        "total": len(rows),
        "returned": len(shown),
        "truncated": len(rows) > len(shown),
        "records": shown,
        "definition": RECORD_DEFINITIONS.get(kind, ""),
    }


def strong_sets(page: Any) -> dict[str, Any]:
    if not isinstance(page, dict):
        return {"error": "No lifting sets in this period"}
    return {
        "count": page.get("count"),
        "has_more": bool(page.get("has_more")),
        "returned": len(_items(page)),
        "sets": [
            {
                "id": item.get("id"),
                "session_id": item.get("session_id"),
                "date": item.get("session_date"),
                "exercise": item.get("exercise"),
                "muscle": item.get("muscle_group"),
                "set_order": item.get("set_order"),
                "weight_kg": _number(item.get("weight_kg")),
                "reps": item.get("reps"),
                "rpe": _number(item.get("rpe")),
                "volume_kg": _number(item.get("volume_kg")),
                "est_1rm_kg": _number(item.get("est_1rm_kg")),
            }
            for item in _items(page)
        ],
    }


def strong_yearly(yearly: Any, pace: Any, as_of: str) -> dict[str, Any]:
    totals = [
        {
            "year": item.get("year"),
            "sessions": item.get("sessions"),
            "sets": item.get("sets"),
            "volume_kg": _number(item.get("volume_kg")),
            "volume_t": _number(item.get("volume_t"), 2),
            "hours": _number(item.get("hours"), 2),
        }
        for item in _items(yearly)
    ]
    source = pace if isinstance(pace, dict) else {}
    out: dict[str, Any] = {
        "year": source.get("year"),
        "as_of": as_of,
        "totals_by_year": totals,
        "pace": [
            {
                "year": row.get("year"),
                "sessions": row.get("sessions"),
                "volume_kg": _number(row.get("volume_kg")),
                "cumulative_sessions": row.get("cumulative_sessions"),
                "cumulative_volume_kg": _number(row.get("cumulative_volume_kg")),
            }
            for row in source.get("pace") or []
            if isinstance(row, dict)
        ],
    }
    for key in ("ytd", "last_year_to_date", "projected", "needed_per_week"):
        block = source.get(key)
        out[key] = block if isinstance(block, dict) else None
    out["pace_definition"] = (
        "ytd is this year to as_of; last_year_to_date the same window one year back; "
        "projected is the full-year extrapolation; needed_per_week is what the remainder "
        "of the year would have to average to match last year's total."
    )
    return out


def strong_curve(curve: Any, max_points: int) -> dict[str, Any]:
    rows = [
        {
            "year": item.get("year"),
            "date": item.get("date"),
            "day_of_year": item.get("day_of_year"),
            "sessions": item.get("sessions"),
            "cumulative_sessions": item.get("cumulative_sessions"),
            "cumulative_volume_kg": _number(item.get("cumulative_volume_kg")),
        }
        for item in _items(curve)
    ]
    points, truncated = _downsample(rows, max_points)
    return {
        "year": curve.get("year") if isinstance(curve, dict) else None,
        "years": curve.get("years") if isinstance(curve, dict) else None,
        "total_points": len(rows),
        "truncated": truncated,
        "points": points,
    }


# -- profile and events ---------------------------------------------------


def schedule_per_day(schedule: str | None) -> float | None:
    """Interpret a free-text schedule conservatively; unknown text returns None.

    Only patterns whose meaning is unambiguous are accepted. A wrong guess here
    would produce a fake adherence number, which is worse than no number.
    """
    text = " ".join((schedule or "").strip().lower().replace("-", " ").split())
    if not text:
        return None
    if text in {"daily", "every day", "once daily", "1x daily", "1 x daily"}:
        return 1.0
    if text in {"twice daily", "2x daily", "2 x daily", "bid"}:
        return 2.0
    if text in {"weekly", "once weekly", "1x weekly", "1 x weekly"}:
        return 1 / 7
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:per\s*)?(?:day|daily)", text)
    if match:
        return float(match.group(1))
    return None


def medication_adherence(
    planned: Sequence[Mapping[str, Any]],
    doses: Sequence[Mapping[str, Any]],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    start, end = parse_date(start_date), parse_date(end_date)
    days = (end - start).days + 1
    counts: dict[tuple[str, str], int] = {}
    dated: dict[tuple[str, str], set[str]] = {}
    for row in doses:
        substance = str(row.get("substance") or "").strip().casefold()
        kind = str(row.get("kind") or "").strip().lower()
        if not substance:
            continue
        key = (kind, substance)
        counts[key] = counts.get(key, 0) + 1
        dated.setdefault(key, set()).add(str(row.get("date") or ""))
    # One plan row per substance is the normal case, but morning/evening rows for the
    # same substance are legitimate: group them so a logged dose is counted once.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for plan in planned:
        substance = str(plan.get("substance") or "").strip()
        if not substance:
            continue
        kind = str(plan.get("kind") or "medication").strip().lower()
        key = (kind, substance.casefold())
        group = groups.setdefault(
            key,
            {
                "substance": substance,
                "kind": kind,
                "per_day": 0.0,
                "schedules": [],
                "doses": [],
                "units": set(),
                "unparsed": [],
            },
        )
        if plan.get("dose") is not None or plan.get("unit"):
            group["doses"].append({"dose": plan.get("dose"), "unit": plan.get("unit")})
        if plan.get("unit"):
            group["units"].add(str(plan["unit"]))
        per_day = schedule_per_day(plan.get("schedule"))
        if per_day is None:
            group["unparsed"].append(plan.get("schedule"))
            group["schedules"].append(plan.get("schedule"))
            continue
        group["per_day"] += per_day
        group["schedules"].append(plan.get("schedule"))
    items: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    planned_keys: set[tuple[str, str]] = set(groups)
    for key, group in groups.items():
        logged = counts.get(key, 0)
        if group["unparsed"]:
            # A dose cannot be attributed to the scheduled row or the as-needed row,
            # so a mixed group has no honest percentage: report it, exclude it.
            unparsed.append(
                {
                    "substance": group["substance"],
                    "kind": group["kind"],
                    "schedules": group["schedules"],
                    "scheduled_per_day": round(group["per_day"], 3),
                    "expected_scheduled": round(group["per_day"] * days, 1),
                    "logged": logged,
                    "note": (
                        "at least one schedule was not interpreted, so logged doses "
                        "cannot be attributed to scheduled rows; no percentage computed"
                    ),
                }
            )
            continue
        expected = group["per_day"] * days
        missing_days = None
        if group["per_day"] >= 1:
            missing_days = sum(
                1
                for offset in range(days)
                if (start + timedelta(days=offset)).isoformat() not in dated.get(key, set())
            )
        items.append(
            {
                "substance": group["substance"],
                "kind": group["kind"],
                "plan_rows": len(group["schedules"]),
                "schedules": group["schedules"],
                "doses": group["doses"],
                "units": sorted(group["units"]),
                "per_day": round(group["per_day"], 3),
                "expected": round(expected, 1),
                "logged": logged,
                "adherence_pct": round(100 * logged / expected, 1) if expected else None,
                "missing_days": missing_days,
            }
        )
    total_expected = sum(float(item["expected"]) for item in items)
    total_logged = sum(int(item["logged"]) for item in items)
    out: dict[str, Any] = {
        "period": {"start": start_date, "end": end_date, "days": days},
        "n": len(items),
        "items": items,
        "expected": round(total_expected, 1),
        "logged": total_logged,
        "adherence_pct": round(100 * total_logged / total_expected, 1) if total_expected else None,
        "unparsed_schedules": unparsed,
        "unplanned_logged": [
            {"substance": substance, "kind": kind, "logged": count}
            for (kind, substance), count in sorted(counts.items())
            if (kind, substance) not in planned_keys
        ],
        "definition": (
            "Planned doses come from profile.yaml regimen (medication/supplement rows), "
            "grouped by kind and substance so each logged dose is counted once; logged "
            "doses are events.csv rows with kind medication or supplement. Groups with "
            "an uninterpreted schedule are listed in unparsed_schedules and excluded "
            "from the percentage. Symptom and note rows never count as doses."
        ),
    }
    if not items:
        out["interpretation"] = (
            "No scheduled substance could be interpreted, so adherence cannot be "
            "computed; see unparsed_schedules."
        )
        return out
    parts = [
        f"{out['adherence_pct']}% of planned doses logged over {days} days "
        f"(expected {out['expected']:g}, logged {total_logged}) for {len(items)} substance(s)"
    ]
    missing = sum(int(item["missing_days"] or 0) for item in items)
    if missing:
        parts.append(f"{missing} substance-day(s) with no logged dose")
    if unparsed:
        parts.append(f"{len(unparsed)} schedule(s) not interpreted")
    out["interpretation"] = "; ".join(parts) + "."
    return out


# -- coaching inputs (Tier C/D) -------------------------------------------


def body_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        return {"error": "No body data in Open Wearables"}
    slow = payload.get("slow_changing") or payload.get("static") or {}
    averaged = payload.get("averaged") or {}
    latest = payload.get("latest") or {}
    source = payload.get("source") or {}
    return {
        "static": {
            "weight_kg": _number(slow.get("weight_kg"), 2),
            "height_cm": _number(slow.get("height_cm"), 1),
            "body_fat_pct": _number(
                slow.get("body_fat_percent", slow.get("body_fat_percentage")), 1
            ),
            "muscle_mass_kg": _number(slow.get("muscle_mass_kg"), 1),
            "bmi": _number(slow.get("bmi"), 1),
            "age": slow.get("age"),
        },
        "averaged": {
            "period_days": averaged.get("period_days"),
            "period_start": averaged.get("period_start"),
            "period_end": averaged.get("period_end"),
            "resting_heart_rate_bpm": averaged.get("resting_heart_rate_bpm"),
            "avg_hrv_sdnn_ms": _number(averaged.get("avg_hrv_sdnn_ms"), 1),
            "avg_hrv_rmssd_ms": _number(averaged.get("avg_hrv_rmssd_ms"), 1),
        },
        "latest": {
            "body_temperature_celsius": _number(latest.get("body_temperature_celsius"), 2),
            "body_temperature_measured_at": latest.get("body_temperature_measured_at"),
        },
        "source": {"provider": source.get("provider"), "device_name": source.get("device_name")},
        "definition": (
            "static is the most recent value per field; averaged covers "
            "average_period days; latest only appears when measured inside "
            "latest_window_hours. Missing values stay null, never invented."
        ),
    }


def data_sources_summary(
    sources: Any,
    connections: Any,
    counts: Any,
    *,
    max_sources: int = 50,
    max_connections: int = 20,
) -> dict[str, Any]:
    items = (sources or {}).get("items")
    source_items = [row for row in items or [] if isinstance(row, dict)]
    providers: dict[str, int] = {}
    rows = []
    for item in source_items:
        provider = str(item.get("provider") or "unknown")
        providers[provider] = providers.get(provider, 0) + 1
        rows.append(
            {
                "provider": provider,
                "device_name": item.get("device_name"),
                "device_model": item.get("device_model"),
                "device_type": item.get("device_type"),
                "source": item.get("source"),
                "display_name": item.get("display_name"),
            }
        )
    connection_rows = [row for row in connections or [] if isinstance(row, dict)]
    connections_out = [
        {
            "provider": row.get("provider"),
            "status": row.get("status"),
            "last_synced_at": row.get("last_synced_at"),
            "max_historical_days": row.get("max_historical_days"),
            "live_sync_mode": row.get("live_sync_mode"),
            "capabilities": {
                key: row.get(key)
                for key in ("rest_pull", "webhook_stream", "webhook_ping", "webhook_callback")
                if row.get(key) is not None
            },
        }
        for row in connection_rows
    ]
    counts = counts if isinstance(counts, dict) else {}
    series_counts = counts.get("series_type_counts")
    by_provider = counts.get("by_provider")
    ordered_series = (
        sorted(series_counts.items(), key=lambda item: -int(item[1]))[:30]
        if isinstance(series_counts, dict)
        else []
    )
    return {
        "sources": {
            "count": (sources or {}).get("total", len(source_items)),
            "returned": min(len(rows), max_sources),
            "truncated": len(rows) > max_sources,
            "providers": providers,
            "items": rows[:max_sources],
        },
        "connections": {
            "count": len(connections_out),
            "returned": min(len(connections_out), max_connections),
            "truncated": len(connections_out) > max_connections,
            "items": connections_out[:max_connections],
        },
        "counts": {
            "total_data_points": counts.get("total_data_points"),
            "total_workouts": counts.get("total_workouts"),
            "total_sleep_events": counts.get("total_sleep_events"),
            "series_type_counts": {str(key): int(value) for key, value in ordered_series},
            "workout_type_counts": counts.get("workout_type_counts") or {},
            "by_provider": [
                {
                    "provider": row.get("provider"),
                    "data_points": row.get("data_points"),
                    "workout_count": row.get("workout_count"),
                    "sleep_count": row.get("sleep_count"),
                }
                for row in by_provider or []
                if isinstance(row, dict)
            ][:30],
        },
        "definition": (
            "sources are the devices/providers that have written data for this user; "
            "connections are the live provider links and what they support; counts are "
            "all-time unless a range was given."
        ),
    }


def sleep_session_stats(df: pd.DataFrame, tz: str, *, max_sessions: int = 25) -> dict[str, Any]:
    if df is None or len(df) == 0:
        return {"count": 0, "note": "no sleep sessions in this period"}
    err = _need(df, ["start_time", "end_time"])
    if err:
        return {"error": err}
    d: Any = df.copy()
    d["_day"] = to_local_dates(d["start_time"], tz)
    if "sleep_duration_seconds" in d.columns:
        seconds: Any = pd.to_numeric(d["sleep_duration_seconds"], errors="coerce")
        if "duration_seconds" in d.columns:
            fallback: Any = pd.to_numeric(d["duration_seconds"], errors="coerce")
            seconds = seconds.where(seconds.notna(), fallback)
    else:
        seconds = pd.to_numeric(d["duration_seconds"], errors="coerce")
    d["_hours"] = seconds / 3600
    d["_nap"] = d["is_nap"].fillna(False).astype(bool) if "is_nap" in d.columns else False
    efficiency: Any = (
        pd.to_numeric(d["efficiency_percent"], errors="coerce")
        if "efficiency_percent" in d.columns
        else pd.Series(dtype="float64")
    )
    stages = {
        key: safe_mean(d[column])
        for column, key in (
            ("stages.deep_minutes", "avg_deep_min"),
            ("stages.rem_minutes", "avg_rem_min"),
            ("stages.light_minutes", "avg_light_min"),
            ("stages.awake_minutes", "avg_awake_min"),
        )
        if column in d.columns
    }
    providers: dict[str, int] = {}
    if "source.provider" in d.columns:
        for provider, count in d["source.provider"].value_counts().items():
            providers[str(provider)] = int(count)
    ordered: Any = d.sort_values("start_time")
    shown: Any = ordered.tail(max_sessions)
    rows = []
    for _, row in shown.iterrows():
        entry: dict[str, Any] = {
            "date": str(row["_day"])[:10],
            "start": str(row.get("start_time")),
            "end": str(row.get("end_time")),
            "hours": round(float(row.get("_hours") or 0), 2),
            "efficiency_pct": _number(row.get("efficiency_percent"), 1),
            "is_nap": bool(row.get("_nap")),
        }
        if "stages.deep_minutes" in row.index:
            entry["stages"] = {
                "deep_min": _number(row.get("stages.deep_minutes"), 0),
                "rem_min": _number(row.get("stages.rem_minutes"), 0),
                "light_min": _number(row.get("stages.light_minutes"), 0),
                "awake_min": _number(row.get("stages.awake_minutes"), 0),
            }
        rows.append(entry)
    main = int((~d["_nap"]).sum())
    return {
        "count": len(d),
        "main_sessions": main,
        "naps": len(d) - main,
        "nights": int(d["_day"].nunique()),
        "total_hours": round(float(d["_hours"].fillna(0).sum()), 2),
        "avg_hours": round(float(d["_hours"].mean()), 2) if d["_hours"].notna().any() else None,
        "avg_efficiency_pct": safe_mean(efficiency) if len(efficiency) else None,
        "stages": stages,
        "providers": providers,
        "returned": len(rows),
        "truncated": len(d) > len(rows),
        "sessions": rows,
    }


def logging_stats(
    logged: Mapping[str, Any],
    start_date: str,
    end_date: str,
    *,
    missing_cap: int = 31,
) -> dict[str, Any]:
    start, end = parse_date(start_date), parse_date(end_date)
    days = [
        (start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)
    ]
    window = set(days)
    out: dict[str, Any] = {"period": {"start": start_date, "end": end_date, "days": len(days)}}
    for kind, values in logged.items():
        present = {str(value)[:10] for value in values or [] if str(value)[:10] in window}
        missing = [day for day in days if day not in present]
        current = 0
        for day in reversed(days):
            if day not in present:
                break
            current += 1
        best = run = 0
        for day in days:
            run = run + 1 if day in present else 0
            best = max(best, run)
        out[kind] = {
            "days_logged": len(present),
            "pct": round(100 * len(present) / len(days), 1) if days else 0.0,
            "missing_days": len(missing),
            "current_streak": current,
            "best_streak": best,
            "missing_first": missing[:missing_cap],
            "missing_truncated": len(missing) > missing_cap,
        }
    out["definition"] = (
        "A day is logged when at least one record exists for it. Streaks are "
        "consecutive logged calendar days; current counts backwards from the end "
        "date. A workout streak is days with a workout, not a training block."
    )
    return out


def workload(
    series: Any, start_date: str, end_date: str, *, max_weeks: int = 104
) -> dict[str, Any]:
    weeks = [
        {
            "week_start": item.get("bucket_start"),
            "volume_kg": round(float(item.get("value") or 0), 1),
        }
        for item in _items(series)
        if item.get("bucket_start")
    ]
    weeks.sort(key=lambda row: str(row["week_start"]))
    rows = []
    for index, week in enumerate(weeks):
        prior = weeks[max(0, index - 4) : index]
        baseline = (
            round(sum(row["volume_kg"] for row in prior) / len(prior), 1)
            if len(prior) >= 4
            else None
        )
        ratio = round(week["volume_kg"] / baseline, 2) if baseline and baseline > 0 else None
        if ratio is None:
            flag = "baseline_incomplete"
        elif ratio > 1.3:
            flag = "spike"
        elif ratio < 0.8:
            flag = "drop"
        else:
            flag = "in_range"
        rows.append(
            {
                "week_start": week["week_start"],
                "volume_kg": week["volume_kg"],
                "baseline_4w": baseline,
                "acwr": ratio,
                "flag": flag,
            }
        )
    start, end = parse_date(start_date), parse_date(end_date)
    shown = []
    for row in rows:
        week_start = parse_date(str(row.get("week_start")))
        week_end = week_start + timedelta(days=6)
        row["complete"] = week_start >= start and week_end <= end
        if not row["complete"]:
            # A partial week cannot be compared with complete weeks.
            row["flag"] = "incomplete"
        if week_end < start or week_start > end:
            continue
        shown.append(row)
    current = shown[-1] if shown else None
    if len(shown) > max_weeks:
        shown = shown[-max_weeks:]
    return {
        "period": {"start": start_date, "end": end_date},
        "weeks": shown,
        "truncated": len(shown) == max_weeks and len(rows) > max_weeks,
        "current": current,
        "definition": (
            "ACWR is this week's volume divided by the mean of the previous four "
            "weeks. Spike > 1.3 and drop < 0.8 are conventional flags, not "
            "diagnoses; weeks before the period are fetched only to form the "
            "baseline, and a flag of baseline_incomplete means fewer than four "
            "prior weeks existed."
        ),
    }


def readiness_inputs(
    days: Sequence[Mapping[str, Any]],
    *,
    max_days: int = 62,
) -> dict[str, Any]:
    rows = list(days)
    shown = rows[-max_days:]
    recovery = [row["recovery_score"] for row in rows if row.get("recovery_score") is not None]
    sleep = [row["sleep_hours"] for row in rows if row.get("sleep_hours") is not None]
    volume = [
        row["prior_day_volume_kg"] for row in rows if row.get("prior_day_volume_kg") is not None
    ]
    return {
        "n": len(rows),
        "averages": {
            "recovery_score": safe_mean(recovery),
            "sleep_hours": safe_mean(sleep),
            "prior_day_volume_kg": safe_mean(volume),
        },
        "days": shown,
        "truncated": len(rows) > len(shown),
        "definition": (
            "Inputs only: recovery score, sleep hours and the lifting volume of the "
            "day before, for each day in the period. No readiness score is "
            "computed, so these numbers have to be shown when commenting."
        ),
    }


def weight_goal_progress(
    points: Sequence[tuple[str, float]],
    target: float,
    *,
    target_source: str,
    deadline: str | None = None,
) -> dict[str, Any]:
    if len(points) < 2:
        return {"error": "Need at least 2 weight points in the period"}
    baseline_day, baseline = points[0]
    current_day, current = points[-1]
    days = max((parse_date(current_day) - parse_date(baseline_day)).days, 1)
    rate = (current - baseline) / days * 7
    remaining = current - target
    distance = baseline - target
    progress = None if distance == 0 else round(100 * (baseline - current) / distance, 1)
    projected = None
    if rate != 0 and (remaining > 0) == (rate < 0):
        projected = (
            parse_date(current_day) + timedelta(days=abs(remaining / rate) * 7)
        ).isoformat()
    on_track = bool(projected <= deadline) if deadline and projected else None
    return {
        "metric": "weight",
        "baseline": {"date": baseline_day, "kg": round(baseline, 2)},
        "current": {"date": current_day, "kg": round(current, 2)},
        "target_kg": round(target, 2),
        "target_source": target_source,
        "remaining_kg": round(remaining, 2),
        "change_kg": round(current - baseline, 2),
        "rate_kg_per_week": round(rate, 3),
        "progress_pct": progress,
        "deadline": deadline,
        "projected_date": projected,
        "on_track": on_track,
        "definition": (
            "progress_pct is the share of the baseline-to-target distance already "
            "covered; projected_date assumes the period's rate continues, which is "
            "rarely linear."
        ),
    }


def workout_goal_progress(
    sessions: int,
    target: float,
    *,
    target_source: str,
    deadline: str | None = None,
) -> dict[str, Any]:
    return {
        "metric": "workouts",
        "sessions": sessions,
        "target": target,
        "target_source": target_source,
        "remaining": max(round(target - sessions, 1), 0.0),
        "progress_pct": round(100 * sessions / target, 1) if target else None,
        "deadline": deadline,
        "definition": (
            "sessions counts every workout in the period; the target comes from the "
            "profile goal for the workouts metric (or an explicit override)."
        ),
    }


def medicine_logs(
    rows: Sequence[Mapping[str, Any]], start_date: str, end_date: str
) -> dict[str, Any]:
    doses: dict[tuple[str, str], dict[str, Any]] = {}
    symptoms: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind in {"medication", "supplement"}:
            key = (kind, str(row.get("substance") or "").strip())
            entry = doses.setdefault(
                key,
                {
                    "substance": key[1],
                    "kind": kind,
                    "count": 0,
                    "dose_total": 0.0,
                    "units": set(),
                    "dates": set(),
                    "times": set(),
                    "reasons": set(),
                },
            )
            entry["count"] += 1
            try:
                entry["dose_total"] += float(row.get("dose") or 0)
            except (TypeError, ValueError):
                pass
            if row.get("unit"):
                entry["units"].add(str(row["unit"]))
            if row.get("date"):
                entry["dates"].add(str(row["date"]))
            if row.get("time"):
                entry["times"].add(str(row["time"]))
            if row.get("reason"):
                entry["reasons"].add(str(row["reason"]))
        elif kind == "symptom":
            symptoms.append(
                {"date": row.get("date"), "time": row.get("time"), "notes": row.get("notes")}
            )
        elif kind == "note":
            notes.append(
                {"date": row.get("date"), "time": row.get("time"), "notes": row.get("notes")}
            )
    dose_rows = [
        {
            "substance": entry["substance"],
            "kind": entry["kind"],
            "count": entry["count"],
            "dose_total": round(entry["dose_total"], 2),
            "units": sorted(entry["units"]),
            "first_date": min(entry["dates"]) if entry["dates"] else None,
            "last_date": max(entry["dates"]) if entry["dates"] else None,
            "days_logged": len(entry["dates"]),
            "times": sorted(entry["times"])[:10],
            "reasons": sorted(entry["reasons"]),
        }
        for entry in doses.values()
    ]
    dose_rows.sort(key=lambda row: (-row["count"], row["substance"]))
    return {
        "period": {"start": start_date, "end": end_date},
        "n": len(rows),
        "doses": dose_rows[:50],
        "doses_truncated": len(dose_rows) > 50,
        "symptoms": symptoms[:20],
        "symptoms_count": len(symptoms),
        "notes": notes[:20],
        "notes_count": len(notes),
        "definition": (
            "Only medication and supplement rows count as doses; symptoms and notes "
            "are listed separately. dose_total sums the recorded doses per substance "
            "and may mix units when the log did."
        ),
    }


def medicine_impact(
    doses: Sequence[Mapping[str, Any]],
    series: Mapping[str, Mapping[str, float]],
    start_date: str,
    end_date: str,
    *,
    min_days: int = 3,
) -> dict[str, Any]:
    exposed: dict[str, set[str]] = {}
    for row in doses:
        substance = str(row.get("substance") or "").strip()
        day = str(row.get("date") or "")
        if substance and start_date <= day <= end_date:
            exposed.setdefault(substance.casefold(), set()).add(day)
    subjects = []
    for key in sorted(exposed):
        days = exposed[key]
        entry: dict[str, Any] = {"substance": key, "days_with": len(days), "metrics": {}}
        for metric, values in series.items():
            paired = {
                str(day): float(value)
                for day, value in values.items()
                if start_date <= str(day) <= end_date and value is not None
            }
            with_values = [value for day, value in paired.items() if day in days]
            without_values = [value for day, value in paired.items() if day not in days]
            if len(with_values) < min_days or len(without_values) < min_days:
                entry["metrics"][metric] = {
                    "n_with": len(with_values),
                    "n_without": len(without_values),
                    "status": "insufficient_days",
                }
                continue
            with_mean = round(sum(with_values) / len(with_values), 2)
            without_mean = round(sum(without_values) / len(without_values), 2)
            entry["metrics"][metric] = {
                "n_with": len(with_values),
                "n_without": len(without_values),
                "mean_with": with_mean,
                "mean_without": without_mean,
                "difference": round(with_mean - without_mean, 2),
                "status": "compared",
            }
        subjects.append(entry)
    return {
        "period": {"start": start_date, "end": end_date},
        "n_substances": len(subjects),
        "subjects": subjects[:25],
        "subjects_truncated": len(subjects) > 25,
        "definition": (
            "Same-day comparison: mean metric on days with a dose of the substance "
            "vs days without. At least 3 days on each side, no causality, and a "
            "dose logged after the measurement still counts for that day."
        ),
    }
