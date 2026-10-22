"""Runtime configuration, read from environment variables (and `.env`)."""
import secrets
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LEN = 32


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    llm_mode: Literal["mock", "live", "auto"] = "mock"
    anthropic_api_key: str = ""
    anthropic_workspace_id: str = ""      # needed only for API keys not scoped to a workspace
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_server_fallbacks: bool = True
    llm_timeout_seconds: float = 120.0

    # Token budget (see app/llm/budget.py)
    token_budget_per_run: int = 40_000
    token_budget_max_input_per_call: int = 12_000
    token_budget_max_output_per_call: int = 8_000
    token_budget_daily: int = 500_000

    # App
    database_url: str = "sqlite:///./data/pa_orchestrator.db"
    # Secret from which each agent's internal-API credential is derived. Empty = a random
    # per-process secret, which only works with the in-process transport.
    internal_api_secret: str = ""
    internal_api_base_url: str = ""
    # Secret that signs reviewer session tokens. Empty = random per process (sessions end on restart).
    reviewer_token_secret: str = ""
    reviewer_token_ttl_seconds: int = 8 * 3600
    # Worker lease on a run; a heartbeat renews it every third of this while the worker is busy
    # (including during long LLM calls). A RUNNING run with an expired lease is recoverable.
    run_lease_seconds: int = 30
    # A RESERVED token-ledger row older than this belongs to a dead call and is settled by recovery.
    token_reservation_expiry_seconds: int = 900

    @property
    def effective_llm_mode(self) -> Literal["mock", "live"]:
        if self.llm_mode == "auto":
            return "live" if self.anthropic_api_key else "mock"
        return self.llm_mode


_RANDOM = {"internal": secrets.token_urlsafe(48), "reviewer": secrets.token_urlsafe(48)}


def _secret(value: str, name: str, kind: str) -> str:
    if not value:
        return _RANDOM[kind]
    if len(value) < MIN_SECRET_LEN or value.lower().startswith("change-me"):
        raise ConfigError(f"{name} must be a random value of at least {MIN_SECRET_LEN} characters")
    return value


def internal_secret() -> str:
    s = get_settings()
    if s.internal_api_base_url and not s.internal_api_secret:
        raise ConfigError("INTERNAL_API_SECRET must be set when INTERNAL_API_BASE_URL is used")
    return _secret(s.internal_api_secret, "INTERNAL_API_SECRET", "internal")


def reviewer_secret() -> str:
    return _secret(get_settings().reviewer_token_secret, "REVIEWER_TOKEN_SECRET", "reviewer")


def validate_settings() -> None:
    """Fail fast at startup on unsafe configuration."""
    internal_secret()
    reviewer_secret()


@lru_cache
def get_settings() -> Settings:
    return Settings()
