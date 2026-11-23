"""The profile schema, its validator and the only sanctioned writer.

`profile.yaml` and `events.csv` are data the sources do not own: goals,
constraints, regimen and the event log are authored on the host. The MCP server
only reads them, and `python -m health_mcp.profile` is the only code path that
writes, always under `PROFILE_DIR`. Anything that writes here backs the previous
file up first (`<name>.bak-<timestamp>`, newest 10 kept).

Dates in the files are local calendar dates (`YYYY-MM-DD`), like every date that
crosses the tool boundary.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

BACKUP_KEEP = 10
EVENT_COLUMNS = (
    "date",
    "time",
    "kind",
    "substance",
    "dose",
    "unit",
    "reason",
    "notes",
    "end_date",
    "every_days",
)
EVENT_KINDS = ("medication", "supplement", "vaccination", "symptom", "note", "measurement")
DOSE_KINDS = frozenset({"medication", "supplement"})
SUBSTANCE_KINDS = DOSE_KINDS | {"vaccination"}
MAX_EVERY_DAYS = 366
SECTIONS = ("intent", "constraints", "regimen")


class ProfileError(RuntimeError):
    """A profile or event file could not be read, with dotted field paths."""

    def __init__(self, path: Path, errors: Sequence[str]):
        self.path = path
        self.errors = list(errors) or [f"{path}: unreadable"]
        suffix = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        super().__init__(self.errors[0] + suffix)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Goal(_Model):
    metric: str
    target: float
    unit: str | None = None
    by: date | None = None


class Program(_Model):
    name: str
    phase: str | None = None
    started: date | None = None
    deload_every_weeks: Annotated[int | None, Field(ge=1, le=52)] = None
    sessions_per_week: Annotated[int | None, Field(ge=1, le=14)] = None
    progression: str | None = None
    workout_types: Annotated[list[str] | None, Field(max_length=12)] = None

    @field_validator("workout_types")
    @classmethod
    def _normalize_workout_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            token = str(item).strip().lower().replace(" ", "_")
            if not token:
                raise ValueError("workout_types entries must not be empty")
            if not re.fullmatch(r"[a-z0-9_]+", token):
                raise ValueError(
                    f"workout_types entry {item!r} must be a normalized workout type "
                    "such as strength_training"
                )
            if token not in cleaned:
                cleaned.append(token)
        return cleaned


class Intent(_Model):
    updated: date
    goals: list[Goal] = []
    program: Program | None = None
    notes: str | None = None


class Condition(_Model):
    name: str
    since: int | str | None = None
    status: str | None = None
    note: str | None = None


class Injury(_Model):
    area: str
    since: str | None = None
    limits: str | None = None


class Allergy(_Model):
    substance: str
    reaction: str | None = None


class Constraints(_Model):
    updated: date
    conditions: list[Condition] = []
    injuries: list[Injury] = []
    allergies: list[Allergy] = []


class _Interval(_Model):
    started: date | None = None
    stopped: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> _Interval:
        if self.started and self.stopped and self.stopped < self.started:
            raise ValueError("stopped is before started")
        return self


class Medication(_Interval):
    substance: str
    dose: float | None = None
    unit: str | None = None
    schedule: str | None = None
    reason: str | None = None
    prescriber: str | None = None


class Supplement(_Interval):
    substance: str
    dose: float | None = None
    unit: str | None = None
    schedule: str | None = None


class Regimen(_Model):
    updated: date
    medications: list[Medication] = []
    supplements: list[Supplement] = []


class Profile(_Model):
    updated: date
    intent: Intent | None = None
    constraints: Constraints | None = None
    regimen: Regimen | None = None
    notes: str | None = None

    def section(self, name: str) -> Mapping[str, Any] | None:
        if name == "notes":
            return {"text": self.notes} if self.notes else None
        value = getattr(self, name, None)
        return value.model_dump(mode="json", exclude_none=True) if value else None


def dotted_errors(errors: Iterable[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for error in errors:
        path = ""
        for part in error.get("loc") or ():
            if isinstance(part, int):
                path += f"[{part}]"
            else:
                path = f"{path}.{part}" if path else str(part)
        out.append(f"{path or '<root>'}: {error.get('msg')}")
    return out


def _yaml_error(target: Path, exc: yaml.YAMLError) -> str:
    # PyYAML's `problem` quotes the source: an undefined alias, an unknown tag or
    # a scalar. Only the error class and position are safe to return.
    kind = type(exc).__name__
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return (
            f"{target}: YAML parse error ({kind}) at line {mark.line + 1}, column {mark.column + 1}"
        )
    return f"{target}: YAML parse error ({kind})"


def load(path: str | Path) -> Profile:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError(target, [f"{target}: file not found"]) from exc
    except UnicodeDecodeError as exc:
        raise ProfileError(target, [f"{target}: not valid UTF-8 (byte {exc.start})"]) from exc
    except OSError as exc:
        raise ProfileError(target, [f"{target}: {exc}"]) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(target, [_yaml_error(target, exc)]) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        # safe_load constructs timestamps/numbers itself: `updated: 2026-13-01`
        # raises ValueError before schema validation. Treat it as a bad value.
        raise ProfileError(target, [f"{target}: YAML value error"]) from exc
    if not isinstance(raw, dict):
        raise ProfileError(target, [f"{target}: expected a YAML mapping"])
    try:
        return Profile.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(target, dotted_errors(exc.errors())) from exc


def read(path: str | Path) -> tuple[Profile | None, list[str]]:
    """Fail-soft load: the last valid copy stays in use when a file is broken."""
    try:
        return load(path), []
    except ProfileError as exc:
        return None, exc.errors


def dump(profile: Profile) -> str:
    return yaml.safe_dump(
        profile.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )


def backup(path: str | Path) -> Path | None:
    """Copy a file next to itself, keep the newest BACKUP_KEEP backups."""
    target = Path(path)
    if not target.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    copy = target.with_name(f"{target.name}.bak-{stamp}")
    copy.write_bytes(target.read_bytes())
    backups = sorted(target.parent.glob(f"{target.name}.bak-*"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return copy


def save(profile: Profile, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup(target)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(dump(profile), encoding="utf-8")
    os.replace(temporary, target)
    return target


def age_days(updated: date | str | None, today: date) -> int | None:
    if updated is None:
        return None
    if isinstance(updated, str):
        try:
            updated = date.fromisoformat(updated)
        except ValueError:
            return None
    return (today - updated).days


def oldest_section(profile: Profile) -> tuple[str | None, date | None]:
    dated = [(name, getattr(profile, name).updated) for name in SECTIONS if getattr(profile, name)]
    if not dated:
        return None, None
    return min(dated, key=lambda item: item[1])


def state(path: str | Path, today: date | None = None) -> dict[str, Any]:
    """The `get_status.profile` block: presence, age and validity, never content."""
    target = Path(path)
    out: dict[str, Any] = {
        "path": str(target),
        "present": target.exists(),
        "updated": None,
        "oldest_section": None,
        "oldest_section_age_days": None,
        "valid": None,
        "error": None,
    }
    if not out["present"]:
        return out
    profile, errors = read(target)
    if profile is None:
        out["valid"] = False
        out["error"] = errors[0] if errors else "unreadable profile"
        return out
    out["valid"] = True
    out["updated"] = profile.updated.isoformat()
    name, updated = oldest_section(profile)
    out["oldest_section"] = name
    if today is not None:
        out["oldest_section_age_days"] = age_days(updated, today)
    return out


def intent_digest(intent: Intent | None, max_chars: int = 800) -> str:
    """The only profile text that may join the server instructions: intent, never constraints."""
    if intent is None:
        return ""
    lines: list[str] = []
    goals = [
        " ".join(
            part
            for part in (
                goal.metric,
                f"target {goal.target:g}",
                goal.unit,
                f"by {goal.by.isoformat()}" if goal.by else "",
            )
            if part
        )
        for goal in intent.goals
    ]
    if goals:
        lines.append("Goals: " + "; ".join(goals))
    if intent.program:
        program = intent.program
        parts = [
            program.name,
            program.phase,
            f"{program.sessions_per_week} sessions/week" if program.sessions_per_week else None,
            f"deload every {program.deload_every_weeks}w" if program.deload_every_weeks else None,
            program.progression,
            f"counts {', '.join(program.workout_types)}" if program.workout_types else None,
        ]
        lines.append("Program: " + ", ".join(part for part in parts if part))
    if intent.notes:
        lines.append(f"Notes: {intent.notes}")
    if not lines:
        return ""
    header = "Profile intent (authored by the user, not a clinical record):"
    return "\n".join([header, *lines])[:max_chars]


def regimen_plans(profile: Profile | None) -> list[dict[str, Any]]:
    if profile is None or profile.regimen is None:
        return []
    plans: list[dict[str, Any]] = []
    for kind, rows in (
        ("medication", profile.regimen.medications),
        ("supplement", profile.regimen.supplements),
    ):
        for row in rows:
            plans.append(
                {
                    "kind": kind,
                    "substance": row.substance,
                    "dose": row.dose,
                    "unit": row.unit,
                    "schedule": row.schedule,
                    "started": row.started.isoformat() if row.started else None,
                    "stopped": row.stopped.isoformat() if row.stopped else None,
                }
            )
    return plans


# -- events.csv -----------------------------------------------------------


def _row_error(number: int, message: str) -> str:
    return f"row {number}: {message}"


def read_events(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read the event log. Returns rows and row errors; a missing file is empty."""
    target = Path(path)
    try:
        with target.open(encoding="utf-8-sig", newline="") as handle:
            return _read_event_rows(target, handle)
    except FileNotFoundError:
        return [], []
    except UnicodeDecodeError as exc:
        return [], [f"{target}: not valid UTF-8 (byte {exc.start})"]
    except csv.Error as exc:
        return [], [f"{target}: CSV parse error: {exc}"]
    except OSError as exc:
        return [], [f"{target}: {exc}"]


