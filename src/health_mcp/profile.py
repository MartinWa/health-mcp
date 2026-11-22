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
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

BACKUP_KEEP = 10
EVENT_COLUMNS = ("date", "time", "kind", "substance", "dose", "unit", "reason", "notes")
EVENT_KINDS = ("medication", "supplement", "symptom", "note", "measurement")
DOSE_KINDS = frozenset({"medication", "supplement"})
LEGACY_MEDICINE_COLUMNS = ("date", "substance", "dosage", "unit", "time")
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


class Medication(_Model):
    substance: str
    dose: float | None = None
    unit: str | None = None
    schedule: str | None = None
    reason: str | None = None
    prescriber: str | None = None


class Supplement(_Model):
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
            f"deload every {program.deload_every_weeks}w" if program.deload_every_weeks else None,
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
        date.fromisoformat(row["date"])
    except ValueError:
        return _row_error(number, "date is not YYYY-MM-DD")
    kind = str(row["kind"]).lower()
    if kind not in EVENT_KINDS:
        return _row_error(number, f"kind is not one of {', '.join(EVENT_KINDS)}")
    row["kind"] = kind
    if row["time"]:
        try:
            datetime.strptime(row["time"], "%H:%M")
        except ValueError:
            return _row_error(number, "time is not HH:MM")
    if kind in DOSE_KINDS and not row["substance"]:
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


def _event_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_cell(row.get(name)) for name in EVENT_COLUMNS)


def _read_legacy_medicine(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        with source.open(encoding="utf-8-sig", newline="") as handle:
            return _parse_legacy_medicine(source, handle)
    except FileNotFoundError as exc:
        raise ProfileError(source, [f"{source}: file not found"]) from exc
    except UnicodeDecodeError as exc:
        raise ProfileError(source, [f"{source}: not valid UTF-8 (byte {exc.start})"]) from exc
    except csv.Error as exc:
        raise ProfileError(source, [f"{source}: CSV parse error: {exc}"]) from exc
    except OSError as exc:
        raise ProfileError(source, [f"{source}: {exc}"]) from exc


def _parse_legacy_medicine(source: Path, handle: Any) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    imported: list[dict[str, Any]] = []
    reader = csv.DictReader(handle)
    fields = {name.strip().lower(): name for name in reader.fieldnames or []}
    missing = [name for name in LEGACY_MEDICINE_COLUMNS if name not in fields]
    if missing:
        raise ProfileError(source, [f"{source}: missing column(s) {', '.join(missing)}"])
    for number, raw in enumerate(reader, start=2):
        row = {
            "date": (raw.get(fields["date"]) or "").strip(),
            "time": (raw.get(fields["time"]) or "").strip(),
            "kind": "medication",
            "substance": (raw.get(fields["substance"]) or "").strip(),
            "dose": (raw.get(fields["dosage"]) or "").strip(),
            "unit": (raw.get(fields["unit"]) or "").strip(),
            "reason": "",
            "notes": "",
        }
        problem = _validate_event(row, number)
        if problem:
            errors.append(problem)
            continue
        imported.append(row)
    return imported, errors


def migrate_medicine(
    legacy: str | Path, events_path: str | Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """One-way import of a legacy medicine.csv into events.csv (`kind: medication`)."""
    source = Path(legacy)
    imported, errors = _read_legacy_medicine(source)
    destination = Path(events_path)
    existing, existing_errors = read_events(destination)
    if existing_errors:
        # A header typo or a bad row would make read_events omit rows, so rewriting
        # the file would erase history. Refuse instead; the caller fixes the file.
        raise ProfileError(
            destination,
            ["refusing to rewrite events.csv: destination has errors", *existing_errors],
        )
    known = {_event_key(row) for row in existing}
    added = [row for row in imported if _event_key(row) not in known]
    if not dry_run:
        write_events(destination, [*existing, *added])
    return {
        "legacy": str(source),
        "events": str(destination),
        "imported": len(added),
        "skipped": len(imported) - len(added),
        "dry_run": dry_run,
        "errors": errors,
    }


# -- CLI ------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from .config import Settings

    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="python -m health_mcp.profile",
        description="Validate, show or import the host-authored profile. Never runs in the server.",
    )
    parser.add_argument(
        "--profile",
        default=str(settings.profile_path),
        help="profile.yaml path (default: PROFILE_DIR/profile.yaml)",
    )
    parser.add_argument(
        "--events",
        default=str(settings.events_csv_path),
        help="events.csv path (default: PROFILE_DIR/events.csv)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser(
        "validate", help="validate profile.yaml and exit non-zero on errors"
    )
    validate_cmd.add_argument("path", nargs="?", default=None)
    show_cmd = sub.add_parser("show", help="print the current profile for review")
    show_cmd.add_argument("path", nargs="?", default=None)
    migrate_cmd = sub.add_parser(
        "migrate-medicine", help="import a legacy medicine.csv into events.csv"
    )
    migrate_cmd.add_argument("path")
    migrate_cmd.add_argument("--dry-run", action="store_true")
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
    if args.command == "migrate-medicine":
        root = Path(settings.profile_dir).resolve()
        destination = Path(args.events).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            print(
                f"refusing to write outside the profile directory {root}: {destination}",
                file=sys.stderr,
            )
            return 2
        try:
            result = migrate_medicine(args.path, destination, dry_run=args.dry_run)
        except ProfileError as exc:
            for error in exc.errors:
                print(error, file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 1 if result["errors"] else 0
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
