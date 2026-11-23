"""strong-data source: lifting summaries, trends, breakdowns and sessions."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from .. import analysis as A
from ..config import Settings
from ..store import CacheStore
from ..strong_client import StrongDataClient, StrongDataError
from ..utils import parse_date, today_local, validate_range
from .base import AGENTS_ALL, DateStr, MaxPoints, PageLimit

NAME = "strong"
CACHE_NAMESPACE = "strong"
MAX_EXPANDED_SESSIONS = 10
EXPAND_CONCURRENCY = 4
SETS_HARD_CAP = 200
RECORDS_HARD_CAP = 100
EXERCISE_RECORD_ROWS = 30
CURVE_YEARS = re.compile(r"^\d{4}(,\d{4})*$")
UNSAFE_CACHE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

TrendMetric = Literal["volume", "sessions", "volume_per_session", "training_time"]
Bucket = Literal["day", "week", "month", "year"]
GroupBy = Literal["program", "muscle", "exercise"]
BreakdownDimension = Literal["weekday", "program", "muscle", "exercise"]
ExerciseGroupBy = Literal["day", "session"]
RecordKind = Literal["est_1rm", "bodyweight"]
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
SetSortKey = Literal[
    "date",
    "-date",
    "est_1rm",
    "-est_1rm",
    "exercise",
    "-exercise",
    "reps",
    "-reps",
    "rpe",
    "-rpe",
    "set_order",
    "-set_order",
    "set_time",
    "-set_time",
    "volume",
    "-volume",
    "weight",
    "-weight",
]
PointsCap = Annotated[
    int | None,
    Field(description="Max points to return; default from DEFAULT_MAX_POINTS", ge=10, le=1000),
]

EXPOSURE: dict[str, frozenset[str]] = {
    "get_lifting_summary": AGENTS_ALL,
    "get_lifting_trend": AGENTS_ALL,
    "get_lifting_breakdown": AGENTS_ALL,
    "get_lifting_sessions": AGENTS_ALL,
    "get_exercise_detail": frozenset({"health"}),
    "get_lifting_records": frozenset({"health"}),
    "get_lifting_sets": frozenset({"health"}),
    "get_yearly_progress": frozenset({"health"}),
}


def configured(settings: Settings) -> bool:
    return bool(settings.strong_api_url and settings.strong_api_token)


def skip_reason(settings: Settings) -> str:
    return "STRONG_API_URL and STRONG_API_TOKEN must both be set"


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
            "cache_empty": self.cache.namespace_age_hours(self.cache_namespace) is None,
        }

    @staticmethod
    def _key(path: str, params: dict[str, Any]) -> str:
        clean = {key: value for key, value in params.items() if value is not None}
        digest = hashlib.sha256(
            (path + json.dumps(clean, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()[:16]
        # Exercise names reach the path, so the slug must survive CacheStore.SAFE_NAME.
        slug = UNSAFE_CACHE_CHARS.sub("-", path.replace("/", "-")).strip("-")[:48] or "payload"
        return f"{slug}-{digest}"

    async def _exercise_suggestions(self, name: str, limit: int = 5) -> list[str]:
        """Nearest known exercise names, so a typo does not read as an untrained exercise."""
        try:
            payload = await self._get("exercises", {}, lambda: self._client.exercises())
        except Exception:
            return []
        items = payload.get("items") if isinstance(payload, dict) else payload
        names = [
            str(item.get("name") or item.get("key"))
            for item in (items or [])
            if isinstance(item, dict) and (item.get("name") or item.get("key"))
        ]
        return difflib.get_close_matches(name, names, n=limit, cutoff=0.6)

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

    async def weekly_volume_series(self, start: str, end: str) -> dict[str, Any]:
        return await self._get(
            "series/volume",
            {"start": start, "end": end, "bucket": "week"},
            lambda: self._client.series("volume", start=start, end=end, bucket="week"),
        )

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
            self.get_exercise_detail,
            self.get_lifting_records,
            self.get_lifting_sets,
            self.get_yearly_progress,
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
            out = A.strong_summary(
                summary, weekly, exercises, muscle, start_date=start_date, end_date=end_date
            )
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

    async def get_exercise_detail(
        self,
        start_date: DateStr,
        end_date: DateStr,
        exercise: Annotated[str, Field(description="Exact exercise name, e.g. 'Squat'")],
        group_by: Annotated[
            ExerciseGroupBy, Field(description="One progress point per day or per session")
        ] = "day",
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
        max_points: PointsCap = None,
    ) -> dict[str, Any]:
        """One exercise: est. 1RM, top weight and volume over time, plus best sets by reps."""
        try:
            validate_range(start_date, end_date)
            name = exercise.strip()
            if not name:
                return {"error": "exercise must not be blank"}
            params = {"start": start_date, "end": end_date, "program": program}
            progress, records = await asyncio.gather(
                self._get(
                    f"exercises/{name}/progress",
                    {**params, "group_by": group_by},
                    lambda: self._client.exercise_progress(
                        name,
                        start=start_date,
                        end=end_date,
                        group_by=group_by,
                        program=program,
                    ),
                ),
                self._get(
                    f"exercises/{name}/records",
                    params,
                    lambda: self._client.exercise_records(
                        name, start=start_date, end=end_date, program=program
                    ),
                ),
            )
            out = A.strong_exercise_detail(
                progress,
                records,
                name,
                max_points or self.settings.default_max_points,
                EXERCISE_RECORD_ROWS,
            )
            if out.get("total_points") == 0 and "error" not in out:
                out["suggestions"] = await self._exercise_suggestions(name)
                out["suggestion_note"] = (
                    "no sets matched this name; suggestions come from the exercise list, so a "
                    "typo and an untrained exercise are told apart"
                )
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_lifting_records(
        self,
        start_date: DateStr,
        end_date: DateStr,
        kind: Annotated[
            RecordKind,
            Field(description="est_1rm (Epley estimate) or bodyweight (ranked by reps)"),
        ] = "est_1rm",
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
        limit: Annotated[
            int | None,
            Field(description="Rows to return (hard cap 100)", ge=1, le=RECORDS_HARD_CAP),
        ] = None,
    ) -> dict[str, Any]:
        """Best sets per exercise: estimated 1RM, or bodyweight sets ranked by reps."""
        try:
            validate_range(start_date, end_date)
            count = min(limit or self.settings.default_page_limit, RECORDS_HARD_CAP)
            params = {"start": start_date, "end": end_date, "program": program}
            payload = await self._get(
                f"records/{kind.replace('_', '-')}",
                params,
                lambda: self._client.records(kind, start=start_date, end=end_date, program=program),
            )
            out = A.strong_records(payload, kind, count)
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_lifting_sets(
        self,
        start_date: DateStr,
        end_date: DateStr,
        exercise: Annotated[str | None, Field(description="Filter to one exercise")] = None,
        muscle: Annotated[str | None, Field(description="Filter to one muscle group")] = None,
        weighted: Annotated[bool | None, Field(description="Only weighted sets")] = None,
        limit: Annotated[
            int | None, Field(description="Rows to return (hard cap 200)", ge=1, le=SETS_HARD_CAP)
        ] = None,
        offset: Annotated[int, Field(description="Skip first N sets", ge=0)] = 0,
        sort: Annotated[
            SetSortKey, Field(description="Sort key, '-' prefix for descending")
        ] = "set_time",
    ) -> dict[str, Any]:
        """Raw set history (opt-in detail), capped at 200 rows with exercise/muscle filters."""
        try:
            validate_range(start_date, end_date)
            count = min(limit or self.settings.default_page_limit, SETS_HARD_CAP)
            params = {
                "start": start_date,
                "end": end_date,
                "exercise": exercise,
                "muscle": muscle,
                "weighted": weighted,
                "limit": count,
                "offset": offset,
                "sort": sort,
            }
            page = await self._get(
                "sets",
                params,
                lambda: self._client.sets(
                    start=start_date,
                    end=end_date,
                    exercise=exercise,
                    muscle=muscle,
                    weighted=weighted,
                    limit=count,
                    offset=offset,
                    sort=sort,
                ),
            )
            out = A.strong_sets(page)
            out["offset"] = offset
            out["next_offset"] = offset + out["returned"] if out.get("has_more") else None
            out["limit"] = count
            out["period"] = {"start": start_date, "end": end_date}
            return out
        except Exception as exc:
            return self._error(exc)

    async def get_yearly_progress(
        self,
        year: Annotated[
            int | None,
            Field(
                description="Calendar year; defaults to the current local year", ge=2000, le=2100
            ),
        ] = None,
        as_of: Annotated[
            DateStr | None,
            Field(description="Cut-off date inside that year; defaults to today, or Dec 31"),
        ] = None,
        program: Annotated[str | None, Field(description="Filter to one program")] = None,
        include_curve: Annotated[
            bool, Field(description="Add the cumulative day-of-year curve")
        ] = False,
        curve_years: Annotated[
            str | None, Field(description="Comma-separated years for the curve, e.g. '2024,2025'")
        ] = None,
        max_points: PointsCap = None,
    ) -> dict[str, Any]:
        """Yearly totals, year-to-date pace and projection; optional cumulative curve."""
        try:
            today = parse_date(today_local(self.tz))
            cut_off = parse_date(as_of) if as_of else None
            target = year or (cut_off or today).year
            if cut_off is None:
                cut_off = today if target == today.year else date(target, 12, 31)
            if cut_off.year != target:
                raise ValueError(f"as_of {cut_off.isoformat()} is not in year {target}")
            if curve_years is not None:
                if not CURVE_YEARS.match(curve_years):
                    raise ValueError("curve_years must be comma-separated years, e.g. '2024,2025'")
                if not all(2000 <= int(part) <= 2100 for part in curve_years.split(",")):
                    raise ValueError("curve_years must be between 2000 and 2100")
            as_of_text = cut_off.isoformat()
            params = {"year": target, "as_of": as_of_text, "program": program}
            yearly, pace = await asyncio.gather(
                self._get(
                    "yearly",
                    {"program": program},
                    lambda: self._client.yearly(program=program),
                ),
                self._get(
                    "yearly/pace",
                    params,
                    lambda: self._client.yearly_pace(
                        year=target, as_of=as_of_text, program=program
                    ),
                ),
            )
            out = A.strong_yearly(yearly, pace, as_of_text)
            out["year"] = target
            if include_curve:
                curve = await self._get(
                    "yearly/curve",
                    {**params, "years": curve_years},
                    lambda: self._client.yearly_curve(
                        year=target, years=curve_years, as_of=as_of_text, program=program
                    ),
                )
                out["curve"] = A.strong_curve(curve, max_points or self.settings.default_max_points)
            out["period"] = {"start": f"{target}-01-01", "end": as_of_text}
            return out
        except Exception as exc:
            return self._error(exc)


def build(settings: Settings, cache: CacheStore) -> StrongSource:
    return StrongSource(settings, cache)
