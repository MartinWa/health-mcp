from __future__ import annotations

import hashlib

import pytest

from health_mcp.config import Settings, hash_token


def test_settings_construct_without_any_source_credentials(tmp_path):
    settings = Settings(_env_file=None, cache_dir=str(tmp_path))
    assert settings.ow_api_key is None
    assert settings.strong_api_url is None
    assert settings.source_allowlist() is None
    assert settings.bearer_digests() == {}
    assert settings.profile_path.name == "profile.yaml"
    assert settings.events_csv_path.name == "events.csv"


def test_mcp_path_must_be_a_path_and_not_healthz(tmp_path):
    with pytest.raises(ValueError, match="must start"):
        Settings(_env_file=None, mcp_path="mcp", cache_dir=str(tmp_path))
    with pytest.raises(ValueError, match="/healthz"):
        Settings(_env_file=None, mcp_path="/healthz", cache_dir=str(tmp_path))


def test_invalid_timezone_fails_at_startup(tmp_path):
    with pytest.raises(ValueError):
        Settings(_env_file=None, tz="Mars/Olympus", cache_dir=str(tmp_path))


def test_source_allowlist_parsing(tmp_path):
    settings = Settings(_env_file=None, sources=" strong , openwearables ", cache_dir=str(tmp_path))
    assert settings.source_allowlist() == ["strong", "openwearables"]
    empty = Settings(_env_file=None, sources=" , ", cache_dir=str(tmp_path))
    assert empty.source_allowlist() is None


def test_single_bearer_token_alias_is_hashed(tmp_path):
    settings = Settings(_env_file=None, mcp_bearer_token="user-token", cache_dir=str(tmp_path))
    digests = settings.bearer_digests()
    assert digests == {"default": hashlib.sha256(b"user-token").hexdigest()}


def test_bearer_token_map_parses_one_entry_per_consumer(tmp_path):
    first = hash_token("pi-token")
    second = hash_token("zeroclaw-token")
    settings = Settings(
        _env_file=None,
        mcp_bearer_tokens=f"pi:{first}, zeroclaw:{second}",
        cache_dir=str(tmp_path),
    )
    assert settings.bearer_digests() == {"pi": first, "zeroclaw": second}


def test_bearer_token_map_rejects_malformed_entries(tmp_path):
    for raw in ("pi:nothex", "pisecret", ":" + hash_token("x"), "pi:" + "a" * 63):
        settings = Settings(_env_file=None, mcp_bearer_tokens=raw, cache_dir=str(tmp_path))
        with pytest.raises(ValueError):
            settings.bearer_digests()


def test_token_map_takes_precedence_over_single_token(tmp_path):
    digest = hash_token("map-token")
    settings = Settings(
        _env_file=None,
        mcp_bearer_tokens=f"a:{digest}",
        mcp_bearer_token="other",
        cache_dir=str(tmp_path),
    )
    assert settings.bearer_digests() == {"a": digest}
