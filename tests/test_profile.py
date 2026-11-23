"""Profile schema, events loader and the host-side CLI. All fixtures are synthetic."""

from __future__ import annotations

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

EVENTS = """date,time,kind,substance,dose,unit,reason,notes,end_date,every_days
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


def test_intent_digest_is_empty_when_the_section_has_no_content(tmp_path):
    path = write(tmp_path, "empty.yaml", "updated: 2026-11-20\nintent:\n  updated: 2026-11-20\n")
    assert P.intent_digest(P.load(path).intent) == ""


def test_regimen_plans_flattens_medications_and_supplements(tmp_path):
    profile = P.load(write(tmp_path, "profile.yaml", VALID))
    plans = P.regimen_plans(profile)
    assert [row["kind"] for row in plans] == ["medication", "supplement"]
    assert plans[0]["substance"] == "example-med"
    assert P.regimen_plans(None) == []


def test_program_carries_frequency_and_progression(tmp_path):
    text = VALID.replace(
        "    deload_every_weeks: 5",
        "    deload_every_weeks: 5\n    sessions_per_week: 2\n    progression: add weight steadily",
    )
    profile = P.load(write(tmp_path, "program.yaml", text))
    assert profile.intent is not None and profile.intent.program is not None
    assert profile.intent.program.sessions_per_week == 2
    digest = P.intent_digest(profile.intent)
    assert "2 sessions/week" in digest
    assert "add weight steadily" in digest


@pytest.mark.parametrize("weeks", [0, 15])
def test_sessions_per_week_is_bounded(tmp_path, weeks):
    text = VALID.replace(
        "    deload_every_weeks: 5",
        f"    deload_every_weeks: 5\n    sessions_per_week: {weeks}",
    )
    profile, errors = P.read(write(tmp_path, "program.yaml", text))
    assert profile is None
    assert errors[0].startswith("intent.program.sessions_per_week:")


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
    assert "kind is not one of" in errors[0]
    assert "mystery" not in errors[0]
    no_columns = write(tmp_path, "narrow.csv", "date,kind\n2026-11-20,symptom\n")
    rows, errors = P.read_events(no_columns)
    assert rows == []
    assert "missing column" in errors[0]


def test_vaccination_rows_are_valid_events_that_need_a_substance(tmp_path):
    text = (
        EVENTS + "2026-11-22,,vaccination,influenza vaccine,1,dose,annual,left deltoid\n"
        "2026-11-23,,vaccination,,,,annual,no substance\n"
    )
    rows, errors = P.read_events(write(tmp_path, "events.csv", text))
    assert [row["kind"] for row in rows[-1:]] == ["vaccination"]
    assert rows[-1]["substance"] == "influenza vaccine"
    assert errors == ["row 7: vaccination rows need a substance"]
    assert "vaccination" not in P.DOSE_KINDS
    assert "vaccination" in P.SUBSTANCE_KINDS


def test_cli_validate_and_show(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PROFILE_DIR", str(tmp_path))
    path = write(tmp_path, "profile.yaml", VALID)
    assert P.main(["--profile", str(path), "validate"]) == 0
    assert "OK (updated 2026-11-20)" in capsys.readouterr().out

    bad = write(tmp_path, "broken.yaml", "updated: nope\n")
    assert P.main(["--profile", str(bad), "validate"]) == 1
    assert "updated" in capsys.readouterr().err

    assert P.main(["--profile", str(path), "show"]) == 0
    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["intent"]["program"]["phase"] == "hypertrophy"


def test_cli_validate_defaults_to_the_argument_path(tmp_path, capsys):
    path = write(tmp_path, "profile.yaml", VALID)
    assert P.main(["validate", str(path)]) == 0
    assert str(path) in capsys.readouterr().out


def test_yaml_errors_never_echo_the_source_text(tmp_path):
    broken = "updated: 2026-11-20\nconstraints:\n  injuries: [SYNTHETIC_PRIVATE_SENTINEL\n"
    path = write(tmp_path, "profile.yaml", broken)
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    message = caught.value.errors[0]
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in message
    assert "YAML parse error" in message
    assert "at line" in message and "column" in message
    _, errors = P.read(path)
    assert errors and "SYNTHETIC_PRIVATE_SENTINEL" not in errors[0]


def test_non_utf8_profile_is_a_sanitized_validation_error(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_bytes("updated: 2026-11-20\nnotes: naïve\n".encode("utf-16"))
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    assert "not valid UTF-8" in caught.value.errors[0]
    profile, errors = P.read(path)
    assert profile is None
    assert errors and "not valid UTF-8" in errors[0]


def test_non_utf8_events_are_reported_not_raised(tmp_path):
    path = tmp_path / "events.csv"
    path.write_bytes(EVENTS.encode("utf-16"))
    rows, errors = P.read_events(path)
    assert rows == []
    assert errors and "not valid UTF-8" in errors[0]


def test_event_row_errors_do_not_echo_values(tmp_path):
    text = (
        "date,time,kind,substance,dose,unit,reason,notes,end_date,every_days\n"
        "2026-13-01,08:00,medication,SYNTHETIC_PRIVATE_SENTINEL,50,µg,daily,\n"
    )
    rows, errors = P.read_events(write(tmp_path, "events.csv", text))
    assert rows == []
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in errors[0]
    assert errors[0].startswith("row 2: date is not")


def test_yaml_alias_error_does_not_echo_the_alias(tmp_path):
    path = write(tmp_path, "profile.yaml", "updated: *SYNTHETIC_PRIVATE_SENTINEL\n")
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    message = caught.value.errors[0]
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in message
    assert "ComposerError" in message


def test_yaml_unknown_tag_error_does_not_echo_the_tag(tmp_path):
    path = write(tmp_path, "profile.yaml", "updated: !SYNTHETIC_PRIVATE_SENTINEL 2026-11-20\n")
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    message = caught.value.errors[0]
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in message
    assert "ConstructorError" in message


def test_yaml_invalid_date_is_a_sanitized_value_error(tmp_path):
    path = write(tmp_path, "profile.yaml", "updated: 2026-13-01\n")
    with pytest.raises(P.ProfileError) as caught:
        P.load(path)
    assert caught.value.errors[0] == f"{path}: YAML value error"
    profile, errors = P.read(path)
    assert profile is None
    assert errors == [f"{path}: YAML value error"]


def test_optional_range_columns_are_validated(tmp_path):
    header = "date,time,kind,substance,dose,unit,reason,notes,end_date,every_days\n"
    good = (
        header
        + "2026-11-20,08:00,medication,example-med,50,µg,,,2026-11-27,7\n"
        + "2026-11-20,08:00,medication,example-med,50,µg,,,,\n"
    )
    rows, errors = P.read_events(write(tmp_path, "ok.csv", good))
    assert errors == []
    assert len(rows) == 2
    assert rows[0]["end_date"] == "2026-11-27"
    assert rows[0]["every_days"] == 7
    assert rows[1]["end_date"] == ""

    bad_end = header + "2026-11-20,,medication,example-med,50,µg,,,2026-11-19,\n"
    rows, errors = P.read_events(write(tmp_path, "bad-end.csv", bad_end))
    assert rows == []
    assert errors == ["row 2: end_date is before date"]

    bad_format = header + "2026-11-20,,medication,example-med,50,µg,,,27-11-2026,\n"
    rows, errors = P.read_events(write(tmp_path, "bad-fmt.csv", bad_format))
    assert errors == ["row 2: end_date is not YYYY-MM-DD"]

    no_window = header + "2026-11-20,,medication,example-med,50,µg,,,27-11-2026,7\n"
    _, errors = P.read_events(write(tmp_path, "no-window.csv", no_window))
    assert errors == ["row 2: end_date is not YYYY-MM-DD"]

    ongoing = header + "2026-11-20,,medication,example-med,50,µg,,,,7\n"
    rows, errors = P.read_events(write(tmp_path, "ongoing.csv", ongoing))
    assert errors == []
    assert rows[0]["end_date"] == ""
    assert rows[0]["every_days"] == 7

    ongoing_note = header + "2026-11-20,,note,,,,,a note,,7\n"
    _, errors = P.read_events(write(tmp_path, "ongoing-note.csv", ongoing_note))
    assert errors == [
        "row 2: every_days without end_date makes an ongoing series; only doses can be ongoing"
    ]

    out_of_range = header + "2026-11-20,,medication,example-med,50,µg,,,2026-11-27,367\n"
    _, errors = P.read_events(write(tmp_path, "freq.csv", out_of_range))
    assert errors == ["row 2: every_days must be 1-366"]


def test_a_file_without_the_range_columns_is_rejected(tmp_path):
    header = "date,time,kind,substance,dose,unit,reason,notes"
    text = header + "\n2026-11-20,,note,,,,,x\n"
    path = write(tmp_path, "short.csv", text)
    rows, errors = P.read_events(path)
    assert rows == []
    assert errors == [f"{path}: missing column(s) end_date, every_days"]


def test_write_events_keeps_the_range_columns(tmp_path):
    path = tmp_path / "events.csv"
    P.write_events(
        path,
        [
            {
                "date": "2026-11-20",
                "time": "08:00",
                "kind": "medication",
                "substance": "example-med",
                "dose": 50,
                "unit": "µg",
                "end_date": "2026-11-27",
                "every_days": 7,
            }
        ],
    )
    rows, errors = P.read_events(path)
    assert errors == []
    assert rows[0]["end_date"] == "2026-11-27"
    assert rows[0]["every_days"] == 7
    assert "end_date" in path.read_text(encoding="utf-8").splitlines()[0]


def test_regimen_intervals_are_optional_and_ordered(tmp_path):
    text = VALID.replace(
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily}",
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily,\n"
        '       started: "2026-01-01", stopped: "2026-11-01"}',
    )
    profile = P.load(write(tmp_path, "interval.yaml", text))
    assert profile.regimen is not None
    plan = P.regimen_plans(profile)[0]
    assert (plan["started"], plan["stopped"]) == ("2026-01-01", "2026-11-01")

    backwards = VALID.replace(
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily}",
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily,\n"
        '       started: "2026-05-01", stopped: "2026-01-01"}',
    )
    profile, errors = P.read(write(tmp_path, "backwards.yaml", backwards))
    assert profile is None
    assert errors[0].startswith("regimen.medications[0]")
    assert "stopped is before started" in errors[0]


def test_program_workout_types_are_normalized_and_reach_the_digest(tmp_path):
    text = VALID.replace(
        "    deload_every_weeks: 5",
        "    deload_every_weeks: 5\n    workout_types: ['Strength Training', 'elliptical']",
    )
    profile, errors = P.read(write(tmp_path, "program.yaml", text))
    assert errors == []
    assert profile is not None
    assert profile.intent is not None
    assert profile.intent.program is not None
    assert profile.intent.program.workout_types == ["strength_training", "elliptical"]
    digest = P.intent_digest(profile.intent)
    assert "counts strength_training, elliptical" in digest


def test_program_workout_types_reject_a_non_normalized_token(tmp_path):
    text = VALID.replace(
        "    deload_every_weeks: 5",
        "    deload_every_weeks: 5\n    workout_types: ['strength training!']",
    )
    profile, errors = P.read(write(tmp_path, "program.yaml", text))
    assert profile is None
    assert errors[0].startswith("intent.program.workout_types:")
