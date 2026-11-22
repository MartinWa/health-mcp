from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from helpers import envelope, strong_client

from health_mcp.strong_client import (
    StrongAuthError,
    StrongDataClient,
    StrongDataError,
    StrongNotFound,
    StrongUnavailable,
    StrongValidationError,
    parse_major_minor,
)


def run(coro):
    return asyncio.run(coro)


def test_auth_header_and_start_end_param_names():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=envelope(volume_kg=1, sessions=1, sets=1))

    client = strong_client(handler)

    async def go():
        try:
            await client.summary(start="2026-01-01", end="2026-01-31")
        finally:
            await client.aclose()

    run(go())
    assert seen["auth"] == "Bearer strong-token"
    assert seen["path"] == "/api/v1/summary"
    assert seen["params"] == {"start": "2026-01-01", "end": "2026-01-31"}


def test_retryable_status_is_retried_with_the_same_query():
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": "busy", "message": "busy"}})
        return httpx.Response(200, json=envelope(sessions=1, sets=1, volume_kg=1))

    client = strong_client(handler, max_retries=3)

    async def go():
        try:
            await client.summary(start="2026-01-01", end="2026-01-31")
        finally:
            await client.aclose()

    run(go())
    assert len(calls) == 3
    assert all(call == calls[0] for call in calls)


def test_validation_error_includes_the_api_code_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"error": {"code": "bad_param", "message": "unknown parameter"}}
        )

    client = strong_client(handler)

    async def go():
        try:
            with pytest.raises(StrongValidationError) as info:
                await client.summary(start="2026-01-01", end="2026-01-31")
        finally:
            await client.aclose()
        return info.value

    error = run(go())
    assert error.status == 422
    assert error.code == "bad_param"
    assert "unknown parameter" in str(error)


def test_auth_404_and_exhausted_5xx_are_mapped():
    def make(status: int):
        return strong_client(
            lambda request: httpx.Response(status, json={"error": {"code": "x", "message": "m"}}),
            max_retries=2,
        )

    async def call(client: StrongDataClient):
        try:
            await client.me()
        finally:
            await client.aclose()

    for status, error in ((401, StrongAuthError), (403, StrongAuthError), (404, StrongNotFound)):
        with pytest.raises(error):
            run(call(make(status)))
    with pytest.raises(StrongUnavailable):
        run(call(make(500)))


def test_malformed_envelope_is_rejected():
    client = strong_client(lambda request: httpx.Response(200, json={"volume_kg": 1}))

    async def go():
        try:
            with pytest.raises(StrongDataError, match="envelope"):
                await client.summary(start="2026-01-01", end="2026-01-31")
        finally:
            await client.aclose()

    run(go())


def test_contract_major_mismatch_marks_the_source_degraded():
    client = strong_client(
        lambda request: httpx.Response(200, json={"contract_version": "2.0.0", "generated_at": "x"})
    )

    async def go():
        try:
            await client.me()
            assert client.contract_version == "2.0.0"
            assert client.contract_degraded is True
            assert client.contract_warning is not None
        finally:
            await client.aclose()

    run(go())


def test_contract_patch_drift_is_not_degraded():
    client = strong_client(
        lambda request: httpx.Response(200, json={"contract_version": "1.0.9", "generated_at": "x"})
    )

    async def go():
        try:
            await client.me()
            assert client.contract_degraded is False
        finally:
            await client.aclose()

    run(go())


def test_parse_major_minor():
    assert parse_major_minor("1.0.0") == (1, 0)
    assert parse_major_minor("1.0") == (1, 0)
    with pytest.raises(ValueError):
        parse_major_minor("v1")


def test_total_deadline_stops_the_call():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(503, json={"error": {"code": "busy", "message": "busy"}})

    client = StrongDataClient(
        "https://strong.test",
        "token",
        transport=httpx.MockTransport(slow),
        deadline=0.03,
        backoff=0.001,
        max_retries=100,
    )

    async def go():
        started = time.perf_counter()
        try:
            with pytest.raises(StrongUnavailable, match="deadline"):
                await client.me()
        finally:
            await client.aclose()
        assert time.perf_counter() - started < 1.0

    run(go())


def test_sort_keys_are_whitelisted_before_the_request():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=envelope(items=[], count=0, next=None, previous=None, has_more=False)
        )

    client = strong_client(handler)

    async def go():
        try:
            with pytest.raises(ValueError, match="Invalid sort"):
                await client.sessions(start="2026-01-01", end="2026-01-31", sort="--drop")
            with pytest.raises(ValueError, match="Invalid sort"):
                await client.sets(start="2026-01-01", end="2026-01-31", sort="est_1rm;")
            await client.sessions(start="2026-01-01", end="2026-01-31", sort="-volume")
        finally:
            await client.aclose()

    run(go())
    assert len(requests) == 1
    assert requests[0].url.params["sort"] == "-volume"


def test_pagination_envelope_is_passed_through_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope(items=[{"id": 1}], count=2, next="n", previous=None, has_more=True),
        )

    client = strong_client(handler)

    async def go():
        try:
            payload = await client.sessions(start="2026-01-01", end="2026-01-31", limit=1)
        finally:
            await client.aclose()
        assert payload["has_more"] is True
        assert payload["count"] == 2

    run(go())


def test_empty_items_are_not_an_error():
    client = strong_client(
        lambda request: httpx.Response(200, json=envelope(items=[], count=0, bucket="day"))
    )

    async def go():
        try:
            payload = await client.series(
                "volume", start="2026-01-01", end="2026-01-31", bucket="day"
            )
        finally:
            await client.aclose()
        assert payload["items"] == []

    run(go())
