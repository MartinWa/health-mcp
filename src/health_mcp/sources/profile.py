"""Profile source: reads the host-authored profile.yaml, never writes it.

Loading is fail-soft: a broken or half-edited file keeps the last valid copy in
memory and reports the error path, so a typo never takes the server down. The
file is re-read when its mtime or size changes, so a hand-edited profile shows
up on the next call without a restart.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, ClassVar

from .. import profile as P
from ..config import Settings
from ..store import CacheStore
from ..utils import today_local

NAME = "profile"
CACHE_NAMESPACE = "profile"

EXPOSURE: dict[str, frozenset[str]] = {}


def configured(settings: Settings) -> bool:
    return settings.profile_path.exists()


class ProfileSource:
    NAME: ClassVar[str] = NAME
    cache_namespace: ClassVar[str] = CACHE_NAMESPACE

    def __init__(self, settings: Settings, cache: CacheStore):
        self.settings = settings
        self.cache = cache
        self.path = Path(settings.profile_path)
        self.tz = settings.tz
        self._stamp: tuple[int, int] | None = None
        self._loaded: P.Profile | None = None
        self._errors: list[str] = []
        self._present = False
        self.reload()

    def _stat(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def reload(self) -> None:
        stamp = self._stat()
        self._present = stamp is not None
        if stamp is None:
            self._stamp = None
            self._errors = []
            return
        if stamp == self._stamp:
            return
        self._stamp = stamp
        profile, errors = P.read(self.path)
        self._errors = errors
        if profile is not None:
            self._loaded = profile

    def current(self) -> P.Profile | None:
        self.reload()
        return self._loaded

    def intent(self) -> P.Intent | None:
        profile = self.current()
        return profile.intent if profile else None

    def regimen_plans(self) -> list[dict[str, Any]]:
        return P.regimen_plans(self.current())

    def health(self) -> dict[str, Any]:
        """Presence/validity for callers that must not treat stale data as current."""
        self.reload()
        return {
            "present": self._present,
            "valid": (not self._errors) if self._present else None,
            "stale": bool(self._errors) and self._loaded is not None,
            "error": self._errors[0] if self._errors else None,
        }

    def refresh(self) -> int:
        return 0

    async def close(self) -> None:
        return None

    async def status(self) -> dict[str, Any]:
        self.reload()
        return {
            "source": self.NAME,
            "ok": self._present and not self._errors,
            "path": str(self.path),
            "present": self._present,
            "error": self._errors[0] if self._errors else None,
            "cache_age_hours": None,
        }

    async def state(self) -> dict[str, Any]:
        """The `get_status.profile` block: presence, age and validity, never content."""
        self.reload()
        today = date.fromisoformat(today_local(self.tz))
        out: dict[str, Any] = {
            "path": str(self.path),
            "present": self._present,
            "updated": None,
            "oldest_section": None,
            "oldest_section_age_days": None,
            "valid": None,
            "error": self._errors[0] if self._errors else None,
        }
        if not self._present:
            return out
        out["valid"] = not self._errors
        profile = self._loaded
        if profile is None:
            return out
        out["updated"] = profile.updated.isoformat()
        name, updated = P.oldest_section(profile)
        out["oldest_section"] = name
        out["oldest_section_age_days"] = P.age_days(updated, today)
        return out

    async def block(self, section: str | None = None) -> dict[str, Any]:
        """The profile half of `get_profile`: sections, ages and validity."""
        self.reload()
        today = date.fromisoformat(today_local(self.tz))
        out: dict[str, Any] = {
            "path": str(self.path),
            "present": self._present,
            "valid": (not self._errors) if self._present else None,
            "error": self._errors[0] if self._errors else None,
            "sections": {},
        }
        profile = self._loaded
        if profile is None:
            return out
        out["updated"] = profile.updated.isoformat()
        out["age_days"] = P.age_days(profile.updated, today)
        names = (section,) if section else (*P.SECTIONS, "notes")
        for name in names:
            value = profile.section(name)
            if value is None:
                continue
            if name != "notes":
                value = {**value, "age_days": P.age_days(value.get("updated"), today)}
            out["sections"][name] = value
        if section and not out["sections"]:
            out["note"] = f"{section} is not present in profile.yaml"
        return out

    def tools(self) -> list[Any]:
        return []


def build(settings: Settings, cache: CacheStore) -> ProfileSource:
    return ProfileSource(settings, cache)
