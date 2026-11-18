"""Runtime settings, read from environment variables (prefix ``CAREFLOW_``) or a .env file."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["offline", "openai", "anthropic", "ollama"]

# Anthropic has no default: set CAREFLOW_LLM_MODEL to the model id you want.
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAREFLOW_", env_file=".env", extra="ignore")

    llm_provider: LLMProvider = "offline"
    llm_model: str | None = None
    llm_temperature: float = 0.0
    ollama_base_url: str = "http://localhost:11434"

    data_dir: Path = Path("data")

    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_attempts: int = Field(default=2, ge=1, le=5)

    reviewer_api_key: SecretStr | None = None
    checkpoint_key: SecretStr | None = None
    mcp_transport: Literal["inprocess", "stdio"] = "inprocess"

    # Pin "today" so demos and tests are reproducible. Leave unset in real use.
    today: date | None = None

    @property
    def model_name(self) -> str | None:
        if self.llm_provider == "offline":
            return None
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider)

    @property
    def ehr_db(self) -> Path:
        """Mock EHR / scheduling / billing data that sits behind the MCP server."""
        return self.data_dir / "ehr.db"

    @property
    def app_db(self) -> Path:
        """Request registry and audit log."""
        return self.data_dir / "app.db"

    @property
    def checkpoint_db(self) -> Path:
        """LangGraph checkpoints, so a paused request survives a restart."""
        return self.data_dir / "checkpoints.db"

    def current_date(self) -> date:
        return self.today or date.today()


@lru_cache
def get_settings() -> Settings:
    return Settings()
