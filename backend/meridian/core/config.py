from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (12-factor). No secrets in code."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    env: Literal["development", "test", "production"] = Field("development", alias="MERIDIAN_ENV")
    database_url: str = Field("postgresql+psycopg://meridian:meridian@localhost:5432/meridian", alias="DATABASE_URL")
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    jwt_secret: str = Field("dev-only-change-me", alias="JWT_SECRET")
    jwt_ttl_minutes: int = Field(720, alias="JWT_TTL_MINUTES")
    cio_username: str | None = Field(None, alias="CIO_USERNAME")
    cio_password: str | None = Field(None, alias="CIO_PASSWORD")

    # "simulated" uses the clearly-labeled simulated provider; "coinbase" uses the public read-only API.
    market_data_mode: Literal["simulated", "coinbase"] = Field("simulated", alias="MARKET_DATA_MODE")
    product_overrides: str = Field("", alias="MERIDIAN_PRODUCT_OVERRIDES")  # JSON {"GOLD": "GOL-..."}
    candle_granularity: str = Field("ONE_HOUR", alias="CANDLE_GRANULARITY")

    # Language model (research department + macro desk). No key -> those workers report UNAVAILABLE.
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    llm_model: str = Field("claude-sonnet-5-5", alias="MERIDIAN_LLM_MODEL")
    llm_daily_budget_usd: float = Field(3.0, alias="LLM_DAILY_BUDGET_USD")
    llm_price_input_per_mtok: float = Field(2.0, alias="LLM_PRICE_INPUT_PER_MTOK")
    llm_price_output_per_mtok: float = Field(10.0, alias="LLM_PRICE_OUTPUT_PER_MTOK")
    llm_price_per_web_search: float = Field(0.01, alias="LLM_PRICE_PER_WEB_SEARCH")

    run_workers: bool = Field(True, alias="RUN_WORKERS")
    stream_prefix: str = Field("meridian", alias="STREAM_PREFIX")
    static_dir: str = Field("", alias="STATIC_DIR")
    otel_endpoint: str = Field("", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Railway / Heroku style URLs -> SQLAlchemy psycopg3 driver
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    def assert_safe_for_production(self) -> None:
        if self.env == "production":
            if self.jwt_secret in ("", "dev-only-change-me") or len(self.jwt_secret) < 32:
                raise RuntimeError("JWT_SECRET must be set to a random value of >=32 chars in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def code_version() -> str:
    return os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_SHA") or "dev"
