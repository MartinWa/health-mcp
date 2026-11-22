"""Open Wearables source: sleep, weight, activity, recovery, workouts, scores."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, ClassVar, Literal

import pandas as pd
from pydantic import Field

from .. import analysis as A
from ..client import OpenWearablesClient, Page
from ..config import Settings
from ..store import CacheStore
from ..utils import DATE_FMT, filter_range, to_local_dates, validate_range
from .base import AGENTS_ALL, DateStr, MaxPoints, PageLimit, SeriesFlag

NAME = "openwearables"
CACHE_NAMESPACE = "openwearables"

ScoreCategory = Literal["sleep", "recovery", "readiness", "activity", "stress", "strain"]
TsType = Literal[
    "heart_rate",
    "steps",
    "weight",
    "sleep",
    "hrv",
    "spo2",
    "calories",
    "distance",
    "respiratory_rate",
    "temperature",
]
Resolution = Literal["raw", "1min", "5min", "15min", "1hour", "1day", "1week", "1month"]

EXPOSURE: dict[str, frozenset[str]] = {
    "get_weight_trend": AGENTS_ALL,
    "get_sleep_summary": AGENTS_ALL,
    "get_activity_summary": AGENTS_ALL,
    "get_recovery_summary": AGENTS_ALL,
    "get_workouts": AGENTS_ALL,
    "get_health_scores": AGENTS_ALL,
    "get_timeseries": AGENTS_ALL,
}


def configured(settings: Settings) -> bool:
    return bool(settings.ow_api_key and settings.ow_user_id and settings.ow_base_url)


class OpenWearablesSource:
    NAME: ClassVar[str] = NAME
    cache_namespace: ClassVar[str] = CACHE_NAMESPACE

    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        client: OpenWearablesClient | None = None,
    ):
        self.settings = settings
        self.cache = cache
        self.tz = settings.tz
        self.user_id = settings.ow_user_id or ""
        self._client = client or OpenWearablesClient(
            settings.ow_base_url or "",
            settings.ow_api_key.get_secret_value() if settings.ow_api_key else "",
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def refresh(self) -> int:
        return self.cache.refresh(self.cache_namespace)

    def _error(self, exc: BaseException) -> dict[str, Any]:
        return {
            "error": str(exc),
            "source": self.NAME,
            "cache_age_hours": self.cache.namespace_age_hours(self.cache_namespace),
        }

    async def status(self) -> dict[str, Any]:
        started = time.perf_counter()
        out: dict[str, Any] = {
            "source": self.NAME,
            "base_url": self._client.base_url,
            "cache_age_hours": self.cache.namespace_age_hours(self.cache_namespace),
        }
        try:
            user = await self._client.user(self.user_id)
        except Exception as exc:
            out.update(
                ok=False, error=str(exc), latency_ms=round((time.perf_counter() - started) * 1000)
            )
            return out
        out.update(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000),
            user={"id": user.get("id"), "username": user.get("username")},
        )
        return out

    # -- cached frames ----------------------------------------------------
    async def _cached_df(
        self,
        name: str,
        start: str,
        end: str,
        col: str,
        fetcher: Any,
    ) -> tuple[pd.DataFrame, bool]:
        key = f"{name}-{start}_{end}"
        cached = self.cache.read_parquet(self.cache_namespace, key)
        if cached is not None:
            return filter_range(cached, col, start, end, self.tz), False
        page: Page = await fetcher(start, end)
        frame = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
        frame = filter_range(frame, col, start, end, self.tz) if len(frame) else frame
        if len(frame) and not page.truncated:
            self.cache.write_parquet(self.cache_namespace, key, frame)
        return frame, page.truncated

    async def _sleep_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        return await self._cached_df(
            "sleep",
            start,
            end,
            "date",
            lambda a, b: self._client.sleep(self.user_id, a, b),
        )

    async def _activity_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        return await self._cached_df(
            "activity",
            start,
            end,
            "date",
            lambda a, b: self._client.activity(self.user_id, a, b),
        )

    async def _recovery_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        return await self._cached_df(
            "recovery",
            start,
            end,
            "date",
            lambda a, b: self._client.recovery(self.user_id, a, b),
        )

    async def _workouts_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        return await self._cached_df(
            "workouts",
            start,
            end,
            "start_time",
            lambda a, b: self._client.workouts(self.user_id, a, b),
        )

    async def _weight_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        key = f"weight-{start}_{end}"
        cached = self.cache.read_parquet(self.cache_namespace, key)
        if cached is not None:
            return cached, False
        page = await self._client.timeseries(self.user_id, start, end, ["weight"], "raw")
        raw: Any = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
        if len(raw) and "type" in raw.columns:
            raw = raw[raw["type"] == "weight"].copy()
            raw["date"] = to_local_dates(raw["timestamp"], self.tz).dt.strftime(DATE_FMT)
            raw = raw.rename(columns={"value": "weight_kg"})
            raw = raw[["date", "weight_kg"]]
            raw = filter_range(raw, "date", start, end, self.tz)
            if not page.truncated:
                self.cache.write_parquet(self.cache_namespace, key, raw)
            return raw, page.truncated
        return pd.DataFrame(columns=["date", "weight_kg"]), page.truncated

    async def _health_scores_df(
        self, start: str, end: str, category: str | None
    ) -> tuple[pd.DataFrame, bool]:
        key = f"health-scores-{start}_{end}-{category or 'all'}"
        cached = self.cache.read_parquet(self.cache_namespace, key)
        if cached is not None:
            return filter_range(cached, "recorded_at", start, end, self.tz), False
        page = await self._client.health_scores(self.user_id, start, end, category)
        frame = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
        if len(frame) and "recorded_at" in frame.columns:
            frame = filter_range(frame, "recorded_at", start, end, self.tz)
            if not page.truncated:
                self.cache.write_parquet(self.cache_namespace, key, frame)
        return frame, page.truncated

    # -- high-level blocks used by composites ----------------------------
    async def overview(self, start: str, end: str) -> dict[str, Any]:
        results = await asyncio.gather(
            self._weight_df(start, end),
            self._sleep_df(start, end),
            self._activity_df(start, end),
            self._recovery_df(start, end),
            self._workouts_df(start, end),
            return_exceptions=True,
        )
        errors = [str(item) for item in results if isinstance(item, BaseException)]
        frames: list[pd.DataFrame] = []
        for item in results:
            if isinstance(item, BaseException):
                frames.append(pd.DataFrame())
            else:
                frames.append(item[0])
        weight_df, sleep_df, activity_df, recovery_df, workouts_df = frames
        start_day, end_day = validate_range(start, end)
        days = (end_day - start_day).days + 1
        out: dict[str, Any] = {
            "weight": A.attach_series(A.weight_stats(weight_df), False, 0),
            "sleep": A.attach_series(A.sleep_stats(sleep_df), False, 0),
            "activity": A.attach_series(A.activity_stats(activity_df), False, 0),
            "recovery": A.attach_series(A.recovery_stats(recovery_df), False, 0),
            "workouts": A.workout_stats(workouts_df, self.tz),
            "logging": A.consistency(
                days,
                {
                    "weight": _logged_days(weight_df, "date"),
                    "sleep": _logged_days(sleep_df, "date"),
                    "workout": _logged_days(workouts_df, "start_time"),
                },
            ),
        }
        for block in ("weight", "sleep", "activity", "recovery"):
            out[block].pop("series_included", None)
            out[block].pop("total_points", None)
        if errors:
            out["error"] = "; ".join(errors)
        return out

    async def daily(self, day: str) -> dict[str, Any]:
        results = await asyncio.gather(
            self._weight_df(day, day),
            self._sleep_df(day, day),
            self._workouts_df(day, day),
            self._recovery_df(day, day),
            return_exceptions=True,
        )
        out: dict[str, Any] = {"date": day}
        weight, sleep, workouts, recovery = [
            pd.DataFrame() if isinstance(item, BaseException) else item[0] for item in results
        ]
        errors = [str(item) for item in results if isinstance(item, BaseException)]
        weights: Any = weight.get("weight_kg")
        out["weight_kg"] = (
            round(float(weights.iloc[-1]), 2) if weights is not None and len(weights) else None
        )
        durations: Any = sleep.get("duration_minutes")
        out["sleep_hours"] = (
            round(float(durations.iloc[0]) / 60, 2)
            if durations is not None and len(durations)
            else None
        )
        seconds: Any = workouts.get("duration_seconds")
        numbers: Any = pd.to_numeric(seconds, errors="coerce")
        out["workout_minutes"] = (
            round(float(numbers.fillna(0).sum()) / 60, 1)
            if seconds is not None and len(seconds)
            else 0.0
        )
        scores: Any = recovery.get("recovery_score")
        out["recovery_score"] = int(scores.iloc[-1]) if scores is not None and len(scores) else None
        if errors:
            out["error"] = "; ".join(errors)
        return out

    async def sleep_hours_by_date(self, start: str, end: str) -> dict[str, float]:
        frame, _ = await self._sleep_df(start, end)
        durations: Any = frame.get("duration_minutes")
        if durations is None:
            return {}
        dates: Any = frame.get("date")
        return {
            str(date)[:10]: round(float(minutes) / 60, 2)
            for date, minutes in zip(dates.tolist(), durations.tolist(), strict=False)
            if pd.notna(minutes)
        }

    # -- tools ------------------------------------------------------------
    def tools(self) -> list[Any]:
        return [
            self.get_weight_trend,
            self.get_sleep_summary,
            self.get_activity_summary,
            self.get_recovery_summary,
            self.get_workouts,
            self.get_health_scores,
            self.get_timeseries,
        ]

    async def get_weight_trend(
        self,
        start_date: DateStr,
        end_date: DateStr,
        include_series: SeriesFlag = False,
        max_points: MaxPoints = None,
    ) -> dict[str, Any]:
        """Weight trend and rate of change. Series = weight samples (kg)."""
        try:
            validate_range(start_date, end_date)
            frame, _ = await self._weight_df(start_date, end_date)
            stats = A.weight_stats(frame)
            out = A.attach_series(
                stats, include_series, max_points or self.settings.default_max_points
            )
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_sleep_summary(
        self,
        start_date: DateStr,
        end_date: DateStr,
        include_series: SeriesFlag = False,
        max_points: MaxPoints = None,
    ) -> dict[str, Any]:
        """Sleep duration, efficiency and stage averages. Series = nightly hours."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._sleep_df(start_date, end_date)
            stats = A.attach_series(
                A.sleep_stats(frame),
                include_series,
                max_points or self.settings.default_max_points,
                value_key="hours",
            )
            stats["period"] = {"start": start_date, "end": end_date}
            stats["fetch_truncated"] = truncated
            return stats
        except Exception as exc:
            return self._error(exc)

    async def get_activity_summary(
        self,
        start_date: DateStr,
        end_date: DateStr,
        include_series: SeriesFlag = False,
        max_points: MaxPoints = None,
    ) -> dict[str, Any]:
        """Daily steps, calories and active minutes. Series = steps per day."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._activity_df(start_date, end_date)
            stats = A.attach_series(
                A.activity_stats(frame),
                include_series,
                max_points or self.settings.default_max_points,
                value_key="steps",
            )
            stats["period"] = {"start": start_date, "end": end_date}
            stats["fetch_truncated"] = truncated
            return stats
        except Exception as exc:
            return self._error(exc)

    async def get_recovery_summary(
        self,
        start_date: DateStr,
        end_date: DateStr,
        include_series: SeriesFlag = False,
        max_points: MaxPoints = None,
    ) -> dict[str, Any]:
        """Recovery score, resting HR, HRV and SpO2. Series = recovery score per day."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._recovery_df(start_date, end_date)
            stats = A.attach_series(
                A.recovery_stats(frame),
                include_series,
                max_points or self.settings.default_max_points,
                value_key="scores",
            )
            stats["period"] = {"start": start_date, "end": end_date}
            stats["fetch_truncated"] = truncated
            return stats
        except Exception as exc:
            return self._error(exc)

    async def get_workouts(
        self,
        start_date: DateStr,
        end_date: DateStr,
        group_by: Annotated[
            Literal["session", "week"], Field(description="Return sessions or weekly buckets")
        ] = "session",
        limit: PageLimit = None,
        offset: Annotated[int, Field(description="Skip first N rows", ge=0)] = 0,
    ) -> dict[str, Any]:
        """Paginated workout sessions, or weekly buckets with group_by='week'."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._workouts_df(start_date, end_date)
            stats = A.workout_stats(
                frame,
                self.tz,
                group_by=group_by,
                limit=limit or self.settings.default_page_limit,
                offset=offset,
            )
            stats["period"] = {"start": start_date, "end": end_date}
            stats["fetch_truncated"] = truncated
            return stats
        except Exception as exc:
            return self._error(exc)

    async def get_health_scores(
        self,
        start_date: DateStr,
        end_date: DateStr,
        category: Annotated[
            ScoreCategory | None, Field(description="Filter to one score category")
        ] = None,
    ) -> dict[str, Any]:
        """Health scores grouped by category (average + latest). Omit category for all."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._health_scores_df(start_date, end_date, category)
            stats = A.health_score_stats(frame)
            stats["period"] = {"start": start_date, "end": end_date}
            stats["fetch_truncated"] = truncated
            return stats
        except Exception as exc:
            return self._error(exc)

    async def get_timeseries(
        self,
        types: Annotated[
            list[TsType],
            Field(
                description="Series types, e.g. ['heart_rate','steps']",
                min_length=1,
                max_length=10,
            ),
        ],
        start_time: Annotated[
            str, Field(description="ISO start, e.g. 2026-09-01 or 2026-09-01T00:00:00")
        ],
        end_time: Annotated[str, Field(description="ISO end")],
        resolution: Annotated[
            Resolution,
            Field(
                description=(
                    "Granularity. Provider day values (resting HR, nightly HRV, SpO2, "
                    "respiratory rate, VO2 max, skin temperature, daily steps/energy) exist "
                    "only at '1day' or coarser and are empty at 'raw' or minute resolutions."
                )
            ),
        ] = "raw",
    ) -> dict[str, Any]:
        """Granular biometric series. Prefer 1day/1hour to bound tokens; fetch is capped."""
        try:
            page = await self._client.timeseries(
                self.user_id, start_time, end_time, list(types), resolution
            )
            frame = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
            stats = A.timeseries_stats(frame)
            stats.update(
                period={"start_time": start_time, "end_time": end_time},
                resolution=resolution,
                fetch_truncated=page.truncated,
                pages=page.pages,
            )
            return stats
        except Exception as exc:
            return self._error(exc)


def _logged_days(frame: pd.DataFrame, column: str) -> int:
    if frame is None or len(frame) == 0 or column not in frame.columns:
        return 0
    try:
        if "_local_date" in frame.columns:
            unique: Any = frame["_local_date"].nunique()
            return int(unique)
        unique = to_local_dates(frame[column]).nunique()
        return int(unique)
    except Exception:
        return 0


def build(settings: Settings, cache: CacheStore) -> OpenWearablesSource:
    return OpenWearablesSource(settings, cache)
