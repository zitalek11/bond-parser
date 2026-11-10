from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parents[2]

LLMProvider = Literal["ollama", "vllm", "openrouter"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General
    app_name: str = "Парсер эмиссионки"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # Database
    database_url: str = "sqlite+aiosqlite:///./storage/bond_parser.db"
    db_echo: bool = False

    # Storage
    storage_dir: Path = Path("./storage")
    max_upload_size_mb: int = 50

    # Extraction
    extractor_backend: Literal["auto", "docling", "pymupdf"] = "auto"

    # LLM — Qwen 2.5 (Ollama / vLLM / OpenRouter)
    llm_provider: LLMProvider = "ollama"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "not-needed"
    # Prefer LLM_MODEL_NAME; LLM_MODEL kept as alias for backward compatibility
    llm_model_name: str = "qwen2.5:32b"
    llm_model: str | None = None
    llm_temperature: float = 0.05
    llm_max_tokens: int = 8192
    llm_context_window: int = 32_768
    llm_max_retries: int = 3
    llm_timeout_seconds: float = 300.0
    llm_max_input_chars: int = 100_000
    # Qwen 2.5: MD_JSON is the most stable mode for nested schemas
    llm_instructor_mode: Literal["TOOLS", "JSON", "MD_JSON"] = "MD_JSON"
    llm_repeat_penalty: float = 1.05
    # If true (or API key looks like a placeholder), pipeline uses MockLLMBondParser
    llm_use_mock: bool = False

    # Collector
    rss_url: str = "https://www.e-disclosure.ru/rss/rss.aspx"
    rss_use_mock: bool = True
    rss_keywords: list[str] = Field(
        default_factory=lambda: ["решение о выпуске", "проспект ценных бумаг", "облигац"]
    )
    moex_iss_base_url: str = "https://iss.moex.com/iss"
    http_timeout_seconds: float = 30.0

    # Scheduler
    scheduler_enabled: bool = True
    collector_interval_minutes: int = 30

    # Data Quality Gate
    amortization_tolerance: float = 0.01
    nominal_tolerance: float = 0.01

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> Settings:
        # Resolve model alias
        if self.llm_model and not self.llm_model_name:
            self.llm_model_name = self.llm_model
        elif self.llm_model is None:
            object.__setattr__(self, "llm_model", self.llm_model_name)

        # Provider-specific defaults when URL still points at the other stack
        if self.llm_provider == "ollama":
            if "8000/v1" in self.llm_base_url and "11434" not in self.llm_base_url:
                object.__setattr__(self, "llm_base_url", "http://localhost:11434/v1")
            if self.llm_model_name.startswith("Qwen/") or self.llm_model_name.startswith("qwen/"):
                object.__setattr__(self, "llm_model_name", "qwen2.5:32b")
                object.__setattr__(self, "llm_model", "qwen2.5:32b")
        elif self.llm_provider == "vllm":
            if "11434" in self.llm_base_url or "openrouter.ai" in self.llm_base_url:
                object.__setattr__(self, "llm_base_url", "http://localhost:8000/v1")
            if self.llm_model_name.startswith("qwen2.5:") or self.llm_model_name.startswith("qwen/"):
                object.__setattr__(
                    self, "llm_model_name", "Qwen/Qwen2.5-32B-Instruct-AWQ"
                )
                object.__setattr__(
                    self, "llm_model", "Qwen/Qwen2.5-32B-Instruct-AWQ"
                )
        elif self.llm_provider == "openrouter":
            if "openrouter.ai" not in self.llm_base_url:
                object.__setattr__(self, "llm_base_url", "https://openrouter.ai/api/v1")
            if (
                self.llm_model_name.startswith("qwen2.5:")
                or self.llm_model_name.startswith("Qwen/")
                or "/" not in self.llm_model_name
            ):
                object.__setattr__(
                    self, "llm_model_name", "qwen/qwen-2.5-72b-instruct"
                )
                object.__setattr__(self, "llm_model", "qwen/qwen-2.5-72b-instruct")

        return self

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _resolve_storage_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @field_validator("database_url", mode="after")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """Relative SQLite paths are anchored to the project root, not to CWD."""
        url = make_url(value)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return value
        db_path = Path(url.database)
        if db_path.is_absolute():
            return value
        return url.set(database=str((BASE_DIR / db_path).resolve())).render_as_string(
            hide_password=False
        )

    @property
    def is_sqlite(self) -> bool:
        return make_url(self.database_url).get_backend_name() == "sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "documents"

    @property
    def resolved_llm_model(self) -> str:
        return self.llm_model_name or self.llm_model or "qwen2.5:32b"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
