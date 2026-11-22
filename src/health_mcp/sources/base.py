"""The contract every source module implements.

A module under `sources/` exports:

    NAME: str                      # source identity, also the cache namespace
    EXPOSURE: dict[str, frozenset[str]]   # tool name -> agent exposure sets
    configured(settings) -> bool   # credentials/file present, no network call
    build(settings, cache) -> Source

A `Source` instance owns its client, its cache namespace and its tools. It never
raises at the server boundary: it returns `{"error", "source", "cache_age_hours"}`.
Caps (`include_series`, `max_points`, `truncated`) are the module's responsibility.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

from pydantic import Field

ToolFn = Callable[..., Any]
AGENTS_ALL = frozenset({"health", "health-coach"})

DateStr = Annotated[str, Field(description="Date as YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")]
SeriesFlag = Annotated[
    bool, Field(description="Include a downsampled date/value series? Default false (stats only).")
]
MaxPoints = Annotated[
    int | None,
    Field(
        description="Max series points when include_series=true; default from DEFAULT_MAX_POINTS",
        ge=10,
        le=1000,
    ),
]
PageLimit = Annotated[
    int | None,
    Field(description="Max rows to return; default from DEFAULT_PAGE_LIMIT", ge=1, le=500),
]


@runtime_checkable
class Source(Protocol):
    NAME: ClassVar[str]
    cache_namespace: ClassVar[str]

    def tools(self) -> list[ToolFn]: ...

    def refresh(self) -> int: ...

    async def status(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...
