"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the ModernEdge assessment backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    output_dir: Path = Field(default=Path("./output"), alias="OUTPUT_DIR")

    # Client system base URLs
    payroll_url: str = Field(default="http://localhost:8081", alias="PAYROLL_URL")
    dispatch_url: str = Field(default="http://localhost:8082", alias="DISPATCH_URL")
    finance_url: str = Field(default="http://localhost:8083", alias="FINANCE_URL")

    # Model provider — OpenAI preferred when both are set
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL"
    )

    # Payroll write behaviour
    payroll_dry_run: bool = Field(default=True, alias="PAYROLL_DRY_RUN")
    payroll_api_key: str = Field(default="assess-7f3c9a", alias="PAYROLL_API_KEY")

    # Dispatch OAuth client credentials
    dispatch_client_id: str = Field(default="me-assess", alias="DISPATCH_CLIENT_ID")
    dispatch_client_secret: str = Field(
        default="d1sp4tch-s3cret", alias="DISPATCH_CLIENT_SECRET"
    )

    # Finance Basic auth
    finance_user: str = Field(default="svc_assess", alias="FINANCE_USER")
    finance_password: str = Field(default="Q4-close!", alias="FINANCE_PASSWORD")

    # Database — prefer Postgres in docker-compose. For local smoke tests without
    # Docker you can set DATABASE_URL=sqlite:///./modernedge.db (models use
    # portable sqlalchemy.JSON, not Postgres-only JSONB).
    database_url: str = Field(
        default="postgresql+psycopg://modernedge:modernedge@localhost:5432/modernedge",
        alias="DATABASE_URL",
    )

    # KPI / ingest period
    period: str = Field(default="2026-10", alias="PERIOD")

    # App behaviour
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ORIGINS",
    )
    auto_run_pipeline: bool = Field(default=False, alias="AUTO_RUN_PIPELINE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
