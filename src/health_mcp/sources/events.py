"""Events source: reads the host-authored events.csv (doses, symptoms, notes).

The file is local, so there is nothing to cache over the network; the reader
keeps the last good parse and re-reads when the file's mtime or size changes.
Rows that fail validation are reported in `status` and skipped, never fatal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from .. import analysis as A
from .. import profile as P
from ..config import Settings
from ..store import CacheStore
from ..utils import validate_range
from .base import DateStr

NAME = "events"
CACHE_NAMESPACE = "events"

EXPOSURE: dict[str, frozenset[str]] = {
    "get_medicine_logs": frozenset({"health-coach"}),
}


def configured(settings: Settings) -> bool:
    return settings.events_csv_path.exists()


class EventsSource:
    NAME: ClassVar[str] = NAME
    cache_namespace: ClassVar[str] = CACHE_NAMESPACE

    def __init__(self, settings: Settings, cache: CacheStore):
        self.settings = settings
        self.cache = cache
        self.path = Path(settings.events_csv_path)
        self._stamp: tuple[int, int] | None = None
        self._rows: list[dict[str, Any]] = []
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
        rows, errors = P.read_events(self.path)
        self._rows = rows
        self._errors = errors

    def rows(self) -> list[dict[str, Any]]:
        self.reload()
        return self._rows

    def _in_range(self, start: str, end: str) -> list[dict[str, Any]]:
        return [row for row in self.rows() if start <= str(row.get("date") or "") <= end]

    def doses(self, start: str, end: str) -> list[dict[str, Any]]:
        """Only medication/supplement rows: a symptom is never a dose."""
        return [row for row in self._in_range(start, end) if row.get("kind") in P.DOSE_KINDS]

    def symptoms(self, start: str, end: str) -> list[dict[str, Any]]:
        return [row for row in self._in_range(start, end) if row.get("kind") == "symptom"]

    def notes(self, start: str, end: str) -> list[dict[str, Any]]:
        return [row for row in self._in_range(start, end) if row.get("kind") == "note"]

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
            "rows": len(self._rows),
            "errors": self._errors[:5],
            "error": self._errors[0] if self._errors else None,
            "cache_age_hours": None,
        }

    async def get_medicine_logs(self, start_date: DateStr, end_date: DateStr) -> dict[str, Any]:
        """Dose/symptom/note summary per substance; symptoms are never counted as doses."""
        try:
            validate_range(start_date, end_date)
        except ValueError as exc:
            return {"error": str(exc)}
        rows = self._in_range(start_date, end_date)
        out = A.medicine_logs(rows, start_date, end_date)
        if not self._present:
            out["note"] = "events.csv is not present; nothing is logged for this period."
        if self._errors:
            out["row_errors"] = self._errors[:5]
        return out

    def tools(self) -> list[Any]:
        return [self.get_medicine_logs]


def build(settings: Settings, cache: CacheStore) -> EventsSource:
    return EventsSource(settings, cache)
