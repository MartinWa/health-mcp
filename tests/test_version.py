"""The reported version is the deployed one: the tag, or the packaged version locally."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from health_mcp import VERSION_ENV, __version__, _resolve_version

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
STABLE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.([0-9][0-9]*)")


def declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_release_tag_wins_over_package_metadata(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "9.8.7")
    assert _resolve_version() == "9.8.7"


def test_blank_tag_env_falls_back_to_package_metadata(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "  ")
    assert _resolve_version() == version("health-mcp")
    monkeypatch.delenv(VERSION_ENV)
    assert _resolve_version() == version("health-mcp")


def test_uninstalled_source_tree_reports_unknown(monkeypatch):
    monkeypatch.setenv(VERSION_ENV, "")

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("health_mcp.version", missing, raising=False)
    assert _resolve_version() == "0.0.0+unknown"


def test_local_version_matches_the_package_metadata():
    assert __version__ == version("health-mcp")


def test_tag_and_declared_version_use_the_same_shape():
    assert STABLE.fullmatch(declared_version())
