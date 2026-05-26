"""Configuration loader.

Reads a TOML config file from one of:
  1. $TARS_CONFIG env var (preferred for production via systemd)
  2. ~/.tars/config.toml (default — both Windows and Linux)

Validates with pydantic so typos and missing keys fail loudly at startup
rather than at the first cron firing at 5 AM.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    bot_token: str
    allowed_chat_ids: list[int] = Field(default_factory=list)


class OpenRouterConfig(BaseModel):
    api_key: str
    daily_cap_usd: float = 5.0


class OpenAIConfig(BaseModel):
    api_key: str
    daily_cap_usd: float = 2.0


class VoyageConfig(BaseModel):
    api_key: str


class PathsConfig(BaseModel):
    db: str
    vault: str
    backups: str


class NetworkConfig(BaseModel):
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8088


class TiersConfig(BaseModel):
    interactive_fast: str = "openai/gpt-5-mini"
    cron_default: str = "deepseek/deepseek-v3.2"
    ingest: str = "deepseek/deepseek-v3.2"
    web_research: str = "openai/gpt-5"


class Config(BaseModel):
    """Top-level TARS configuration. Strict — extra keys fail."""

    model_config = {"extra": "forbid"}

    timezone: str = "Asia/Jerusalem"
    telegram: TelegramConfig
    openrouter: OpenRouterConfig
    openai: OpenAIConfig
    voyage: VoyageConfig
    paths: PathsConfig
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    tiers: TiersConfig = Field(default_factory=TiersConfig)


def _default_config_path() -> Path:
    """Resolve ~/.tars/config.toml in a way that works on Windows and Linux."""
    return Path.home() / ".tars" / "config.toml"


def load_config(path: str | Path | None = None) -> Config:
    """Load TARS config. Raises FileNotFoundError or pydantic ValidationError loudly."""
    if path is None:
        env_path = os.environ.get("TARS_CONFIG")
        path = Path(env_path) if env_path else _default_config_path()
    else:
        path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"TARS config not found at {path}. "
            f"Set $TARS_CONFIG or place a config.toml at ~/.tars/config.toml. "
            f"See config.example.toml in the repo for the template."
        )

    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
