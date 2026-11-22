import pytest

from tools import publish_release


class FakeClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("0.1.0", (0, 1, 0)),
        ("v1.2.3", None),
        ("1.2", None),
        ("1.2.3-rc1", None),
        ("01.2.3", None),
        ("", None),
    ],
)
def test_version_number_accepts_only_bare_stable_tags(value, expected):
    assert publish_release.version_number(value) == expected


def test_wait_for_ci_run_returns_when_ci_concludes(monkeypatch):
    runs = [{"conclusion": None}, {"conclusion": None}, {"conclusion": "success"}]
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: runs.pop(0))
    slept = []

    run = publish_release.wait_for_ci_run(
        "abc",
        wait_seconds=60,
        poll_seconds=5,
        sleep=slept.append,
        monotonic=FakeClock(0, 5, 10),
    )

    assert run == {"conclusion": "success"}
    assert slept == [5, 5]


def test_wait_for_ci_run_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: {"conclusion": None})
    slept = []

    run = publish_release.wait_for_ci_run(
        "abc",
        wait_seconds=60,
        poll_seconds=5,
        sleep=slept.append,
        monotonic=FakeClock(0, 60),
    )

    assert run == {"conclusion": None}
    assert slept == []


def test_wait_for_ci_run_tolerates_a_run_that_does_not_exist_yet(monkeypatch):
    runs = [None, {"conclusion": "success"}]
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: runs.pop(0))

    run = publish_release.wait_for_ci_run(
        "abc",
        wait_seconds=60,
        poll_seconds=5,
        sleep=lambda seconds: None,
        monotonic=FakeClock(0, 5),
    )

    assert run == {"conclusion": "success"}


def test_validate_source_rejects_ci_that_did_not_pass(monkeypatch):
    monkeypatch.setattr(publish_release, "read_command", lambda *arguments: "abc123")
    monkeypatch.setattr(
        publish_release,
        "wait_for_ci_run",
        lambda commit: {
            "conclusion": "failure",
            "headSha": "abc123",
            "headBranch": "main",
            "event": "push",
        },
    )

    with pytest.raises(ValueError, match="successful CI push run on main"):
        publish_release.validate_source("1.0.0", event_name="push", tag_created="true")


def test_validate_source_rejects_a_moved_tag(monkeypatch):
    def read_command(*arguments):
        if arguments[:2] == ("git", "rev-parse") and "HEAD" in arguments:
            return "def456"
        return "abc123"

    monkeypatch.setattr(publish_release, "read_command", read_command)

    with pytest.raises(ValueError, match="no longer points at the triggering commit"):
        publish_release.validate_source("1.0.0", event_name="push", tag_created="true")


def test_validate_source_rejects_an_existing_tag(monkeypatch):
    monkeypatch.setattr(publish_release, "read_command", lambda *arguments: "abc123")

    with pytest.raises(ValueError, match="do not move or overwrite"):
        publish_release.validate_source("1.0.0", event_name="push", tag_created="false")


def test_validate_source_requires_the_expected_ci_jobs(monkeypatch):
    def read_command(*arguments):
        if arguments[:2] == ("gh", "run") and "list" in arguments:
            return (
                '[{"databaseId": 7, "conclusion": "success", "headSha": "abc123",'
                ' "headBranch": "main", "event": "push"}]'
            )
        if arguments[:3] == ("gh", "run", "view"):
            return '{"jobs": [{"name": "Checks (Linux)", "conclusion": "success"}]}'
        return "abc123"

    monkeypatch.setattr(publish_release, "read_command", read_command)

    with pytest.raises(ValueError, match="lacks successful application or container checks"):
        publish_release.validate_source("1.0.0", event_name="push", tag_created="true")


def test_release_plan_creates_a_latest_release_for_a_first_tag():
    arguments, latest = publish_release.release_plan("1.0.0", [])

    assert latest is True
    assert arguments[:4] == ["create", "1.0.0", "--verify-tag", "--title"]
    assert "--latest=true" in arguments


def test_release_plan_does_not_promote_a_lower_version():
    releases = [{"tag_name": "2.0.0", "draft": False, "prerelease": False}]

    arguments, latest = publish_release.release_plan("1.9.9", releases)

    assert latest is False
    assert "--latest=false" in arguments


def test_release_plan_publishes_an_existing_draft():
    releases = [{"tag_name": "1.2.0", "draft": True, "prerelease": False}]

    arguments, latest = publish_release.release_plan("1.2.0", releases)

    assert latest is True
    assert arguments[:2] == ["edit", "1.2.0"]
    assert "--draft=false" in arguments


def test_release_plan_keeps_an_already_published_release():
    releases = [{"tag_name": "1.2.0", "draft": False, "prerelease": False}]

    arguments, latest = publish_release.release_plan("1.2.0", releases)

    assert arguments == []
    assert latest is True


def test_release_plan_refuses_to_publish_a_prerelease_as_stable():
    releases = [{"tag_name": "1.2.0", "draft": False, "prerelease": True}]

    with pytest.raises(ValueError, match="already has a prerelease"):
        publish_release.release_plan("1.2.0", releases)
