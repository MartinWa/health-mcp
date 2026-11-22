from __future__ import annotations

import pytest
from helpers import both_settings, make_settings, ow_settings, strong_settings

from health_mcp import sources as registry


def test_registry_names_and_exposure():
    assert registry.NAMES == ("openwearables", "strong", "profile", "events")
    assert registry.get("strong").name == "strong"
    merged: set[str] = set()
    for spec in registry.MODULES:
        merged |= set(spec.exposure)
    assert set(registry.exposure()) == merged
    assert set(registry.get("profile").exposure) == set()
    assert registry.get("events").exposure == {"get_medicine_logs": frozenset({"health-coach"})}
    with pytest.raises(ValueError, match="Unknown source"):
        registry.get("hevy")


def test_no_credentials_enables_no_sources(tmp_path):
    assert registry.configured_modules(make_settings(tmp_path)) == []


def test_openwearables_only(tmp_path):
    modules = registry.configured_modules(ow_settings(tmp_path))
    assert [module.name for module in modules] == ["openwearables"]


def test_strong_only(tmp_path):
    modules = registry.configured_modules(strong_settings(tmp_path))
    assert [module.name for module in modules] == ["strong"]


def test_both_sources(tmp_path):
    modules = registry.configured_modules(both_settings(tmp_path))
    assert [module.name for module in modules] == ["openwearables", "strong"]


def test_sources_allowlist_restricts_enabled_modules(tmp_path):
    settings = both_settings(tmp_path, sources="strong")
    assert [module.name for module in registry.configured_modules(settings)] == ["strong"]


def test_unknown_source_in_the_allowlist_fails_loudly(tmp_path):
    settings = make_settings(
        tmp_path, sources="strong,hevy", strong_api_url="https://x", strong_api_token="t"
    )
    with pytest.raises(ValueError, match="Unknown source"):
        registry.configured_modules(settings)


def test_ttls_follow_the_configured_cache_hours(tmp_path):
    settings = both_settings(tmp_path, cache_ttl_hours=12, strong_cache_ttl_hours=3)
    assert registry.ttls(settings) == {
        "openwearables": 12,
        "strong": 3,
        "profile": 0,
        "events": 0,
    }


def test_plan_names_why_a_requested_source_is_missing(tmp_path):
    settings = strong_settings(tmp_path, sources="openwearables,strong,profile,events")
    plan = registry.plan_sources(settings)
    assert plan.names == ["strong"]
    assert set(plan.skipped) == {"openwearables", "profile", "events"}
    assert "profile.yaml" in plan.skipped["profile"]
    assert "events.csv" in plan.skipped["events"]
    assert all("listed in SOURCES" in reason for reason in plan.skipped.values())


def test_plan_does_not_call_unrequested_sources_skipped(tmp_path):
    plan = registry.plan_sources(strong_settings(tmp_path, sources="strong"))
    assert plan.names == ["strong"]
    assert plan.skipped == {}


def test_startup_log_names_enabled_and_skipped_sources(tmp_path, caplog):
    import logging

    from health_mcp.store import CacheStore

    settings = strong_settings(tmp_path, sources="strong,events")
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    caplog.set_level(logging.INFO, logger="health_mcp.sources")
    registry.build_sources(settings, cache)

    records = {record.getMessage(): record for record in caplog.records}
    started = records["sources"]
    assert started.fields["enabled"] == ["strong"]
    assert "events.csv" in started.fields["skipped"]["events"]

    warned = records["source listed in SOURCES is not configured"]
    assert warned.fields["source"] == "events"
