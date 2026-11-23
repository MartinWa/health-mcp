"""Async client for the strong-data read-only API.

The API is owner-scoped by the bearer token, uses `start`/`end` (not
`start_date`/`end_date`), and wraps every response with `contract_version` and
`generated_at`. This module only transports and validates that envelope; caching
lives in `store.py` and shaping in `analysis.py`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

CONTRACT_MAJOR_MINOR = (1, 0)


class StrongDataError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code


class StrongAuthError(StrongDataError):
    """The token is missing, invalid, revoked or expired."""


class StrongNotFound(StrongDataError):
    """The requested session or exercise does not exist."""


class StrongValidationError(StrongDataError):
    """The API rejected a parameter."""


class StrongUnavailable(StrongDataError):
    """The API could not be reached, or the call exceeded its total deadline."""


SERIES_ENDPOINTS = {
    "sessions": "series/sessions",
    "volume": "series/volume",
    "volume_per_session": "series/volume-per-session",
    "training_time": "series/training-time",
}

BREAKDOWN_ENDPOINTS = {
    "weekday": "breakdown/weekday",
    "program": "breakdown/program",
    "muscle": "breakdown/muscle",
    "exercise": "breakdown/exercises",
}

SESSION_SORT_KEYS = frozenset({"date", "program", "sets", "start_time", "volume", "workout_name"})
SET_SORT_KEYS = frozenset(
    {"date", "est_1rm", "exercise", "reps", "rpe", "set_order", "set_time", "volume", "weight"}
)
RECORD_ENDPOINTS = {
    "est_1rm": "records/est-1rm",
    "bodyweight": "records/bodyweight",
}
RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _clean(params: dict[str, Any] | None) -> dict[str, str]:
    return {key: _param(value) for key, value in (params or {}).items() if value is not None}


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _exercise(name: str) -> str:
    text = name.strip()
    if not text:
        raise ValueError("exercise name must not be blank")
    return quote(text, safe="")


def _validate_sort(sort: str | None, allowed: frozenset[str]) -> str | None:
    if sort is None:
        return None
    key = sort[1:] if sort.startswith("-") else sort
    if key not in allowed:
        names = ", ".join(sorted(allowed))
        raise ValueError(f"Invalid sort {sort!r}; allowed keys: {names}")
    return sort


def parse_major_minor(version: str) -> tuple[int, int]:
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Unparseable contract_version {version!r}") from exc
    return major, minor


class StrongDataClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        deadline: float = 20.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token:
            raise StrongAuthError("STRONG_API_TOKEN is not configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.deadline = deadline
        self.max_retries = max_retries
        self.backoff = backoff
        self.contract_version: str | None = None
        self.contract_degraded = False
        self.contract_warning: str | None = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()

    def check_envelope(self, payload: dict[str, Any]) -> None:
        version = payload.get("contract_version")
        if not isinstance(version, str) or not version:
            raise StrongDataError("Unexpected response envelope from strong-data")
        self.contract_version = version
        try:
            actual = parse_major_minor(version)
        except ValueError:
            actual = (0, 0)
        if actual != CONTRACT_MAJOR_MINOR and not self.contract_degraded:
            expected = ".".join(str(part) for part in CONTRACT_MAJOR_MINOR)
            self.contract_degraded = True
            self.contract_warning = (
                f"strong-data contract_version {version} does not match expected {expected}"
            )
            log.warning(self.contract_warning)

    async def _sleep(self, attempt: int, stop: float) -> None:
        loop = asyncio.get_running_loop()
        delay = min(self.backoff * 2 ** (attempt - 1), max(0.0, stop - loop.time()))
        if delay > 0:
            await asyncio.sleep(delay)

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        stop = loop.time() + (self.deadline if deadline is None else deadline)
        attempt = 0
        query = _clean(params)
        while True:
            remaining = stop - loop.time()
            if remaining <= 0:
                raise StrongUnavailable(
                    f"strong-data exceeded its {self.deadline:g}s deadline calling /{path}"
                )
            try:
                response = await self._client.get(
                    f"/api/v1/{path}",
                    params=query,
                    timeout=min(self.timeout, remaining),
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise StrongDataError(
                        f"Unexpected response envelope from strong-data at {path}"
                    )
                self.check_envelope(payload)
                return payload
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                detail = _error_body(exc.response)
                message = detail.get("message") or f"strong-data returned {status}"
                code = detail.get("code")
                if status in {401, 403}:
                    raise StrongAuthError(
                        "strong-data rejected the token", status=status, code=code
                    ) from exc
                if status == 404:
                    raise StrongNotFound(message, status=status, code=code) from exc
                if status in {400, 422}:
                    raise StrongValidationError(message, status=status, code=code) from exc
                if status in RETRYABLE and attempt < self.max_retries:
                    attempt += 1
                    await self._sleep(attempt, stop)
                    continue
                raise StrongUnavailable(message, status=status, code=code) from exc
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    await self._sleep(attempt, stop)
                    continue
                raise StrongUnavailable(f"strong-data is unavailable: {exc}") from exc

    # -- metadata ---------------------------------------------------------
    async def healthz(self) -> dict[str, Any]:
        return await self._get("healthz")

    async def me(self) -> dict[str, Any]:
        return await self._get("me")

    async def exercises(self) -> dict[str, Any]:
        return await self._get("exercises")

    # -- aggregates -------------------------------------------------------
    async def summary(
        self,
        *,
        start: str,
        end: str,
        program: str | None = None,
    ) -> dict[str, Any]:
        return await self._get("summary", {"start": start, "end": end, "program": program})

    async def series(
        self,
        metric: str,
        *,
        start: str,
        end: str,
        bucket: str = "week",
        group_by: str | None = None,
        moving_average: bool | None = None,
        program: str | None = None,
    ) -> dict[str, Any]:
        try:
            path = SERIES_ENDPOINTS[metric]
        except KeyError as exc:
            names = ", ".join(SERIES_ENDPOINTS)
            raise ValueError(f"Unknown series metric {metric!r}; allowed: {names}") from exc
        return await self._get(
            path,
            {
                "bucket": bucket,
                "group_by": group_by,
                "start": start,
                "end": end,
                "moving_average": moving_average,
                "program": program,
            },
        )

    async def breakdown(
        self,
        dimension: str,
        *,
        start: str,
        end: str,
        limit: int | None = 50,
        program: str | None = None,
    ) -> dict[str, Any]:
        try:
            path = BREAKDOWN_ENDPOINTS[dimension]
        except KeyError as exc:
            names = ", ".join(BREAKDOWN_ENDPOINTS)
            raise ValueError(
                f"Unknown breakdown dimension {dimension!r}; allowed: {names}"
            ) from exc
        return await self._get(
            path, {"start": start, "end": end, "limit": limit, "program": program}
        )

    # -- exercises --------------------------------------------------------
    async def exercise_progress(
        self,
        name: str,
        *,
        start: str,
        end: str,
        group_by: str = "day",
        program: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            f"exercises/{_exercise(name)}/progress",
            {"start": start, "end": end, "group_by": group_by, "program": program},
        )

    async def exercise_records(
        self,
        name: str,
        *,
        start: str,
        end: str,
        program: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            f"exercises/{_exercise(name)}/records",
            {"start": start, "end": end, "program": program},
        )

    # -- records and yearly ----------------------------------------------
    async def records(
        self,
        kind: str,
        *,
        start: str,
        end: str,
        program: str | None = None,
    ) -> dict[str, Any]:
        try:
            path = RECORD_ENDPOINTS[kind]
        except KeyError as exc:
            names = ", ".join(RECORD_ENDPOINTS)
            raise ValueError(f"Unknown record kind {kind!r}; allowed: {names}") from exc
        return await self._get(path, {"start": start, "end": end, "program": program})

    async def yearly(self, *, program: str | None = None) -> dict[str, Any]:
        return await self._get("yearly", {"program": program})

    async def yearly_pace(
        self,
        *,
        year: int | None = None,
        as_of: str | None = None,
        program: str | None = None,
    ) -> dict[str, Any]:
        return await self._get("yearly/pace", {"year": year, "as_of": as_of, "program": program})

    async def yearly_curve(
        self,
        *,
        year: int | None = None,
        years: str | None = None,
        as_of: str | None = None,
        program: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "yearly/curve",
            {"year": year, "years": years, "as_of": as_of, "program": program},
        )

    # -- sessions and raw sets -------------------------------------------
    async def sessions(
        self,
        *,
        start: str,
        end: str,
        program: str | None = None,
        exercise: str | None = None,
        muscle: str | None = None,
        weighted: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str | None = "date",
    ) -> dict[str, Any]:
        return await self._get(
            "sessions",
            {
                "start": start,
                "end": end,
                "program": program,
                "exercise": exercise,
                "muscle": muscle,
                "weighted": weighted,
                "limit": limit,
                "offset": offset,
                "sort": _validate_sort(sort, SESSION_SORT_KEYS),
            },
        )

    async def session(self, session_id: int) -> dict[str, Any]:
        return await self._get(f"sessions/{int(session_id)}")

    async def sets(
        self,
        *,
        start: str,
        end: str,
        program: str | None = None,
        exercise: str | None = None,
        muscle: str | None = None,
        weighted: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "sets",
            {
                "start": start,
                "end": end,
                "program": program,
                "exercise": exercise,
                "muscle": muscle,
                "weighted": weighted,
                "limit": limit,
                "offset": offset,
                "sort": _validate_sort(sort, SET_SORT_KEYS),
            },
        )