def _read_event_rows(target: Path, handle: Any) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    reader = csv.DictReader(handle)
    header = [name.strip() for name in reader.fieldnames or []]
    missing = [name for name in EVENT_COLUMNS if name not in header]
    if missing:
        return [], [f"{target}: missing column(s) {', '.join(missing)}"]
    for number, raw in enumerate(reader, start=2):
        row = {name: (raw.get(name) or "").strip() for name in EVENT_COLUMNS}
        if not any(row.values()):
            continue
        problem = _validate_event(row, number)
        if problem:
            errors.append(problem)
            continue
        rows.append(row)
    return rows, errors


def _validate_event(row: dict[str, Any], number: int) -> str | None:
    # Messages carry the row number and field only: event values are private data.
    try:
        day = date.fromisoformat(row["date"])
    except ValueError:
        return _row_error(number, "date is not YYYY-MM-DD")
    end_date = str(row.get("end_date") or "").strip()
    if end_date:
        try:
            covered_to = date.fromisoformat(end_date)
        except ValueError:
            return _row_error(number, "end_date is not YYYY-MM-DD")
        if covered_to < day:
            return _row_error(number, "end_date is before date")
    every_text = str(row.get("every_days") or "").strip()
    if every_text:
        try:
            every = int(every_text)
        except ValueError:
            return _row_error(number, "every_days is not a whole number")
        if not 1 <= every <= MAX_EVERY_DAYS:
            return _row_error(number, f"every_days must be 1-{MAX_EVERY_DAYS}")
        row["every_days"] = every
    kind = str(row["kind"]).lower()
    if kind not in EVENT_KINDS:
        return _row_error(number, f"kind is not one of {', '.join(EVENT_KINDS)}")
    row["kind"] = kind
    if every_text and not end_date and kind not in DOSE_KINDS:
        return _row_error(
            number, "every_days without end_date makes an ongoing series; only doses can be ongoing"
        )
    if row["time"]:
        try:
            datetime.strptime(row["time"], "%H:%M")
        except ValueError:
            return _row_error(number, "time is not HH:MM")
    if kind in SUBSTANCE_KINDS and not row["substance"]:
        return _row_error(number, f"{kind} rows need a substance")
    if row["dose"]:
        try:
            row["dose"] = float(row["dose"])
        except ValueError:
            return _row_error(number, "dose is not a number")
    return None


