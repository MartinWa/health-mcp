from __future__ import annotations

import os
import time

import pandas as pd
import pytest

from health_mcp.store import CacheStore


def test_json_roundtrip_and_ttl_expiry(tmp_path):
    store = CacheStore(tmp_path, {"strong": 6})
    store.write_json("strong", "summary-abc", {"payload": {"volume_kg": 1}})
    assert store.read_json("strong", "summary-abc") == {"payload": {"volume_kg": 1}}

    path = tmp_path / "strong" / "summary-abc.json"
    old = time.time() - 7 * 3600
    os.utime(path, (old, old))
    assert store.read_json("strong", "summary-abc") is None


def test_parquet_roundtrip(tmp_path):
    store = CacheStore(tmp_path, {"openwearables": 24})
    frame = pd.DataFrame({"date": ["2026-01-01"], "value": [1.5]})
    store.write_parquet("openwearables", "sleep-2026-01-01_2026-01-02", frame)
    loaded = store.read_parquet("openwearables", "sleep-2026-01-01_2026-01-02")
    assert loaded is not None
    assert loaded["value"].tolist() == [1.5]


def test_refresh_only_clears_the_named_namespace(tmp_path):
    store = CacheStore(tmp_path, {"strong": 6, "openwearables": 24})
    store.write_json("strong", "a", {"value": 1})
    store.write_json("openwearables", "b", {"value": 2})
    assert store.refresh("strong") == 1
    assert store.read_json("strong", "a") is None
    assert store.read_json("openwearables", "b") == {"value": 2}
    assert store.refresh("unknown") == 0
    assert store.refresh("all") == 1


def test_namespace_is_swept_to_a_file_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(CacheStore, "MAX_FILES", 5)
    store = CacheStore(tmp_path, {"strong": 1})
    for index in range(12):
        store.write_json("strong", f"entry-{index}", {"value": index})
        time.sleep(0.002)
    files = list((tmp_path / "strong").glob("*"))
    assert len(files) == 5
    assert store.swept["strong"] == 7
    assert store.info()["swept"]["strong"] == 7


def test_expired_files_are_swept_on_write(tmp_path):
    store = CacheStore(tmp_path, {"strong": 1})
    store.write_json("strong", "old", {"value": 1})
    old_path = tmp_path / "strong" / "old.json"
    ancient = time.time() - 60 * 24 * 3600
    os.utime(old_path, (ancient, ancient))
    store.write_json("strong", "new", {"value": 2})
    assert not old_path.exists()
    assert store.swept["strong"] >= 1


def test_legacy_flat_namespace_files_are_removed_on_start(tmp_path):
    (tmp_path / "0123456789ab_sleep.parquet").write_bytes(b"x")
    (tmp_path / "0123456789ab_strong_summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "keep-me.txt").write_text("not a cache file", encoding="utf-8")
    store = CacheStore(tmp_path, {})
    assert store.legacy_removed == 2
    assert not (tmp_path / "0123456789ab_sleep.parquet").exists()
    assert (tmp_path / "keep-me.txt").exists()


def test_unknown_namespace_and_key_are_rejected(tmp_path):
    store = CacheStore(tmp_path, {})
    with pytest.raises(ValueError):
        store.read_json("../etc", "x")
    with pytest.raises(ValueError):
        store.read_json("strong", "../escape")


def test_cache_info_reports_per_namespace_ages(tmp_path):
    store = CacheStore(tmp_path, {"strong": 6, "openwearables": 24})
    store.write_json("strong", "a", {"value": 1})
    info = store.info()
    assert info["files"] == 1
    assert set(info["namespaces"]) == {"openwearables", "strong"}
    assert info["namespaces"]["strong"]["files"] == 1
    assert info["namespaces"]["strong"]["newest_age_hours"] is not None
    assert info["namespaces"]["openwearables"]["files"] == 0
