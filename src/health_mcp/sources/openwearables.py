"""Open Wearables source: sleep, weight, activity, recovery, workouts, scores."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Literal

import pandas as pd
from pydantic import Field

from .. import analysis as A
from ..client import OpenWearablesClient, Page
from ..config import Settings
from ..store import CacheStore
from ..utils import DATE_FMT, filter_range, to_local_dates, today_local, validate_range
from .base import AGENTS_ALL, DateStr, MaxPoints, PageLimit, SeriesFlag

NAME = "openwearables"
CACHE_NAMESPACE = "openwearables"

ScoreCategory = Literal["sleep", "recovery", "readiness", "activity", "stress", "strain"]
TsType = Literal[
    "heart_rate",
    "steps",
    "weight",
    "hrv",
    "spo2",
    "calories",
    "distance",
    "respiratory_rate",
    "temperature",
]
# Friendly tool names -> Open Wearables `SeriesType` values (from the live
# /openapi.json, 141 entries). `sleep` is deliberately absent: OW serves sleep
# through /summaries/sleep, so it is not a timeseries type at all.
TS_ALIASES: dict[TsType, str] = {
    "heart_rate": "heart_rate",
    "steps": "steps",
    "weight": "weight",
    "hrv": "heart_rate_variability_rmssd",
    "spo2": "oxygen_saturation",
    "calories": "active_energy",
    "distance": "distance",
    "respiratory_rate": "respiratory_rate",
    "temperature": "body_temperature",
}
Resolution = Literal["raw", "1min", "5min", "15min", "1hour", "1day", "1week", "1month"]

EXPOSURE: dict[str, frozenset[str]] = {
    "get_weight_trend": AGENTS_ALL,
    "get_sleep_summary": AGENTS_ALL,
    "get_activity_summary": AGENTS_ALL,
    "get_recovery_summary": AGENTS_ALL,
    "get_workouts": AGENTS_ALL,
    "get_health_scores": AGENTS_ALL,
    "get_timeseries": AGENTS_ALL,
    "get_body": frozenset({"health-coach"}),
    "get_data_sources": frozenset({"health-coach"}),
    "get_sleep_sessions": frozenset({"health-coach"}),
    "get_consistency": frozenset({"health-coach"}),
}


def configured(settings: Settings) -> bool:
    return bool(settings.ow_api_key and settings.ow_user_id and settings.ow_base_url)


def skip_reason(settings: Settings) -> str:
    return "OW_API_KEY, OW_USER_ID and OW_BASE_URL must all be set"


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
        if isinstance(exc, ValueError):
            return {
                "error": str(exc),
                "source": "local",
                "hint": "rejected by local validation; no upstream call was made",
            }
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
            "cache_empty": self.cache.namespace_age_hours(self.cache_namespace) is None,
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

    async def _workouts_df(
        self, start: str, end: str, types: Sequence[str] | None = None
    ) -> tuple[pd.DataFrame, bool]:
        frame, truncated = await self._cached_df(
            "workouts",
            start,
            end,
            "start_time",
            lambda a, b: self._client.workouts(self.user_id, a, b),
        )
        frame_any: Any = frame
        if not types or frame_any is None or "type" not in frame_any.columns:
            return frame, truncated
        wanted = {str(item).strip().lower() for item in types if str(item).strip()}
        filtered: pd.DataFrame = frame_any[
            frame_any["type"].astype(str).str.lower().isin(wanted)
        ].copy()
        return filtered, truncated

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
        errors = {
            name: str(item)
            for name, item in zip(
                ("weight", "sleep", "activity", "recovery", "workouts"), results, strict=True
            )
            if isinstance(item, BaseException)
        }
        truncated = {
            name: False if isinstance(item, BaseException) else bool(item[1])
            for name, item in zip(
                ("weight", "sleep", "activity", "recovery", "workouts"), results, strict=True
            )
        }
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
        out["fetch_truncated"] = truncated
        capped = [name for name in ("workouts",) if out[name].get("has_more")]
        if capped:
            out["capped"] = capped
            out["capped_note"] = (
                "the block lists a capped page; returned/has_more say how much is missing, "
                "while the block's own count covers the whole period"
            )
        for block, message in errors.items():
            out[block]["error"] = message
        if errors:
            out["error"] = "; ".join(errors.values())
            out["failed_blocks"] = list(errors)
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
        errors = {
            name: str(item)
            for name, item in zip(("weight", "sleep", "workouts", "recovery"), results, strict=True)
            if isinstance(item, BaseException)
        }
        truncated = {
            name: False if isinstance(item, BaseException) else bool(item[1])
            for name, item in zip(("weight", "sleep", "workouts", "recovery"), results, strict=True)
        }
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
        out["fetch_truncated"] = truncated
        if errors:
            out["error"] = "; ".join(errors.values())
            out["failed_blocks"] = list(errors)
        return out

    async def sleep_hours_by_date(self, start: str, end: str) -> dict[str, Any]:
        frame, truncated = await self._sleep_df(start, end)
        return {"hours": _sleep_hours(frame), "fetch_truncated": truncated}

    async def person(self) -> dict[str, Any]:
        """Identity and static body data Open Wearables owns; the profile never duplicates it."""
        try:
            record, static = await asyncio.gather(
                self._client.personal_record(self.user_id),
                self._client.body_static(self.user_id),
            )
        except Exception as exc:
            return {
                "available": False,
                "error": str(exc),
                "source": self.NAME,
                "cache_age_hours": self.cache.namespace_age_hours(self.cache_namespace),
            }
        record = record if isinstance(record, dict) else {}
        static = static if isinstance(static, dict) else {}
        out: dict[str, Any] = {
            "available": True,
            "source": self.NAME,
            "birth_date": record.get("birth_date"),
            "sex": record.get("sex"),
            "gender": record.get("gender"),
            "height_cm": _pick(static, "height_cm", "height"),
            "body_fat_pct": _pick(
                static, "body_fat_percent", "body_fat_percentage", "body_fat_pct", "body_fat"
            ),
            "muscle_mass_kg": _pick(static, "muscle_mass_kg", "muscle_mass"),
            "bmi": _pick(static, "bmi"),
            "age": _pick(static, "age"),
        }
        missing = [
            name for name in ("birth_date", "sex", "gender", "height_cm") if out[name] is None
        ]
        out["missing"] = missing
        if missing:
            out["note"] = (
                f"Set {', '.join(missing)} in Open Wearables; profile.yaml does not duplicate "
                "fields another service owns."
            )
        return out

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
            self.get_body,
            self.get_data_sources,
            self.get_sleep_sessions,
            self.get_consistency,
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
                description=(
                    "Series types: heart_rate, steps, weight, "
                    "hrv (-> heart_rate_variability_rmssd), "
                    "spo2 (-> oxygen_saturation), calories (-> active_energy), distance, "
                    "respiratory_rate, temperature (-> body_temperature). Sleep is not a "
                    "timeseries type; use get_sleep_summary or get_sleep_sessions."
                ),
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
            resolved = [TS_ALIASES[item] for item in types]
            page = await self._client.timeseries(
                self.user_id, start_time, end_time, resolved, resolution
            )
            frame = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
            stats = A.timeseries_stats(frame)
            stats.update(
                period={"start_time": start_time, "end_time": end_time},
                resolution=resolution,
                fetch_truncated=page.truncated,
                pages=page.pages,
                types_requested=list(types),
                types_resolved=resolved,
            )
            return stats
        except Exception as exc:
            return self._error(exc)

    # -- Tier C detail tools ----------------------------------------------
    async def _sleep_sessions_df(self, start: str, end: str) -> tuple[pd.DataFrame, bool]:
        key = f"sleep-sessions-{start}_{end}"
        cached = self.cache.read_parquet(self.cache_namespace, key)
        if cached is not None:
            return filter_range(cached, "start_time", start, end, self.tz), False
        page = await self._client.sleep_sessions(self.user_id, start, end)
        frame = pd.json_normalize(page.rows) if page.rows else pd.DataFrame()
        if len(frame) and "start_time" in frame.columns:
            frame = filter_range(frame, "start_time", start, end, self.tz)
            if not page.truncated:
                self.cache.write_parquet(self.cache_namespace, key, frame)
        return frame, page.truncated

    async def get_body(
        self,
        average_period: Annotated[
            int, Field(description="Days to average resting HR and HRV (1-7)", ge=1, le=7)
        ] = 7,
        latest_window_hours: Annotated[
            int,
            Field(description="Only show point readings measured within this window", ge=1, le=24),
        ] = 4,
    ) -> dict[str, Any]:
        """Body composition (latest values) plus averaged vitals and point readings."""
        try:
            key = f"body-{average_period}-{latest_window_hours}"
            cached = self.cache.read_json(self.cache_namespace, key, ttl_hours=1)
            payload = cached.get("payload") if isinstance(cached, dict) else None
            if not isinstance(payload, dict):
                payload = await self._client.body(
                    self.user_id,
                    average_period=average_period,
                    latest_window_hours=latest_window_hours,
                )
                self.cache.write_json(
                    self.cache_namespace,
                    key,
                    {"fetched_at": time.time(), "payload": payload},
                    ttl_hours=1,
                )
            out = A.body_summary(payload)
            out["as_of"] = today_local(self.tz)
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_data_sources(self) -> dict[str, Any]:
        """Devices/sources that wrote data, live provider connections and per-type counts."""
        try:
            results = await asyncio.gather(
                self._client.data_sources(self.user_id),
                self._client.connections(self.user_id),
                self._client.data_summary(self.user_id),
                return_exceptions=True,
            )
            sources = results[0] if not isinstance(results[0], BaseException) else {}
            connections = results[1] if not isinstance(results[1], BaseException) else []
            counts = results[2] if not isinstance(results[2], BaseException) else {}
            out = A.data_sources_summary(sources, connections, counts)
            errors = [str(result) for result in results if isinstance(result, BaseException)]
            if errors:
                out["error"] = "; ".join(errors)
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_sleep_sessions(
        self,
        start_date: DateStr,
        end_date: DateStr,
    ) -> dict[str, Any]:
        """Session-level sleep (naps, efficiency, stages), summarised with at most 25 rows."""
        try:
            validate_range(start_date, end_date)
            frame, truncated = await self._sleep_sessions_df(start_date, end_date)
            out = A.sleep_session_stats(frame, self.tz)
            out["period"] = {"start": start_date, "end": end_date}
            out["fetch_truncated"] = truncated
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_consistency(
        self,
        start_date: DateStr,
        end_date: DateStr,
    ) -> dict[str, Any]:
        """Logging percentages, missing days and streaks for workouts, sleep and weight."""
        try:
            validate_range(start_date, end_date)
            weight_pair, sleep_pair, workouts_pair = await asyncio.gather(
                self._weight_df(start_date, end_date),
                self._sleep_df(start_date, end_date),
                self._workouts_df(start_date, end_date),
            )
            out = A.logging_stats(
                {
                    "workouts": _local_days(workouts_pair[0], "start_time", self.tz),
                    "sleep": _local_days(sleep_pair[0], "date", self.tz),
                    "weight": _local_days(weight_pair[0], "date", self.tz),
                },
                start_date,
                end_date,
            )
            truncated = {
                "weight": weight_pair[1],
                "sleep": sleep_pair[1],
                "workouts": workouts_pair[1],
            }
            out["fetch_truncated"] = truncated
            if any(truncated.values()):
                out["complete"] = False
                out["warning"] = (
                    "At least one source hit the page ceiling, so percentages and "
                    "missing days are computed from a subset of the period."
                )
            return out
        except Exception as exc:
            return self._error(exc)

    # -- inputs for the coaching composites -------------------------------
    async def sleep_hours_with_truncation(
        self, start: str, end: str
    ) -> tuple[dict[str, float], bool]:
        frame, truncated = await self._sleep_df(start, end)
        return _sleep_hours(frame), truncated

    async def weight_points(self, start: str, end: str) -> tuple[list[tuple[str, float]], bool]:
        frame, truncated = await self._weight_df(start, end)
        if frame is None or len(frame) == 0 or "weight_kg" not in frame.columns:
            return [], truncated
        ordered: Any = frame.sort_values("date")
        points = [
            (str(row["date"])[:10], float(row["weight_kg"]))
            for _, row in ordered.iterrows()
            if pd.notna(row["weight_kg"])
        ]
        return points, truncated

    async def workout_count(
        self, start: str, end: str, types: Sequence[str] | None = None
    ) -> tuple[int, bool]:
        frame, truncated = await self._workouts_df(start, end, types)
        return (0 if frame is None else len(frame)), truncated

    async def daily_metrics(
        self, start: str, end: str
    ) -> tuple[dict[str, dict[str, float]], dict[str, bool]]:
        """Per-day recovery score, resting HR and sleep efficiency for the coach composites."""
        recovery, sleep = await asyncio.gather(
            self._recovery_df(start, end), self._sleep_df(start, end)
        )
        metrics = {
            "recovery_score": _by_day(recovery[0], "date", "recovery_score", self.tz),
            "resting_heart_rate_bpm": _by_day(
                recovery[0], "date", "resting_heart_rate_bpm", self.tz
            ),
            "sleep_efficiency_pct": _by_day(sleep[0], "date", "efficiency_percent", self.tz),
            "sleep_hours": _by_day(sleep[0], "date", "duration_minutes", self.tz, scale=1 / 60),
        }
        return metrics, {"recovery": recovery[1], "sleep": sleep[1]}

    async def recovery_days(self, start: str, end: str) -> tuple[dict[str, dict[str, Any]], bool]:
        frame, truncated = await self._recovery_df(start, end)
        scores = _by_day(frame, "date", "recovery_score", self.tz)
        resting = _by_day(frame, "date", "resting_heart_rate_bpm", self.tz)
        days = {
            day: {"recovery_score": scores.get(day), "resting_heart_rate_bpm": resting.get(day)}
            for day in sorted(set(scores) | set(resting))
        }
        return days, truncated


def _pick(block: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = block.get(name)
        if value is not None:
            return value
    return None


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


def _sleep_hours(frame: pd.DataFrame) -> dict[str, float]:
    if frame is None or len(frame) == 0:
        return {}
    durations: Any = frame.get("duration_minutes")
    if durations is None:
        return {}
    dates: Any = frame.get("date")
    return {
        str(day)[:10]: round(float(minutes) / 60, 2)
        for day, minutes in zip(dates.tolist(), durations.tolist(), strict=False)
        if pd.notna(minutes)
    }


def _local_days(frame: pd.DataFrame, column: str, tz: str) -> list[str]:
    if frame is None or len(frame) == 0 or column not in frame.columns:
        return []
    return [str(value)[:10] for value in to_local_dates(frame[column], tz).tolist()]


def _by_day(
    frame: pd.DataFrame, column: str, value_column: str, tz: str, *, scale: float = 1.0
) -> dict[str, float]:
    if (
        frame is None
        or len(frame) == 0
        or column not in frame.columns
        or value_column not in frame.columns
    ):
        return {}
    days = _local_days(frame, column, tz)
    values: Any = pd.to_numeric(frame[value_column], errors="coerce")
    out: dict[str, float] = {}
    for day, value in zip(days, values.tolist(), strict=False):
        if pd.notna(value):
            out[day] = round(float(value) * scale, 2)
    return out


def build(settings: Settings, cache: CacheStore) -> OpenWearablesSource:
    return OpenWearablesSource(settings, cache)
