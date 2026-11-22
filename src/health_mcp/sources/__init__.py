"""The registry: one spec per source module, and the enabled subset for a run."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..config import Settings
from ..store import CacheStore
from . import events, openwearables, profile, strong
from .base import Source

log = logging.getLogger(__name__)

AGENTS = ("health", "health-coach")


@dataclass(frozen=True)
class SourceSpec:
    name: str
    exposure: Mapping[str, frozenset[str]]
    configured: Callable[[Settings], bool]
    build: Callable[[Settings, CacheStore], Source]
    skip_reason: Callable[[Settings], str]


MODULES: tuple[SourceSpec, ...] = (
    SourceSpec(
        openwearables.NAME,
        openwearables.EXPOSURE,
        openwearables.configured,
        openwearables.build,
        openwearables.skip_reason,
    ),
    SourceSpec(strong.NAME, strong.EXPOSURE, strong.configured, strong.build, strong.skip_reason),
    SourceSpec(
        profile.NAME, profile.EXPOSURE, profile.configured, profile.build, profile.skip_reason
    ),
    SourceSpec(events.NAME, events.EXPOSURE, events.configured, events.build, events.skip_reason),
)

NAMES: tuple[str, ...] = tuple(spec.name for spec in MODULES)


@dataclass(frozen=True)
class Plan:
    """What this deployment will serve, and why everything else is missing."""

    enabled: tuple[SourceSpec, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [spec.name for spec in self.enabled]


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


def plan_sources(settings: Settings) -> Plan:
    allowed = settings.source_allowlist()
    if allowed:
        unknown = [name for name in allowed if name not in NAMES]
        if unknown:
            valid = ", ".join(NAMES)
            raise ValueError(f"Unknown source(s) {unknown}; valid sources: {valid}")
        candidates = [spec for spec in MODULES if spec.name in allowed]
    else:
        candidates = list(MODULES)
    enabled: list[SourceSpec] = []
    skipped: dict[str, str] = {}
    for spec in candidates:
        if spec.configured(settings):
            enabled.append(spec)
            continue
        reason = spec.skip_reason(settings)
        if allowed:
            reason += " (listed in SOURCES, so this deployment expects it)"
        skipped[spec.name] = reason
    return Plan(tuple(enabled), skipped)


def configured_modules(settings: Settings) -> list[SourceSpec]:
    return list(plan_sources(settings).enabled)


def ttls(settings: Settings) -> dict[str, int]:
    return {
        "openwearables": settings.cache_ttl_hours,
        "strong": settings.strong_cache_ttl_hours,
        # Local files: nothing is cached, the TTL only keeps the namespace known.
        "profile": 0,
        "events": 0,
    }


def build_sources(
    settings: Settings, cache: CacheStore, *, plan: Plan | None = None
) -> dict[str, Source]:
    plan = plan or plan_sources(settings)
    if not plan.enabled:
        raise RuntimeError(
            "No sources are configured. Set OW_API_KEY/OW_USER_ID/OW_BASE_URL and/or "
            "STRONG_API_URL/STRONG_API_TOKEN (see .env.example)."
        )
    log_sources(plan, settings)
    return {spec.name: spec.build(settings, cache) for spec in plan.enabled}


def log_sources(plan: Plan, settings: Settings) -> None:
    """One startup line naming what is live, so a missing file is not invisible."""
    log.info(
        "sources",
        extra={
            "fields": {
                "enabled": plan.names,
                "skipped": dict(plan.skipped),
            }
        },
    )
    requested = set(settings.source_allowlist() or ())
    for name, reason in plan.skipped.items():
        if name in requested:
            log.warning(
                "source listed in SOURCES is not configured",
                extra={"fields": {"source": name, "reason": reason}},
            )
