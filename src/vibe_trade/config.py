"""Configuration loading and validation via Pydantic."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerConfig(BaseModel):
    host: str = "127.0.0.1"
    paper_port: int = 7497
    live_port: int = 7496
    client_id: int = Field(default=1, ge=0)
    timeout: int = Field(default=30, ge=1, le=300)
    account: str = ""
    connect_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=2.0, gt=0, le=60)
    order_pacing_seconds: float = Field(default=0.05, ge=0, le=5.0)

    def get_port(self, mode: str) -> int:
        return self.paper_port if mode == "paper" else self.live_port


class UniverseConfig(BaseModel):
    source: Literal["sp500", "custom"] = "sp500"
    custom_symbols: list[str] = Field(default_factory=list)

    @field_validator("custom_symbols")
    @classmethod
    def symbols_uppercase(cls, v: list[str]) -> list[str]:
        return [s.upper().strip() for s in v if s.strip()]


class SchedulerConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=1, le=1440)
    market_open: str = "09:30"
    market_close: str = "16:00"
    timezone: str = "US/Eastern"
    trading_days: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"]
    )

    @field_validator("market_open", "market_close")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError(f"Time must be HH:MM format, got '{v}'")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"Invalid time: '{v}'")
        return v

    @field_validator("trading_days")
    @classmethod
    def validate_days(cls, v: list[str]) -> list[str]:
        valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        for day in v:
            if day.lower() not in valid:
                raise ValueError(f"Invalid trading day: '{day}'. Must be one of {valid}")
        return [d.lower() for d in v]


class RiskConfig(BaseModel):
    """V2 risk config — fixed-% sizing with a hard cap on open positions.

    Per locked spec (project_v2_next_sessions.md memory):
    - 1.8% of net_liquidation per BUY
    - 50 max open positions
    - At cap = skip all new BUY signals that day; exits still run.
    """

    pct_per_position: float = Field(default=0.018, gt=0, le=1)
    max_open_positions: int = Field(default=50, ge=1, le=100)


VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class TelegramConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    chat_id: str = ""
    notify_on_trade: bool = True
    notify_on_error: bool = True
    daily_summary: bool = True


class GeneralConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    log_level: str = "INFO"
    log_file: str = "logs/vibe_trade.log"
    db_path: str = "data/vibe_trade.db"
    reports_dir: str = "reports"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_LOG_LEVELS:
            raise ValueError(f"Invalid log_level '{v}'. Must be one of {VALID_LOG_LEVELS}")
        return v


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIBE_TRADE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load config from TOML file, falling back to defaults.

    When no explicit path is given, the ``VIBE_TRADE_CONFIG`` env var is used
    if set, then ``config/config.toml``. The env var lets ``docker compose run``
    invocations that override the service command (e.g. ``config-check``) still
    find the mounted config — Hygiene #3 in docs/SESSION_H_FINDINGS.md.
    """
    if config_path is None:
        config_path = Path(os.environ.get("VIBE_TRADE_CONFIG", "config/config.toml"))
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return AppConfig(**data)

    return AppConfig()
