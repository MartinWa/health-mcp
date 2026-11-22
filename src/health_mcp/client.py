"""Async Open Wearables API client (pagination, caps and retries only, no caching)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

RETRYABLE = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_PAGES = 20


class OpenWearablesError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    pages: int = 0


class OpenWearablesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise OpenWearablesError("OW_API_KEY is not configured")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff = backoff
        self._client = httpx.AsyncClient(
            headers={"X-Open-Wearables-API-Key": api_key},
            timeout=timeout,
            base_url=self.base_url,
            transport=transport,
        )

    async def aclose(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(path, params=clean)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status in RETRYABLE and attempt < self.max_retries:
                    last = exc
                    await asyncio.sleep(self.backoff * 2**attempt)
                    continue
                raise OpenWearablesError(
                    f"Open Wearables returned {status}", status=status
                ) from exc
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
                last = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff * 2**attempt)
                    continue
                raise OpenWearablesError(f"Open Wearables is unavailable: {exc}") from exc
        raise OpenWearablesError(f"Open Wearables is unavailable: {last}")

    async def _paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> Page:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while True:
            if pages >= max_pages:
                return Page(rows=rows, truncated=True, pages=pages)
            query = dict(params or {})
            if cursor:
                query["cursor"] = cursor
            data = await self._get(path, query)
            rows.extend(data.get("data") or [])
            pages += 1
            pagination = data.get("pagination") or {}
            cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not cursor:
                return Page(rows=rows, truncated=False, pages=pages)

    def _user(self, uid: str, suffix: str) -> str:
        return f"/api/v1/users/{quote(uid, safe='')}/{suffix}"

    async def activity(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated(
            self._user(uid, "summaries/activity"), {"start_date": s, "end_date": e}
        )

    async def sleep(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated(
            self._user(uid, "summaries/sleep"), {"start_date": s, "end_date": e}
        )

    async def recovery(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated(
            self._user(uid, "summaries/recovery"), {"start_date": s, "end_date": e}
        )

    async def workouts(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated(
            self._user(uid, "events/workouts"), {"start_date": s, "end_date": e}
        )

    async def health_scores(
        self,
        uid: str,
        s: str,
        e: str,
        category: str | None = None,
    ) -> Page:
        return await self._paginated(
            self._user(uid, "health-scores"),
            {"start_date": s, "end_date": e, "category": category},
        )

    async def timeseries(
        self,
        uid: str,
        s: str,
        e: str,
        types: list[str] | None = None,
        resolution: str = "raw",
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> Page:
        return await self._paginated(
            self._user(uid, "timeseries"),
            {
                "start_time": s,
                "end_time": e,
                "resolution": resolution,
                "types": ",".join(types) if types else None,
            },
            max_pages=max_pages,
        )

    async def user(self, uid: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/users/{quote(uid, safe='')}")
