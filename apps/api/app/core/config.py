from functools import lru_cache

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the FastAPI app layer."""

    app_name: str = "torlink API"
    app_version: str = "0.1.0"
    environment: str = "development"
    torlink_base_url: AnyHttpUrl = Field(default="http://127.0.0.1:9161")
    torlink_token: str | None = None
    torlink_timeout_seconds: float = 5.0

    model_config = SettingsConfigDict(
        env_prefix="TORLINK_API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached app settings."""

    return Settings()
