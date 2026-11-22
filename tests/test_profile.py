"""Profile schema, events loader and the host-side CLI. All fixtures are synthetic."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from health_mcp import profile as P

VALID = """
updated: 2026-11-20
intent:
  updated: 2026-11-20
  goals:
    - {metric: bodyweight, target: 78, unit: kg, by: 2027-03-01}
  program:
    name: "4-day upper/lower"
    phase: hypertrophy
    started: 2026-09-01
    deload_every_weeks: 5
  notes: "prefer morning sessions"
constraints:
  updated: 2026-05-01
  injuries:
    - {area: left_knee, since: 2025-04, limits: "no deep flexion"}
regimen:
  updated: 2026-11-01
  medications:
    - {substance: example-med, dose: 50, unit: µg, schedule: daily}
  supplements:
    - {substance: creatine, dose: 5, unit: g, schedule: daily}
notes: "synthetic fixture, not personal data"
"""

EVENTS = """date,time,kind,substance,dose,unit,reason,notes
2026-11-20,08:00,medication,example-med,50,µg,daily,
2026-11-20,08:00,supplement,creatine,5,g,daily,
2026-11-21,21:10,symptom,,,,,knee ache after squats
2026-11-21,09:00,note,,,,,felt good after warm-up
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_every_section(tmp_path):
    profile = P.load(write(tmp_path, "profile.yaml", VALID))
    assert profile.updated == date(2026, 11, 20)
    assert profile.intent is not None
    assert profile.intent.goals[0].metric == "bodyweight"
    assert profile.intent.program is not None
    assert profile.intent.program.deload_every_weeks == 5
    assert profile.constraints is not None
    assert profile.constraints.injuries[0].area == "left_knee"
    assert profile.regimen is not None
    assert profile.regimen.medications[0].substance == "example-med"
    assert profile.section("notes") == {"text": "synthetic fixture, not personal data"}


def test_validation_errors_use_dotted_paths(tmp_path):
    broken = VALID.replace("deload_every_weeks: 5", "deload_every_weeks: 0").replace(
        "until-2026", "until-2026"
    )
    path = write(tmp_path, "profile.yaml", broken)
    profile, errors = P.read(path)
    assert profile is None
    assert errors
    assert errors[0].startswith("intent.program.deload_every_weeks:")


def test_unknown_keys_are_rejected_with_a_path(tmp_path):
    path = write(tmp_path, "profile.yaml", VALID.replace("limits:", "limits_typo:"))
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    assert "constraints.injuries[0].limits_typo" in caught.value.errors[0]


