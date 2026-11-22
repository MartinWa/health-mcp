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
