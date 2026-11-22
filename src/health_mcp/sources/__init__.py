"""The registry: one spec per source module, and the enabled subset for a run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..config import Settings
from ..store import CacheStore
from . import openwearables, strong
from .base import Source

AGENTS = ("health", "health-coach")


@dataclass(frozen=True)
class SourceSpec:
    name: str
    exposure: Mapping[str, frozenset[str]]
    configured: Callable[[Settings], bool]
    build: Callable[[Settings, CacheStore], Source]


MODULES: tuple[SourceSpec, ...] = (
    SourceSpec(
        openwearables.NAME, openwearables.EXPOSURE, openwearables.configured, openwearables.build
    ),
    SourceSpec(strong.NAME, strong.EXPOSURE, strong.configured, strong.build),
)

NAMES: tuple[str, ...] = tuple(spec.name for spec in MODULES)


def get(name: str) -> SourceSpec:
    for spec in MODULES:
        if spec.name == name:
            return spec
    valid = ", ".join(NAMES)
    raise ValueError(f"Unknown source {name!r}; valid sources: {valid}")


def exposure() -> dict[str, frozenset[str]]:
    merged: dict[str, frozenset[str]] = {}
    for spec in MODULES:
        merged.update(spec.exposure)
    return merged


def configured_modules(settings: Settings) -> list[SourceSpec]:
    allowed = settings.source_allowlist()
    if allowed:
        unknown = [name for name in allowed if name not in NAMES]
        if unknown:
            valid = ", ".join(NAMES)
            raise ValueError(f"Unknown source(s) {unknown}; valid sources: {valid}")
        candidates = [spec for spec in MODULES if spec.name in allowed]
    else:
        candidates = list(MODULES)
    return [spec for spec in candidates if spec.configured(settings)]


def ttls(settings: Settings) -> dict[str, int]:
    return {
        "openwearables": settings.cache_ttl_hours,
        "strong": settings.strong_cache_ttl_hours,
    }


def build_sources(settings: Settings, cache: CacheStore) -> dict[str, Source]:
    modules = configured_modules(settings)
    if not modules:
        raise RuntimeError(
            "No sources are configured. Set OW_API_KEY/OW_USER_ID/OW_BASE_URL and/or "
            "STRONG_API_URL/STRONG_API_TOKEN (see .env.example)."
        )
    return {spec.name: spec.build(settings, cache) for spec in modules}
