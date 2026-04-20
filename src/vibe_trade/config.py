"""Configuration loading and validation via Pydantic."""

from __future__ import annotations

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


class TrailingStopConfig(BaseModel):
    enabled: bool = True
    method: Literal["atr", "percentage"] = "atr"
    atr_multiplier: float = Field(default=1.5, gt=0)
    atr_period: int = Field(default=14, ge=2, le=100)
    percentage: float = Field(default=3.0, gt=0, le=50)


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)
    max_open_positions: int = Field(default=5, ge=1, le=100)
    max_portfolio_exposure_pct: float = Field(default=80.0, gt=0, le=100)
    max_single_stock_pct: float = Field(default=20.0, gt=0, le=100)
    trailing_stop: TrailingStopConfig = Field(default_factory=TrailingStopConfig)


class MACrossoverConfig(BaseModel):
    fast_period: int = Field(default=20, ge=2, le=500)
    slow_period: int = Field(default=50, ge=2, le=500)

    @field_validator("slow_period")
    @classmethod
    def slow_gt_fast(cls, v: int, info) -> int:
        fast = info.data.get("fast_period")
        if fast is not None and v <= fast:
            raise ValueError(f"slow_period ({v}) must be greater than fast_period ({fast})")
        return v


class RSIMeanRevertConfig(BaseModel):
    rsi_period: int = Field(default=14, ge=2, le=100)
    oversold: int = Field(default=30, ge=1, le=49)
    overbought: int = Field(default=70, ge=51, le=99)

    @field_validator("overbought")
    @classmethod
    def overbought_gt_oversold(cls, v: int, info) -> int:
        oversold = info.data.get("oversold")
        if oversold is not None and v <= oversold:
            raise ValueError(f"overbought ({v}) must be greater than oversold ({oversold})")
        return v


VALID_TIMEFRAMES = {"1h", "4h", "1d"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class StrategyConfig(BaseModel):
    active: list[str] = Field(default_factory=lambda: ["ma_crossover"])
    timeframe: str = "1h"
    lookback_days: int = Field(default=60, ge=1, le=365)
    ma_crossover: MACrossoverConfig = Field(default_factory=MACrossoverConfig)
    rsi_mean_revert: RSIMeanRevertConfig = Field(default_factory=RSIMeanRevertConfig)

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, v: str) -> str:
        if v not in VALID_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe '{v}'. Must be one of {VALID_TIMEFRAMES}")
        return v

    @field_validator("active")
    @classmethod
    def validate_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one strategy must be active")
        return v


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
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load config from TOML file, falling back to defaults."""
    if config_path is None:
        config_path = Path("config/config.toml")
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return AppConfig(**data)

    return AppConfig()