def write_events(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup(target)
    ordered = sorted(rows, key=lambda row: (str(row.get("date") or ""), str(row.get("time") or "")))
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EVENT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow({name: _cell(row.get(name)) for name in EVENT_COLUMNS})
    os.replace(temporary, target)
    return target


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# -- CLI ------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from .config import Settings

    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="python -m health_mcp.profile",
        description="Validate or show the host-authored profile. Never runs in the server.",
    )
    parser.add_argument(
        "--profile",
        default=str(settings.profile_path),
        help="profile.yaml path (default: PROFILE_DIR/profile.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser(
        "validate", help="validate profile.yaml and exit non-zero on errors"
    )
    validate_cmd.add_argument("path", nargs="?", default=None)
    show_cmd = sub.add_parser("show", help="print the current profile for review")
    show_cmd.add_argument("path", nargs="?", default=None)
    args = parser.parse_args(argv)

    profile_path = Path(args.path) if getattr(args, "path", None) else Path(args.profile)
    if args.command == "validate":
        try:
            profile = load(profile_path)
        except ProfileError as exc:
            for error in exc.errors:
                print(error, file=sys.stderr)
            return 1
        print(f"{profile_path}: OK (updated {profile.updated.isoformat()})")
        return 0
    if args.command == "show":
        try:
            profile = load(profile_path)
        except ProfileError as exc:
            for error in exc.errors:
                print(error, file=sys.stderr)
            return 1
        print(dump(profile), end="")
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
