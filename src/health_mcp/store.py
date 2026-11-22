"""Cache primitives: one directory per source, a TTL, and a bounded sweep.

The store owns paths and TTLs only. A source module decides whether it caches
JSON or Parquet, and shapes the payload. Every cache key embeds the range it was
fetched for, so each namespace is swept on write instead of growing forever.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

LEGACY_FILE = re.compile(r"^[0-9a-f]{12}_.+\.(json|parquet)$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
NAMESPACE = re.compile(r"^[a-z0-9_]+$")


class CacheStore:
    MAX_FILES = 500

    def __init__(self, root: str | Path, ttls: Mapping[str, int] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttls = {name: int(hours) for name, hours in (ttls or {}).items()}
        self.swept: dict[str, int] = {}
        self.legacy_removed = self._drop_legacy()

    def _drop_legacy(self) -> int:
        removed = 0
        for path in self.root.iterdir():
            if path.is_file() and LEGACY_FILE.match(path.name):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def namespace_dir(self, namespace: str) -> Path:
        if not NAMESPACE.match(namespace):
            raise ValueError(f"Invalid cache namespace {namespace!r}")
        target = self.root / namespace
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _resolve(self, namespace: str, key: str, suffix: str) -> Path:
        if not SAFE_NAME.match(key):
            raise ValueError(f"Invalid cache key {key!r}")
        return self.namespace_dir(namespace) / f"{key}.{suffix}"

    def ttl_hours(self, namespace: str) -> int:
        return self.ttls.get(namespace, 24)

    def _fresh(self, path: Path, ttl_hours: int | None, namespace: str) -> bool:
        ttl = self.ttl_hours(namespace) if ttl_hours is None else ttl_hours
        return path.exists() and (time.time() - path.stat().st_mtime) < ttl * 3600

    def read_json(
        self, namespace: str, key: str, ttl_hours: int | None = None
    ) -> dict[str, Any] | None:
        path = self._resolve(namespace, key, "json")
        if not self._fresh(path, ttl_hours, namespace):
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_json(
        self,
        namespace: str,
        key: str,
        value: Mapping[str, Any],
        ttl_hours: int | None = None,
    ) -> None:
        path = self._resolve(namespace, key, "json")
        try:
            path.write_text(json.dumps(value, default=str), encoding="utf-8")
        except OSError:
            return
        self.sweep(namespace, ttl_hours)

    def read_parquet(
        self, namespace: str, key: str, ttl_hours: int | None = None
    ) -> pd.DataFrame | None:
        path = self._resolve(namespace, key, "parquet")
        if not self._fresh(path, ttl_hours, namespace):
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            return None

    def write_parquet(
        self, namespace: str, key: str, frame: pd.DataFrame, ttl_hours: int | None = None
    ) -> None:
        path = self._resolve(namespace, key, "parquet")
        try:
            frame.to_parquet(path)
        except Exception:
            return
        self.sweep(namespace, ttl_hours)

    def sweep(self, namespace: str, ttl_hours: int | None = None) -> int:
        """Drop expired files, then the oldest files above the per-namespace cap."""
        directory = self.namespace_dir(namespace)
        ttl = self.ttl_hours(namespace) if ttl_hours is None else ttl_hours
        retention_hours = max(4 * ttl, 30 * 24)
        cutoff = time.time() - retention_hours * 3600
        files = [path for path in directory.iterdir() if path.is_file()]
        removed = [path for path in files if path.stat().st_mtime < cutoff]
        remaining = [path for path in files if path not in removed]
        if len(remaining) > self.MAX_FILES:
            remaining.sort(key=lambda path: path.stat().st_mtime)
            removed.extend(remaining[: len(remaining) - self.MAX_FILES])
        for path in removed:
            path.unlink(missing_ok=True)
        if removed:
            self.swept[namespace] = self.swept.get(namespace, 0) + len(removed)
        return len(removed)

    def refresh(self, namespace: str = "all") -> int:
        if namespace == "all":
            removed = sum(self.refresh(name) for name in self._namespaces())
            self.swept.clear()
            return removed
        if not NAMESPACE.match(namespace):
            return 0
        directory = self.root / namespace
        if not directory.is_dir():
            return 0
        removed = 0
        for path in directory.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
        self.swept.pop(namespace, None)
        return removed

    def _namespaces(self) -> list[str]:
        on_disk = {path.name for path in self.root.iterdir() if path.is_dir()}
        return sorted(on_disk | set(self.ttls))

    def namespace_age_hours(self, namespace: str) -> float | None:
        directory = self.root / namespace
        if not directory.is_dir():
            return None
        files = [path for path in directory.iterdir() if path.is_file()]
        if not files:
            return None
        newest = max(path.stat().st_mtime for path in files)
        return round((time.time() - newest) / 3600, 2)

    def info(self) -> dict[str, Any]:
        namespaces: dict[str, dict[str, Any]] = {}
        total = 0
        for name in self._namespaces():
            directory = self.root / name
            files = (
                [path for path in directory.iterdir() if path.is_file()]
                if directory.is_dir()
                else []
            )
            total += len(files)
            namespaces[name] = {
                "files": len(files),
                "newest_age_hours": self.namespace_age_hours(name),
                "oldest_age_hours": _oldest_age(files),
            }
        return {
            "dir": str(self.root),
            "files": total,
            "namespaces": namespaces,
            "swept": dict(self.swept),
            "legacy_removed": self.legacy_removed,
        }


def _oldest_age(files: list[Path]) -> float | None:
    if not files:
        return None
    oldest = min(path.stat().st_mtime for path in files)
    return round((time.time() - oldest) / 3600, 2)
