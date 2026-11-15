"""Load config.yaml (+ .env) into a typed Config object."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from job_agent.db import PROJECT_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class Preferences(_Section):
    role_types: list[str] = Field(default_factory=lambda: ["backend", "ai_ml", "fullstack"])
    target_titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=lambda: ["Dublin", "Ireland"])
    remote_regions: list[str] = Field(default_factory=lambda: ["Ireland", "EMEA", "Europe", "EU"])
    accept_unqualified_remote: bool = False
    min_salary: float | None = None


class Discover(_Section):
    watchlist_file: str = "data/companies.yaml"
    user_agent: str = "job-agent/0.1 (personal job search script)"
    request_interval_s: float = 1.0
    http_cache_hours: float = 6
    workday_search_terms: list[str] = Field(default_factory=lambda: ["software engineer"])
    workday_max_pages: int = 5


class Config(_Section):
    profile: dict[str, Any] = Field(default_factory=dict)
    preferences: Preferences = Field(default_factory=Preferences)
    filters: dict[str, Any] = Field(default_factory=dict)
    skills: dict[str, Any] = Field(default_factory=dict)
    scoring: dict[str, Any] = Field(default_factory=dict)
    discover: Discover = Field(default_factory=Discover)
    referrals: dict[str, Any] = Field(default_factory=dict)
    linkedin: dict[str, Any] = Field(default_factory=dict)
    gmail: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)

    def path(self, rel: str) -> Path:
        """Resolve a config path relative to the project root."""
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
