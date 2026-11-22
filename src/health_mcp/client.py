"""Async Open Wearables API client (pagination, caps and retries only, no caching)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

RETRYABLE = frozenset({429, 500, 502, 503, 504})
SERVER_ERRORS = frozenset({500, 502, 503, 504})
DEFAULT_MAX_PAGES = 20
MAX_SPLIT_DEPTH = 4

log = logging.getLogger(__name__)


class OpenWearablesError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    pages: int = 0
    failed: bool = False


def _unique_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


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

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retry_5xx: bool = True,
    ) -> Any:
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(path, params=clean)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                retryable = status in RETRYABLE and (retry_5xx or status not in SERVER_ERRORS)
                if retryable and attempt < self.max_retries:
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
        fallback: bool = False,
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
            try:
                # A repeated cursor that 5xxes is what the date-window fallback exists
                # for, so retrying it only delays the split. 429 and network errors stay
                # retryable, and an endpoint without a fallback keeps every retry.
                data = await self._get(path, query, retry_5xx=not (fallback and cursor))
            except OpenWearablesError as exc:
                if pages == 0 or exc.status not in SERVER_ERRORS:
                    raise
                log.warning(
                    "paging stopped; falling back to date windows",
                    extra={
                        "fields": {
                            "endpoint": path.rsplit("/", 1)[-1],
                            "pages": pages,
                            "status": exc.status,
                        }
                    },
                )
                return Page(rows=rows, truncated=True, pages=pages, failed=True)
            rows.extend(data.get("data") or [])
            pages += 1
            pagination = data.get("pagination") or {}
            cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not cursor:
                return Page(rows=rows, truncated=False, pages=pages)

    async def _paginated_days(
        self,
        path: str,
        start: str,
        end: str,
        *,
        extra: dict[str, Any] | None = None,
        pad_days: int = 0,
        max_pages: int = DEFAULT_MAX_PAGES,
        depth: int = 0,
    ) -> Page:
        """Day-ranged paging that splits the window when the cursor stops working.

        Open Wearables answers a repeated cursor for some endpoints with 500, which
        would otherwise discard every row after the first page. The windows are
        disjoint, so a split returns the same rows the cursor would have. `pad_days`
        widens each half into its sibling for endpoints that filter a session by both
        its start and its end, so a session crossing the boundary is still fetched.
        """
        params = {**(extra or {}), "start_date": start, "end_date": end}
        page = await self._paginated(path, params, max_pages=max_pages, fallback=True)
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            return page
        if not page.failed or depth >= MAX_SPLIT_DEPTH or first >= last:
            return page
        middle = first + (last - first) // 2
        left_end = min(middle + timedelta(days=pad_days), last)
        right_start = max(middle + timedelta(days=1 - pad_days), first)
        halves = await asyncio.gather(
            self._paginated_days(
                path,
                start,
                left_end.isoformat(),
                extra=extra,
                pad_days=pad_days,
                max_pages=max_pages,
                depth=depth + 1,
            ),
            self._paginated_days(
                path,
                right_start.isoformat(),
                end,
                extra=extra,
                pad_days=pad_days,
                max_pages=max_pages,
                depth=depth + 1,
            ),
            return_exceptions=True,
        )
        recovered = [half for half in halves if isinstance(half, Page)]
        pages = page.pages + sum(half.pages for half in recovered)
        if len(recovered) != 2:
            # A sibling failed: keep what every successful window recovered next to the
            # rows already in hand, and report the answer as incomplete.
            rows = _unique_rows([*page.rows, *(row for half in recovered for row in half.rows)])
            return Page(rows=rows, truncated=True, pages=pages, failed=True)
        left, right = recovered
        return Page(
            rows=_unique_rows([*left.rows, *right.rows]),
            truncated=left.truncated or right.truncated,
            pages=pages,
        )

    def _user(self, uid: str, suffix: str) -> str:
        return f"/api/v1/users/{quote(uid, safe='')}/{suffix}"

    async def activity(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated_days(self._user(uid, "summaries/activity"), s, e)

    async def sleep(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated_days(self._user(uid, "summaries/sleep"), s, e)

    async def recovery(self, uid: str, s: str, e: str) -> Page:
        return await self._paginated_days(self._user(uid, "summaries/recovery"), s, e)

    async def workouts(self, uid: str, s: str, e: str) -> Page:
        # A session is filtered by both its start and its end, so a split boundary has
        # to overlap by a day or a session crossing it is fetched by neither half.
        return await self._paginated_days(self._user(uid, "events/workouts"), s, e, pad_days=1)

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
                # OW declares `types` as a list of query parameters, so send it repeated.
                "types": list(types) if types else None,
            },
            max_pages=max_pages,
        )

    async def user(self, uid: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/users/{quote(uid, safe='')}")

    async def personal_record(self, uid: str) -> dict[str, Any]:
        return await self._get(self._user(uid, "personal-record"))

    async def body(
        self, uid: str, *, average_period: int = 7, latest_window_hours: int = 4
    ) -> dict[str, Any]:
        payload = await self._get(
            self._user(uid, "summaries/body"),
            {"average_period": average_period, "latest_window_hours": latest_window_hours},
        )
        return payload if isinstance(payload, dict) else {}

    async def body_static(self, uid: str) -> dict[str, Any]:
        payload = await self.body(uid)
        slow = payload.get("slow_changing") or payload.get("static")
        return slow if isinstance(slow, dict) else {}

    async def data_sources(self, uid: str) -> dict[str, Any]:
        payload = await self._get(self._user(uid, "data-sources"))
        return payload if isinstance(payload, dict) else {}

    async def connections(self, uid: str) -> list[dict[str, Any]]:
        payload = await self._get(self._user(uid, "connections"))
        return (
            [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        )

    async def data_summary(
        self, uid: str, *, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        payload = await self._get(
            self._user(uid, "summaries/data"), {"start_date": start, "end_date": end}
        )
        return payload if isinstance(payload, dict) else {}

    async def sleep_sessions(
        self, uid: str, s: str, e: str, *, max_pages: int = DEFAULT_MAX_PAGES
    ) -> Page:
        return await self._paginated(
            self._user(uid, "events/sleep"),
            {"start_date": s, "end_date": e, "include": "stages"},
            max_pages=max_pages,
        )