def test_missing_and_malformed_files_are_errors(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(P.ProfileError, match="file not found"):
        P.load(missing)
    malformed = write(tmp_path, "broken.yaml", "intent: [unclosed\n")
    with pytest.raises(P.ProfileError, match="YAML"):
        P.load(malformed)
    assert P.read(missing) == (None, [f"{missing}: file not found"])


def test_dump_round_trips_through_load(tmp_path):
    profile = P.load(write(tmp_path, "profile.yaml", VALID))
    again = P.load(write(tmp_path, "again.yaml", P.dump(profile)))
    assert again == profile
    assert "updated: '2026-11-20'" in P.dump(profile) or "updated: 2026-11-20" in P.dump(profile)


def test_save_backs_up_and_keeps_the_last_ten(tmp_path):
    path = write(tmp_path, "profile.yaml", VALID)
    profile = P.load(path)
    for _ in range(12):
        P.save(profile, path)
    backups = sorted(tmp_path.glob("profile.yaml.bak-*"))
    assert len(backups) == P.BACKUP_KEEP
    assert P.load(path) == profile


def test_state_reports_presence_age_and_validity(tmp_path):
    path = write(tmp_path, "profile.yaml", VALID)
    block = P.state(path, today=date(2026, 11, 22))
    assert block["present"] is True
    assert block["valid"] is True
    assert block["updated"] == "2026-11-20"
    assert block["oldest_section"] == "constraints"
    assert block["oldest_section_age_days"] == (date(2026, 11, 22) - date(2026, 5, 1)).days
    broken = write(tmp_path, "broken.yaml", "updated: not-a-date\n")
    bad = P.state(broken, today=date(2026, 11, 22))
    assert bad["present"] is True
    assert bad["valid"] is False
    assert "updated" in bad["error"]
    absent = P.state(tmp_path / "absent.yaml", today=date(2026, 11, 22))
    assert absent["present"] is False
    assert absent["valid"] is None


def test_intent_digest_never_includes_constraints(tmp_path):
    profile = P.load(write(tmp_path, "profile.yaml", VALID))
    digest = P.intent_digest(profile.intent)
    assert "bodyweight" in digest
    assert "hypertrophy" in digest
    assert "left_knee" not in digest
    assert "example-med" not in digest
    assert P.intent_digest(None) == ""


def test_regimen_plans_flattens_medications_and_supplements(tmp_path):
    profile = P.load(write(tmp_path, "profile.yaml", VALID))
    plans = P.regimen_plans(profile)
    assert [row["kind"] for row in plans] == ["medication", "supplement"]
    assert plans[0]["substance"] == "example-med"
    assert P.regimen_plans(None) == []


def test_read_events_parses_and_reports_bad_rows(tmp_path):
    path = write(tmp_path, "events.csv", EVENTS + "not-a-date,,symptom,,,,,bad row\n")
    rows, errors = P.read_events(path)
    assert len(rows) == 4
    assert rows[0]["kind"] == "medication"
    assert rows[0]["dose"] == 50.0
    assert rows[2]["kind"] == "symptom"
    assert len(errors) == 1
    assert errors[0].startswith("row 6:")
    assert P.read_events(tmp_path / "absent.csv") == ([], [])


def test_read_events_rejects_unknown_kinds_and_missing_columns(tmp_path):
    bad_kind = EVENTS.replace("kind,substance", "kind,substance").replace("note,,,,", "mystery,,,,")
    rows, errors = P.read_events(write(tmp_path, "kinds.csv", bad_kind))
    assert len(rows) == 3
    assert "mystery" in errors[0]
    no_columns = write(tmp_path, "narrow.csv", "date,kind\n2026-11-20,symptom\n")
    rows, errors = P.read_events(no_columns)
    assert rows == []
    assert "missing column" in errors[0]


LEGACY = """Date,Substance,Dosage,Unit,Time
2026-11-19,example-med,50,µg,08:00
2026-11-20,example-med,50,µg,08:00
"""


def test_migrate_medicine_imports_once_and_keeps_existing_rows(tmp_path):
    events_path = write(tmp_path, "events.csv", EVENTS)
    legacy = write(tmp_path, "medicine.csv", LEGACY)
    first = P.migrate_medicine(legacy, events_path)
    assert first["imported"] == 2
    assert first["errors"] == []
    rows, _ = P.read_events(events_path)
    assert len(rows) == 6
    assert [row["kind"] for row in rows if row["date"] == "2026-11-19"] == ["medication"]
    assert any(path.name.startswith("events.csv.bak-") for path in tmp_path.iterdir())
    second = P.migrate_medicine(legacy, events_path)
    assert second["imported"] == 0
    assert second["skipped"] == 2


def test_migrate_medicine_dry_run_writes_nothing(tmp_path):
    events_path = tmp_path / "events.csv"
    legacy = write(tmp_path, "medicine.csv", LEGACY)
    out = P.migrate_medicine(legacy, events_path, dry_run=True)
    assert out["imported"] == 2
    assert out["dry_run"] is True
    assert not events_path.exists()


def test_migrate_medicine_requires_the_legacy_columns(tmp_path):
    legacy = write(tmp_path, "medicine.csv", "Date,Amount\n2026-11-19,50\n")
    with pytest.raises(P.ProfileError, match="missing column"):
        P.migrate_medicine(legacy, tmp_path / "events.csv")


def test_cli_validate_show_and_migrate(tmp_path, capsys):
    path = write(tmp_path, "profile.yaml", VALID)
    assert P.main(["--profile", str(path), "validate"]) == 0
    assert "OK (updated 2026-11-20)" in capsys.readouterr().out

    bad = write(tmp_path, "broken.yaml", "updated: nope\n")
    assert P.main(["--profile", str(bad), "validate"]) == 1
    assert "updated" in capsys.readouterr().err

    assert P.main(["--profile", str(path), "show"]) == 0
    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["intent"]["program"]["phase"] == "hypertrophy"

    events_path = tmp_path / "events.csv"
    legacy = write(tmp_path, "medicine.csv", LEGACY)
    code = P.main(
        [
            "--events",
            str(events_path),
            "migrate-medicine",
            str(legacy),
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["imported"] == 2
    assert result["events"] == str(events_path)


def test_cli_validate_defaults_to_the_argument_path(tmp_path, capsys):
    path = write(tmp_path, "profile.yaml", VALID)
    assert P.main(["validate", str(path)]) == 0
    assert str(path) in capsys.readouterr().out
