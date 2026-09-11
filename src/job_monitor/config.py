from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PrivateConfigError(ValueError):
    def __init__(self, code):
        super().__init__("Private configuration is missing or invalid; contents omitted")
        self.code = code


class Schedule(Strict):
    timezone: str
    delivery_times: list[str]

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("delivery_times")
    @classmethod
    def times(cls, value):
        from datetime import time

        for item in value:
            if time.fromisoformat(item).strftime("%H:%M") != item:
                raise ValueError("Use HH:MM times")
        if len(set(value)) != len(value):
            raise ValueError("Duplicate slots")
        return sorted(value)


class Operations(Strict):
    # Implementation knobs, not inferred candidate preferences.
    source_interval_hours: dict[str, float]
    max_pages_per_source: int = Field(ge=1, le=100)
    max_evaluations_per_run: int = Field(ge=1, le=500)
    scan_timeout_seconds: int = Field(ge=30, le=3600)
    source_timeout_seconds: int = Field(ge=5, le=300)
    max_cards_per_slot: int = Field(ge=1, le=50)
    research_ttl_hours: int = Field(ge=1)
    search_sources_per_run: int = Field(default=6, ge=1, le=100)
    outbound_companies_per_run: int = Field(default=1, ge=0, le=20)


class Preferences(Strict):
    version: str
    schedule: Schedule
    policy_text: str
    clarifications_text: str
    weights: dict[str, int]
    compensation_czk: dict[str, int]
    watchlists: dict[str, list[str]]
    discovery_queries: list[str]
    operations: Operations

    @model_validator(mode="after")
    def check_weights(self):
        required = {"responsibility", "seniority", "employment", "compensation", "company", "freshness"}
        if set(self.weights) != required or sum(self.weights.values()) != 100:
            raise ValueError("Expected six specified weights totalling 100")
        if min(self.weights.values()) < 0:
            raise ValueError("Negative weight")
        return self


class Source(Strict):
    id: str
    kind: Literal["greenhouse", "lever", "ashby", "jsonld", "search"]
    company: str | None = None
    board: str | None = None
    url: str | None = None
    tier: Literal["1", "2", "3"] = "1"
    enabled: bool = True
    query: str | None = None
    employer_source: bool = False


class Settings(Strict):
    private_config_json: str = Field(default="", repr=False)
    private_config_json_1: str = Field(default="", repr=False)
    private_config_json_2: str = Field(default="", repr=False)
    private_config_json_3: str = Field(default="", repr=False)
    private_config_json_4: str = Field(default="", repr=False)
    private_config_path: str = ""
    database_url: str = Field(default="", repr=False)
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_user_id: int = 0
    telegram_chat_id: int = 0
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = ""
    brave_api_key: str = Field(default="", repr=False)
    paid_apis_enabled: bool = False
    llm_calls_per_day: int = Field(default=0, ge=0)
    search_calls_per_day: int = Field(default=0, ge=0)
    llm_max_output_tokens: int = Field(default=5000, ge=1000, le=16000)
    config_path: str = "config/preferences.yaml"
    profile_path: str = "config/candidate.yaml"
    sources_path: str = "config/sources.yaml"
    notion_api_key: str = Field(default="", repr=False)
    notion_sync_enabled: bool = False
    notion_config_path: str = "config/notion.yaml"

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            **{k: os.environ[k.upper()] for k in cls.model_fields if os.environ.get(k.upper(), "") != ""}
        )

    def require_database(self):
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
            raise ValueError("DATABASE_URL must point to PostgreSQL; configure .env")

    @property
    def private_config_parts(self):
        return [getattr(self, f"private_config_json_{i}") for i in range(1, 5)]

    @property
    def has_private_config(self):
        return bool(self.private_config_json or self.private_config_path or any(self.private_config_parts))


def read_yaml(path: str):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def config_section(settings: Settings, section: str, fallback_path: str):
    if not settings.has_private_config:
        return read_yaml(fallback_path)
    try:
        parts = settings.private_config_parts
        if any(parts):
            # Reject ambiguous configuration and missing parts; never fall back to public defaults.
            if settings.private_config_json:
                raise PrivateConfigError("conflicting_variables")
            last = max(i for i, part in enumerate(parts) if part)
            if not all(parts[:last + 1]):
                raise PrivateConfigError("missing_part")
            raw = "".join(parts)
        else:
            raw = settings.private_config_json or Path(settings.private_config_path).read_text(encoding="utf-8")
        bundle = json.loads(raw)
        required = {"preferences", "candidate", "sources", "notion"}
        if not isinstance(bundle, dict) or set(bundle) != required:
            raise PrivateConfigError("invalid_sections")
        if not all(isinstance(bundle[key], dict) for key in required):
            raise PrivateConfigError("invalid_sections")
        return bundle[section]
    except PrivateConfigError:
        raise
    except json.JSONDecodeError:
        raise PrivateConfigError("invalid_json") from None
    except OSError:
        raise PrivateConfigError("unreadable_file") from None
    except (ValueError, TypeError, OSError):
        raise PrivateConfigError("invalid_sections") from None


def load_profile(settings: Settings):
    return config_section(settings, "candidate", settings.profile_path)


def load_preferences(settings: Settings) -> Preferences:
    return Preferences.model_validate(config_section(settings, "preferences", settings.config_path))


def fingerprint(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_sources(settings: Settings) -> list[Source]:
    sources = [
        Source.model_validate(s)
        for s in config_section(settings, "sources", settings.sources_path)["sources"]
    ]
    if len({s.id for s in sources}) != len(sources):
        raise ValueError("Duplicate source IDs")
    return sources
