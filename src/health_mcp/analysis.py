"""Pure analysis: DataFrame/JSON in, JSON-serializable dict out. No I/O.

Every function is defensive about missing columns (returns ``{"error": ...}``)
instead of raising — a source returning a partial payload must degrade an answer,
not the server.
"""

from __future__ import annotations

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
    cap = max(1, min(int(max_points), HARD_MAX_POINTS))
    truncated = total > cap
    if truncated:
        step = total / cap
        points = [points[int(index * step)] for index in range(cap)]
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
