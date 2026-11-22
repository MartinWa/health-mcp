"""Typed configuration — env only, no scattered os.getenv."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    ow_api_key: SecretStr | None = None
    ow_user_id: str | None = None
    ow_base_url: str | None = None

    strong_api_url: str | None = None
    strong_api_token: SecretStr | None = None
    strong_cache_ttl_hours: int = 6
    strong_owner_id: str | None = None

    cache_dir: str = "data/cache"
    cache_ttl_hours: int = 24
    profile_dir: str = "data/profile"

    sources: str | None = None
    tz: str = "Europe/Stockholm"

    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_bearer_token: SecretStr | None = None
    mcp_bearer_tokens: str | None = None

    default_max_points: int = 200
    default_page_limit: int = 50
    log_level: str = "INFO"

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"Invalid timezone {value!r}") from exc
        return value

    @field_validator("mcp_path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("MCP_PATH must start with '/'")
        if value == "/healthz":
            raise ValueError("MCP_PATH must not be /healthz")
        return value

    @property
    def profile_path(self) -> Path:
        return Path(self.profile_dir) / "profile.yaml"

    @property
    def events_csv_path(self) -> Path:
        return Path(self.profile_dir) / "events.csv"

    def source_allowlist(self) -> list[str] | None:
        if not self.sources:
            return None
        names = [part.strip() for part in self.sources.split(",") if part.strip()]
        return names or None

    def bearer_digests(self) -> dict[str, str]:
        """Map consumer name -> sha256(token) hex. Empty means fail closed in HTTP mode."""
        raw = (self.mcp_bearer_tokens or "").strip()
        if not raw:
            token = self.mcp_bearer_token.get_secret_value() if self.mcp_bearer_token else ""
            return {"default": _sha256(token)} if token else {}
        digests: dict[str, str] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, sep, digest = entry.partition(":")
            name, digest = name.strip(), digest.strip()
            if not sep or not name or not _DIGEST.match(digest):
                raise ValueError(
                    "MCP_BEARER_TOKENS entries must be name:sha256(token) pairs "
                    "with a 64-character hex digest"
                )
            digests[name] = digest.lower()
        if not digests:
            raise ValueError("MCP_BEARER_TOKENS is set but contains no entries")
        return digests


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return _sha256(token)
