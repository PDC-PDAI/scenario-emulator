from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    ENVIRONMENT: str = "development"

    LLM_PROVIDER: Literal["openai", "openai_responses", "openai_like", "ollama", "ceia"] = "openai"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5-mini"
    OPENAI_BASE_URL: str | None = None
    OPENAI_REASONING_EFFORT: Literal["minimal", "low", "medium", "high"] | None = "minimal"

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen3:8b"
    OLLAMA_ENABLE_THINKING: bool = True

    RESPONSE_GENERATOR_LLM_PROVIDER: (
        Literal["openai", "openai_responses", "openai_like", "ollama", "ceia"] | None
    ) = None
    RESPONSE_GENERATOR_MODEL: str | None = None
    EVALUATOR_LLM_PROVIDER: (
        Literal["openai", "openai_responses", "openai_like", "ollama", "ceia"] | None
    ) = None
    EVALUATOR_MODEL: str | None = None

    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = ""
    LANGFUSE_PROMPT_LABEL: str = "production"
    LANGFUSE_SYNC_LABEL: str = "dev"
    LANGFUSE_TRACING_ENABLED: bool = True
    PROMPT_CACHE_TTL_SECONDS: int = 60
    PROMPT_FETCH_TIMEOUT_SECONDS: int = 3

    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000
    API_RELOAD: bool = False
    API_DATABASE_PATH: Path = ROOT_DIR / "data" / "scenario-emulator.db"
    API_CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    @field_validator("LANGFUSE_PROMPT_LABEL", "LANGFUSE_SYNC_LABEL")
    @classmethod
    def reject_latest_label(cls, value: str) -> str:
        if value.strip().lower() == "latest":
            raise ValueError("Use um label estável (dev/production), nunca 'latest'.")
        return value.strip()

    @property
    def langfuse_configured(self) -> bool:
        return bool(
            self.LANGFUSE_TRACING_ENABLED
            and self.LANGFUSE_PUBLIC_KEY
            and self.LANGFUSE_SECRET_KEY
            and self.LANGFUSE_BASE_URL
        )

    @property
    def api_cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.API_CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
