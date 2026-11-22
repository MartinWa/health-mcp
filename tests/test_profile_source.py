"""Profile/events sources, the profile composites and the instructions digest."""

from __future__ import annotations

import asyncio

import pytest
from helpers import make_settings, strong_settings
from test_profile import EVENTS, VALID

from health_mcp import sources as registry
from health_mcp.server import (
    compose_medication_adherence,
    compose_profile,
    compose_status,
    create_server,
)
from health_mcp.store import CacheStore


def run(coro):
    return asyncio.run(coro)


def write_profile(tmp_path, text=VALID, name="profile.yaml"):
    directory = tmp_path / "profile"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def write_events(tmp_path, text=EVENTS):
    directory = tmp_path / "profile"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "events.csv"
    path.write_text(text, encoding="utf-8")
    return path


def settings_for(tmp_path, **overrides):
    return make_settings(tmp_path, profile_dir=str(tmp_path / "profile"), **overrides)


def build(tmp_path, settings):
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    return registry.build_sources(settings, cache)


def test_profile_source_validates_and_reports_ages(tmp_path):
    write_profile(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    source = sources["profile"]

    block = run(source.block())
    assert block["present"] is True
    assert block["valid"] is True
    assert block["updated"] == "2026-11-20"
    assert block["sections"]["intent"]["program"]["phase"] == "hypertrophy"
    assert block["sections"]["notes"] == {"text": "synthetic fixture, not personal data"}
    assert isinstance(block["sections"]["constraints"]["age_days"], int)

    only = run(source.block("regimen"))
    assert set(only["sections"]) == {"regimen"}

    minimal = tmp_path / "minimal"
    minimal.mkdir()
    (minimal / "profile.yaml").write_text(
        "updated: 2026-11-20\nintent: {updated: 2026-11-20}\n", encoding="utf-8"
    )
    minimal_source = build(tmp_path, make_settings(tmp_path, profile_dir=str(minimal)))["profile"]
    absent = run(minimal_source.block("constraints"))
    assert absent["sections"] == {}
    assert "not present" in absent["note"]


def test_profile_source_reloads_on_change_without_restart(tmp_path):
    path = write_profile(tmp_path)
    settings = settings_for(tmp_path)
    source = build(tmp_path, settings)["profile"]
    assert run(source.block())["sections"]["intent"]["program"]["phase"] == "hypertrophy"

    path.write_text(VALID.replace("phase: hypertrophy", "phase: strength"), encoding="utf-8")
    assert run(source.block())["sections"]["intent"]["program"]["phase"] == "strength"


def test_profile_source_keeps_the_last_valid_copy(tmp_path):
    path = write_profile(tmp_path)
    settings = settings_for(tmp_path)
    source = build(tmp_path, settings)["profile"]
    assert run(source.state())["valid"] is True

    path.write_text("updated: not-a-date\n", encoding="utf-8")
    block = run(source.block())
    assert block["valid"] is False
    assert "updated" in block["error"]
    assert block["sections"]["intent"]["program"]["name"] == "4-day upper/lower"
    state = run(source.state())
    assert state["valid"] is False
    assert state["updated"] == "2026-11-20"

    path.unlink()
    absent = run(source.block())
    assert absent["present"] is False


def test_profile_source_is_not_configured_without_a_file(tmp_path):
    settings = settings_for(tmp_path)
    assert registry.configured_modules(settings) == []
    with pytest.raises(RuntimeError, match="No sources"):
        create_server(settings)


def test_events_source_counts_only_doses_and_reports_errors(tmp_path):
    write_profile(tmp_path)
    write_events(tmp_path)
    settings = settings_for(tmp_path)
    source = build(tmp_path, settings)["events"]

    doses = source.doses("2026-11-01", "2026-11-30")
    assert [row["kind"] for row in doses] == ["medication", "supplement"]
    assert source.symptoms("2026-11-01", "2026-11-30")[0]["notes"] == "knee ache after squats"
    assert source.notes("2026-11-01", "2026-11-30")[0]["notes"] == "felt good after warm-up"
    status = run(source.status())
    assert status["rows"] == 4
    assert status["ok"] is True and status["errors"] == []
    assert source.refresh() == 0


def test_compose_profile_merges_the_file_and_openwearables(tmp_path):
    write_profile(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)

    out = run(compose_profile(sources, settings))
    assert out["person"]["available"] is False
    assert "Open Wearables is not configured" in out["person"]["note"]
    assert out["profile"]["sections"]["intent"]["goals"][0]["metric"] == "bodyweight"
    assert out["profile"]["sections"]["constraints"]["injuries"][0]["area"] == "left_knee"

    filtered = run(compose_profile(sources, settings, "regimen"))
    assert set(filtered["profile"]["sections"]) == {"regimen"}


def test_get_profile_without_a_profile_file_is_a_clear_answer(tmp_path):
    settings = settings_for(tmp_path)
    out = run(compose_profile({"openwearables": _PersonSource()}, settings))
    assert out["profile"]["present"] is False
    assert "validate" in out["profile"]["note"]
    assert out["person"]["available"] is True


class _PersonSource:
    NAME = "openwearables"
    cache_namespace = "openwearables"

    async def person(self):
        return {"available": True, "birth_date": None, "missing": ["birth_date"]}


def test_medication_adherence_compares_regimen_and_doses(tmp_path):
    write_profile(tmp_path)
    write_events(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)

    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert out["period"]["days"] == 2
    assert out["n"] == 2
    assert out["expected"] == 4.0
    assert out["logged"] == 2
    assert out["adherence_pct"] == 50.0
    rows = {row["substance"]: row for row in out["items"]}
    assert rows["example-med"]["missing_days"] == 1
    assert rows["example-med"]["kind"] == "medication"
    assert "kind medication or supplement" in out["definition"]
    assert "50.0% of planned doses logged" in out["interpretation"]


def test_medication_adherence_needs_both_sources(tmp_path):
    write_profile(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert out["missing"] == ["events"]
    assert "error" in out


def test_medication_adherence_without_scheduled_rows_says_so(tmp_path):
    write_profile(tmp_path, VALID.replace("schedule: daily", "schedule: as needed"))
    write_events(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert out["n"] == 0
    assert [row["substance"] for row in out["unparsed_schedules"]] == ["example-med", "creatine"]
    assert "unparsed_schedules" in out["interpretation"]


def test_status_includes_the_profile_block(tmp_path):
    write_profile(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    out = run(compose_status(sources, cache, settings, 1))
    assert out["profile"]["present"] is True
    assert out["profile"]["valid"] is True
    assert out["profile"]["oldest_section"] == "constraints"
    assert out["profile"]["path"].endswith("profile.yaml")


def test_status_without_a_profile_reports_absence(tmp_path):
    settings = settings_for(tmp_path)
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    out = run(compose_status({}, cache, settings, 1))
    assert out["profile"]["present"] is False
    assert out["profile"]["valid"] is None


def test_broken_profile_still_starts_and_reports_the_error_path(tmp_path):
    write_profile(tmp_path, "updated: nope\nconstraints: {updated: 2026-01-01}\n")
    settings = settings_for(tmp_path)
    mcp = create_server(settings)
    assert mcp.name == "health"
    sources = build(tmp_path, settings)
    block = run(sources["profile"].block())
    assert block["valid"] is False
    assert "updated" in block["error"]


def test_instructions_carry_intent_only(tmp_path):
    write_profile(tmp_path)
    settings = settings_for(tmp_path)
    mcp = create_server(settings)
    instructions = mcp.instructions or ""
    assert "bodyweight" in instructions
    assert "hypertrophy" in instructions
    assert "left_knee" not in instructions
    assert "example-med" not in instructions
    assert "synthetic fixture" not in instructions


def test_profile_tools_are_registered_even_without_a_profile(tmp_path):
    settings = strong_settings(tmp_path)
    mcp = create_server(settings)
    names = {tool.name for tool in run(mcp.list_tools())}
    assert {"get_profile", "get_medication_adherence"} <= names


def test_adherence_refuses_an_unreadable_log(tmp_path):
    write_profile(tmp_path)
    events_path = write_events(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    events_path.write_text("date,time,kind,substance,dosage,unit,reason,notes\n", encoding="utf-8")
    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert "could not be parsed" in out["error"]
    assert out["events"]["valid"] is False


def test_adherence_refuses_after_the_log_is_deleted(tmp_path):
    write_profile(tmp_path)
    events_path = write_events(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    events_path.unlink()
    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert "not available" in out["error"]
    assert out["events"]["present"] is False


def test_adherence_refuses_an_invalid_profile(tmp_path):
    path = write_profile(tmp_path)
    write_events(tmp_path)
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    path.write_text("updated: not-a-date\n", encoding="utf-8")
    out = run(compose_medication_adherence(sources, "2026-11-20", "2026-11-21"))
    assert "profile is unavailable or invalid" in out["error"]
    assert out["profile"]["valid"] is False


def test_non_utf8_profile_does_not_prevent_startup(tmp_path):
    path = write_profile(tmp_path)
    path.write_bytes("updated: 2026-11-20\nnotes: naïve\n".encode("utf-16"))
    settings = settings_for(tmp_path)
    create_server(settings)
    block = run(build(tmp_path, settings)["profile"].block())
    assert block["valid"] is False
    assert "not valid UTF-8" in block["error"]


def test_non_utf8_events_are_reported_not_raised(tmp_path):
    write_profile(tmp_path)
    events_path = write_events(tmp_path)
    events_path.write_bytes(EVENTS.encode("utf-16"))
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    source = sources["events"]
    assert source.rows() == []
    assert run(source.status())["ok"] is False
    out = run(source.get_medicine_logs("2026-11-20", "2026-11-21"))
    assert "could not be parsed" in out["error"]


def test_medicine_logs_reports_absence_after_delete(tmp_path):
    write_profile(tmp_path)
    events_path = write_events(tmp_path)
    settings = settings_for(tmp_path)
    source = build(tmp_path, settings)["events"]
    events_path.unlink()
    out = run(source.get_medicine_logs("2026-11-20", "2026-11-21"))
    assert "not available" in out["error"]


def test_status_never_echoes_profile_content(tmp_path):
    import json

    write_profile(tmp_path, "updated: 2026-11-20\nconstraints: [SYNTHETIC_PRIVATE_SENTINEL\n")
    settings = settings_for(tmp_path)
    sources = build(tmp_path, settings)
    cache = CacheStore(tmp_path / "cache", registry.ttls(settings))
    out = run(compose_status(sources, cache, settings, 1))
    assert out["profile"]["valid"] is False
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in json.dumps(out)
    block = run(sources["profile"].block())
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in json.dumps(block)


def test_invalid_yaml_date_does_not_prevent_startup(tmp_path):
    write_profile(tmp_path, "updated: 2026-13-01\n")
    settings = settings_for(tmp_path)
    create_server(settings)
    block = run(build(tmp_path, settings)["profile"].block())
    assert block["valid"] is False
    assert "YAML value error" in block["error"]


def test_a_range_row_is_returned_for_every_period_it_overlaps(tmp_path):
    text = (
        "date,time,kind,substance,dose,unit,reason,notes,end_date,every_days\n"
        "2026-11-01,,medication,example-med,50,µg,,,2026-11-30,7\n"
        "2026-11-20,,symptom,,,,,knee ache,,\n"
    )
    write_events(tmp_path, text)
    source = build(tmp_path, settings_for(tmp_path))["events"]

    later = run(source.get_medicine_logs("2026-11-25", "2026-11-30"))
    assert later["events"]["valid"] is True
    assert later["doses"][0]["count"] == 1
    assert later["doses"][0]["last_date"] == "2026-11-29"

    earlier = run(source.get_medicine_logs("2026-11-01", "2026-11-07"))
    assert earlier["doses"][0]["count"] == 1
    assert earlier["doses"][0]["first_date"] == "2026-11-01"

    outside = run(source.get_medicine_logs("2026-12-01", "2026-12-07"))
    assert outside["n"] == 0


def test_adherence_end_to_end_with_a_plan_interval_and_a_range_row(tmp_path):
    profile = VALID.replace(
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily}",
        "- {substance: example-med, dose: 50, unit: µg, schedule: daily,\n"
        '       started: "2026-11-18", stopped: "2026-11-22"}',
    )
    write_profile(tmp_path, profile)
    write_events(
        tmp_path,
        "date,time,kind,substance,dose,unit,reason,notes,end_date,every_days\n"
        "2026-11-18,,medication,example-med,50,µg,,,2026-11-22,1\n",
    )
    out = run(
        compose_medication_adherence(
            build(tmp_path, settings_for(tmp_path)), "2026-11-16", "2026-11-30"
        )
    )
    item = next(row for row in out["items"] if row["substance"] == "example-med")
    assert (item["expected"], item["logged"]) == (5.0, 5)
    assert item["missing_days"] == 0
    assert item["intervals"] == [{"started": "2026-11-18", "stopped": "2026-11-22"}]
