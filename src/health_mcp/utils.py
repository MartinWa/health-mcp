"""Single date/series validation path for all tools.

Every date that crosses the tool boundary is a local calendar date (`TZ`).
Date-only values are used verbatim; timestamps are converted to the local zone
*before* truncation, never formatted from UTC.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

DATE_FMT = "%Y-%m-%d"
HARD_MAX_POINTS = 1000
MAX_RANGE_DAYS = 1830
DEFAULT_TZ = "Europe/Stockholm"
_DATE_ONLY = r"\d{4}-\d{2}-\d{2}"
_STRICT_DATE = re.compile(rf"^{_DATE_ONLY}$")


def parse_date(value: str) -> date:
    text = str(value).strip()
    if not _STRICT_DATE.match(text):
        raise ValueError(f"Invalid date {value!r}: expected YYYY-MM-DD")
    try:
        return datetime.strptime(text, DATE_FMT).date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}: expected YYYY-MM-DD") from exc


def validate_range(start_date: str, end_date: str) -> tuple[date, date]:
    start, end = parse_date(start_date), parse_date(end_date)
    if start > end:
        raise ValueError(f"start_date {start_date} is after end_date {end_date}")
    span_days = (end - start).days + 1
    if span_days > MAX_RANGE_DAYS:
        raise ValueError(
            f"Date range too large: {start_date}..{end_date} spans {span_days} days "
            f"(max {MAX_RANGE_DAYS})"
        )
    return start, end


def tzinfo(tz: str | None = None) -> ZoneInfo:
    name = tz or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ValueError(f"Invalid timezone {name!r}") from exc


def today_local(tz: str | None = None) -> str:
    return datetime.now(tzinfo(tz)).date().isoformat()


def to_local_dates(values: Any, tz: str | None = None) -> pd.Series:
    """Normalise a column to tz-naive local midnight timestamps.

    Date-only strings are taken as dates; anything else is treated as a
    timestamp and converted to `tz` before the time part is dropped.
    """
    series: Any = values if isinstance(values, pd.Series) else pd.Series(values)
    if len(series) == 0:
        return pd.Series([], dtype="datetime64[ns]")
    text: Any = series.dropna().astype("string")
    date_only = bool(text.str.fullmatch(_DATE_ONLY).all()) if len(text) else True
    if date_only:
        parsed: Any = pd.to_datetime(series.astype("string"), format=DATE_FMT, errors="coerce")
        return parsed.dt.normalize()
    stamps: Any = pd.to_datetime(series, utc=True, errors="coerce")
    return stamps.dt.tz_convert(tzinfo(tz)).dt.tz_localize(None).dt.normalize()


def filter_range(
    df: pd.DataFrame,
    col: str,
    start_date: str,
    end_date: str,
    tz: str | None = None,
) -> pd.DataFrame:
    """Inclusive local-date filter. Returns an empty frame (same cols) on no match."""
    if df is None or len(df) == 0 or col not in df.columns:
        return df if df is not None else pd.DataFrame()
    start, end = validate_range(start_date, end_date)
    out = df.copy()
    dates = to_local_dates(out[col], tz)
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    filtered = out.loc[mask.to_numpy()].copy()
    filtered["_local_date"] = dates.loc[mask.to_numpy()]
    return filtered.sort_values(col).reset_index(drop=True)


def truncate_series(
    dates: list[Any], values: list[Any], max_points: int
) -> tuple[list[Any], list[Any], int, bool]:
    """Cap a series for token safety, keeping the trend shape. Evenly downsamples."""
    total = len(dates)
    cap = max(1, min(int(max_points), HARD_MAX_POINTS))
    if total <= cap:
        return dates, values, total, False
    step = total / cap
    idx = [int(i * step) for i in range(cap)]
    return [dates[i] for i in idx], [values[i] for i in idx], total, True


def safe_mean(values: Any) -> float | None:
    series: Any = pd.Series(values)
    numeric: Any = pd.to_numeric(series, errors="coerce")
    clean: Any = numeric.dropna()
    return round(float(clean.mean()), 2) if len(clean) else None


def event_days(
    day: str,
    end_day: str | None,
    every_days: int | None,
    start_date: str,
    end_date: str,
) -> list[str]:
    """The local days one event row covers, clipped to the queried range.

    A row with no `end_day` and no `every_days` covers its own date. With an
    `end_day` it covers every `every_days`-th day from its first date to that day
    inclusive, so a daily medication is one row rather than one row per day. An
    `every_days` without an `end_day` is an ongoing series: it repeats through the
    end of the queried range. The step is anchored to the row's own first date, so
    clipping the start of the range never shifts the grid.
    """
    try:
        first = parse_date(day)
    except ValueError:
        return []
    last: date | None
    if end_day:
        try:
            last = max(parse_date(end_day), first)
        except ValueError:
            last = first
    elif every_days:
        last = None
    else:
        last = first
    step = max(int(every_days or 1), 1)
    low, high = parse_date(start_date), parse_date(end_date)
    stop = high if last is None else min(last, high)
    if first > high or (last is not None and last < low):
        return []
    offset = max((low - first).days, 0)
    current = first + timedelta(days=offset - offset % step)
    out: list[str] = []
    while current <= stop:
        if current >= low:
            out.append(current.isoformat())
        current += timedelta(days=step)
    return out
