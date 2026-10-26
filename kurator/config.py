"""Config loading: config.yaml for behaviour, .env for secrets. Fails loudly on start."""

from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Secrets(BaseSettings):
    # The repo's own .env wins over the one a folder up (course grading layout:
    # submissions/.env next to every cloned repo). pydantic-settings lets later files win.
    model_config = SettingsConfigDict(
        env_file=(ROOT.parent / ".env", ROOT / ".env"), extra="ignore"
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    portkey_api_key: str = Field(default="", alias="PORTKEY_API_KEY")
    google_sa_json: str = Field(default="", alias="GOOGLE_SA_JSON")


class Timeouts(BaseModel):
    ask_lesson_after_min: int = 15
    assignment_expected_after_lesson_h: int
    nudge_tutor_after_h: int
    assignment_missing_after_h: int
    solution_due_before_next_lesson_h: int
    attempt2_due_after_h: int
    nudge_child_before_due_h: list[int]
    batch_idle_close_min: int
    batch_max_items: int
    media_group_wait_sec: int
    supplement_window_min: int
    regrade_window_min: int
    graded_auto_report_h: int
    stuck_close_after_days: int
    missing_close_after_days: int
    grading_timeout_min: int
    schedule_horizon_days: int


class LLMProvider(BaseModel):
    provider: str = "anthropic"

    @field_validator("provider")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in ("anthropic", "gemini", "portkey"):
            raise ValueError("llm.provider must be 'anthropic', 'gemini' or 'portkey'")
        return v


class Models(BaseModel):
    vision: str
    solver: str
    grader: str
    matcher: str
    hint: str = ""  # empty — uses grader


class Hints(BaseModel):
    max_per_homework: int = 3  # per homework, regardless of task count
    max_words: int = 60  # longer than that is feedback, not a hint


class Grading(BaseModel):
    independent_passes: int
    min_confidence_no_human_review: float
    max_solution_attempts: int
    max_retries: int
    rubric_version: str
    call_timeout_sec: float = 90
    total_timeout_min: float = 8


class Limits(BaseModel):
    daily_usd: float
    sandbox_daily_usd: float = 0.5


class Sandbox(BaseModel):
    enabled: bool = False
    dir: str = "data/sandboxes"
    subject: str = "math"


class Reports(BaseModel):
    weekly_cron_local_parent: str
    transcript_preview_chars: int
    heightened_attention_until: str
    monthly_local_parent: str = "1 10:00"  # day of the month and time for the parent

    @property
    def monthly_day(self) -> int:
        return int(self.monthly_local_parent.split()[0])

    @property
    def monthly_time(self) -> time:
        hh, mm = self.monthly_local_parent.split()[1].split(":")
        return time(int(hh), int(mm))

    @property
    def weekly_dow(self) -> str:
        return self.weekly_cron_local_parent.split()[0].lower()

    @property
    def weekly_time(self) -> time:
        hh, mm = self.weekly_cron_local_parent.split()[1].split(":")
        return time(int(hh), int(mm))


class Sheets(BaseModel):
    enabled: bool
    spreadsheet_id: str


class Storage(BaseModel):
    db_path: str
    files_dir: str
    backup_dir: str
    backup_keep: int


class Config(BaseModel):
    timeouts: Timeouts
    quiet_hours_local: list[str]
    llm: LLMProvider = LLMProvider()
    models: Models
    prices: dict[str, tuple[float, ...]] = {}
    grading: Grading
    hints: Hints = Hints()
    limits: Limits
    reports: Reports
    sheets: Sheets
    sandbox: Sandbox = Sandbox()
    storage: Storage
    retention_days: int

    @field_validator("quiet_hours_local")
    @classmethod
    def _two_times(cls, v: list[str]) -> list[str]:
        if len(v) != 2:
            raise ValueError("quiet_hours_local must hold exactly two 'HH:MM' values")
        return v

    @property
    def quiet_start(self) -> time:
        hh, mm = self.quiet_hours_local[0].split(":")
        return time(int(hh), int(mm))

    @property
    def quiet_end(self) -> time:
        hh, mm = self.quiet_hours_local[1].split(":")
        return time(int(hh), int(mm))

    def abs_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(os.environ.get("KURATOR_CONFIG", ROOT / "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")
    with cfg_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def load_secrets() -> Secrets:
    return Secrets()  # type: ignore[call-arg]
