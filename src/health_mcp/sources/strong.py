"""strong-data source: lifting summaries, trends, breakdowns and sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from .. import analysis as A
from ..config import Settings
from ..store import CacheStore
from ..strong_client import StrongDataClient, StrongDataError
from ..utils import validate_range
from .base import AGENTS_ALL, DateStr, MaxPoints, PageLimit

NAME = "strong"
CACHE_NAMESPACE = "strong"
MAX_EXPANDED_SESSIONS = 10
EXPAND_CONCURRENCY = 4

TrendMetric = Literal["volume", "sessions", "volume_per_session", "training_time"]
Bucket = Literal["day", "week", "month", "year"]
GroupBy = Literal["program", "muscle", "exercise"]
BreakdownDimension = Literal["weekday", "program", "muscle", "exercise"]
SortKey = Literal[
    "date",
    "-date",
    "volume",
    "-volume",
    "sets",
    "-sets",
    "start_time",
    "-start_time",
    "workout_name",
    "-workout_name",
    "program",
    "-program",
]

EXPOSURE: dict[str, frozenset[str]] = {
    "get_lifting_summary": AGENTS_ALL,
    "get_lifting_trend": AGENTS_ALL,
    "get_lifting_breakdown": AGENTS_ALL,
    "get_lifting_sessions": AGENTS_ALL,
}


def configured(settings: Settings) -> bool:
    return bool(settings.strong_api_url and settings.strong_api_token)


class StrongSource:
    NAME: ClassVar[str] = NAME
    cache_namespace: ClassVar[str] = CACHE_NAMESPACE

    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        client: StrongDataClient | None = None,
    ):
        self.settings = settings
        self.cache = cache
        self.tz = settings.tz
        self._client = client or StrongDataClient(
            settings.strong_api_url or "",
            settings.strong_api_token.get_secret_value() if settings.strong_api_token else "",
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

    @staticmethod
    def _key(path: str, params: dict[str, Any]) -> str:
        clean = {key: value for key, value in params.items() if value is not None}
        digest = hashlib.sha256(
            (path + json.dumps(clean, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()[:16]
        slug = path.replace("/", "-")
        return f"{slug}-{digest}"

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        fetcher: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        key = self._key(path, params)
        cached = self.cache.read_json(self.cache_namespace, key)
        if cached is not None and isinstance(cached.get("payload"), dict):
            try:
                self._client.check_envelope(cached["payload"])
            except StrongDataError:
                pass
            else:
                return cached["payload"]
        payload = await fetcher()
        self.cache.write_json(
            self.cache_namespace,
            key,
            {
                "fetched_at": time.time(),
                "contract_version": payload.get("contract_version"),
                "generated_at": payload.get("generated_at"),
                "payload": payload,
            },
        )
        return payload

    async def status(self) -> dict[str, Any]:
        started = time.perf_counter()
        out: dict[str, Any] = {
            "source": self.NAME,
            "base_url": self._client.base_url,
            "cache_age_hours": self.cache.namespace_age_hours(self.cache_namespace),
            "contract_degraded": self._client.contract_degraded,
        }
        try:
            me = await self._client.me()
        except Exception as exc:
            out.update(
                ok=False,
                error=str(exc),
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
            return out
        expected = (self.settings.strong_owner_id or "").strip()
        actual = str(me.get("id"))
        out.update(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000),
            username=me.get("username"),
            owner_id=actual,
            owner_expected=expected or None,
            owner_match=(actual == expected) if expected else None,
            contract_version=me.get("contract_version"),
            contract_warning=self._client.contract_warning,
        )
        return out

    # -- data accessors ---------------------------------------------------
    async def volume_by_date(self, start: str, end: str) -> dict[str, float]:
        series = await self._get(
            "series/volume",
            {"start": start, "end": end, "bucket": "day"},
            lambda: self._client.series("volume", start=start, end=end, bucket="day"),
        )
        return {
            str(item.get("bucket_start")): float(item.get("value") or 0)
            for item in series.get("items") or []
            if isinstance(item, dict) and item.get("bucket_start")
        }

    async def overview(self, start: str, end: str) -> dict[str, Any]:
        summary, weekly = await asyncio.gather(
            self._get(
                "summary",
                {"start": start, "end": end},
                lambda: self._client.summary(start=start, end=end),
            ),
            self._get(
                "series/volume",
                {"start": start, "end": end, "bucket": "week", "moving_average": True},
                lambda: self._client.series(
                    "volume", start=start, end=end, bucket="week", moving_average=True
                ),
            ),
        )
        return A.strong_compact(summary, weekly)

    async def daily(self, day: str) -> dict[str, Any]:
        try:
            summary, page = await asyncio.gather(
                self._get(
                    "summary",
                    {"start": day, "end": day},
                    lambda: self._client.summary(start=day, end=day),
                ),
                self._get(
                    "sessions",
                    {"start": day, "end": day, "limit": 1, "offset": 0, "sort": "-start_time"},
                    lambda: self._client.sessions(
                        start=day, end=day, limit=1, offset=0, sort="-start_time"
                    ),
                ),
            )
        except Exception as exc:
            return self._error(exc)
        sessions = A.strong_sessions(page)["sessions"]
        return {
            "date": day,
            "lifting_volume_kg": round(float(summary.get("volume_kg") or 0), 1),
            "sessions": int(summary.get("sessions") or 0),
            "sets": int(summary.get("sets") or 0),
            "last_session": sessions[0] if sessions else None,
        }

    # -- tools ------------------------------------------------------------
    def tools(self) -> list[Any]:
        return [
            self.get_lifting_summary,
            self.get_lifting_trend,
            self.get_lifting_breakdown,
            self.get_lifting_sessions,
        ]

    async def get_lifting_summary(
        self,
        start_date: DateStr,
        end_date: DateStr,
        top_n: Annotated[int, Field(description="Top exercises to list", ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Lifting totals, top exercises, muscle balance and weekly volume."""
        try:
            validate_range(start_date, end_date)
            params = {"start": start_date, "end": end_date}
            summary, weekly, exercises, muscle = await asyncio.gather(
                self._get(
                    "summary",
                    params,
                    lambda: self._client.summary(start=start_date, end=end_date),
                ),
                self._get(
                    "series/volume",
                    {**params, "bucket": "week", "moving_average": True},
                    lambda: self._client.series(
                        "volume",
                        start=start_date,
                        end=end_date,
                        bucket="week",
                        moving_average=True,
                    ),
                ),
                self._get(
                    "breakdown/exercises",
                    {**params, "limit": top_n},
                    lambda: self._client.breakdown(
                        "exercise", start=start_date, end=end_date, limit=top_n
                    ),
                ),
                self._get(
                    "breakdown/muscle",
                    {**params, "limit": 50},
                    lambda: self._client.breakdown(
                        "muscle", start=start_date, end=end_date, limit=50
                    ),
                ),
            )
            out = A.strong_summary(summary, weekly, exercises, muscle)
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except StrongDataError as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc)

    async def get_lifting_trend(
        self,
        start_date: DateStr,
        end_date: DateStr,
        metric: Annotated[
            TrendMetric,
            Field(description="volume | sessions | volume_per_session | training_time"),
        ] = "volume",
        bucket: Annotated[Bucket, Field(description="Bucket size")] = "week",
        group_by: Annotated[
            GroupBy | None, Field(description="Optional split by program, muscle or exercise")
        ] = None,
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
        max_points: MaxPoints = None,
    ) -> dict[str, Any]:
        """One lifting metric over time. Week buckets also carry a 4-week moving average."""
        try:
            validate_range(start_date, end_date)
            params = {
                "start": start_date,
                "end": end_date,
                "bucket": bucket,
                "group_by": group_by,
                "program": program,
                "moving_average": True if bucket == "week" else None,
            }
            path = f"series/{metric.replace('_', '-')}"
            series = await self._get(
                path,
                params,
                lambda: self._client.series(
                    metric,
                    start=start_date,
                    end=end_date,
                    bucket=bucket,
                    group_by=group_by,
                    moving_average=True if bucket == "week" else None,
                    program=program,
                ),
            )
            out = A.strong_series(series, max_points or self.settings.default_max_points)
            out["metric"] = metric
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_lifting_breakdown(
        self,
        start_date: DateStr,
        end_date: DateStr,
        dimension: Annotated[
            BreakdownDimension,
            Field(description="weekday | program | muscle | exercise"),
        ] = "exercise",
        limit: PageLimit = None,
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
    ) -> dict[str, Any]:
        """Volume and session counts split by weekday, program, muscle or exercise."""
        try:
            validate_range(start_date, end_date)
            count = limit or self.settings.default_page_limit
            path = "breakdown/exercises" if dimension == "exercise" else f"breakdown/{dimension}"
            payload = await self._get(
                path,
                {"start": start_date, "end": end_date, "limit": count, "program": program},
                lambda: self._client.breakdown(
                    dimension, start=start_date, end=end_date, limit=count, program=program
                ),
            )
            out = A.strong_breakdown(payload, dimension)
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_lifting_sessions(
        self,
        start_date: DateStr,
        end_date: DateStr,
        limit: PageLimit = None,
        offset: Annotated[int, Field(description="Skip first N sessions", ge=0)] = 0,
        sort: Annotated[SortKey, Field(description="Sort key, '-' prefix for descending")] = "date",
        include_sets: Annotated[
            bool, Field(description="Expand sets for at most 10 sessions")
        ] = False,
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
        exercise: Annotated[str | None, Field(description="Filter to one exercise")] = None,
        muscle: Annotated[str | None, Field(description="Filter to one muscle group")] = None,
        weighted: Annotated[bool | None, Field(description="Only weighted sets")] = None,
    ) -> dict[str, Any]:
        """Paginated lifting sessions; include_sets expands up to 10 sessions."""
        try:
            validate_range(start_date, end_date)
            count = limit or self.settings.default_page_limit
            params = {
                "start": start_date,
                "end": end_date,
                "program": program,
                "exercise": exercise,
                "muscle": muscle,
                "weighted": weighted,
                "limit": count,
                "offset": offset,
                "sort": sort,
            }
            page = await self._get(
                "sessions",
                params,
                lambda: self._client.sessions(
                    start=start_date,
                    end=end_date,
                    program=program,
                    exercise=exercise,
                    muscle=muscle,
                    weighted=weighted,
                    limit=count,
                    offset=offset,
                    sort=sort,
                ),
            )
            out = A.strong_sessions(page)
            sessions = out.get("sessions", [])
            if include_sets and sessions:
                expanded = sessions[:MAX_EXPANDED_SESSIONS]
                semaphore = asyncio.Semaphore(EXPAND_CONCURRENCY)

                async def detail(session: dict[str, Any]) -> dict[str, Any]:
                    async with semaphore:
                        payload = await self._get(
                            f"sessions/{session['id']}",
                            {},
                            lambda: self._client.session(int(session["id"])),
                        )
                    return A.strong_session_detail(payload)

                details = await asyncio.gather(*[detail(session) for session in expanded])
                for session, full in zip(expanded, details, strict=False):
                    session["sets"] = full.get("sets", [])
                omitted = len(sessions) - len(expanded)
                out["sets_omitted"] = omitted
                if omitted:
                    out["sets_hint"] = (
                        "Use get_lifting_sets or get_lifting_sessions with one exercise "
                        "filter to see the remaining sets."
                    )
            out["offset"] = offset
            out["next_offset"] = offset + len(sessions) if out.get("has_more") else None
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)


def build(settings: Settings, cache: CacheStore) -> StrongSource:
    return StrongSource(settings, cache)
