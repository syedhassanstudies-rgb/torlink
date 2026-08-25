from functools import lru_cache

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@lru_cache(maxsize=1)
def _default_version() -> str:
    """Resolve the app version from installed package metadata.

    Falls back to a literal only when the package is not installed
    (e.g. running from a bare checkout without `pip install -e .`).
    """

    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("torlink-api")
    except PackageNotFoundError:
        return "0.1.0"


class Settings(BaseSettings):
    """Runtime settings for the FastAPI app layer."""

    app_name: str = "torlink API"
    app_version: str = Field(default_factory=_default_version)
    environment: str = "development"
    torlink_base_url: AnyHttpUrl = Field(default="http://127.0.0.1:9161")
    torlink_token: str | None = None
    torlink_timeout_seconds: float = 5.0
    # Files server (torlnk files, port 9160): used to proxy streams so the
    # daemon never has to be user-reachable.
    files_base_url: AnyHttpUrl = Field(default="http://127.0.0.1:9160")
    files_token: str | None = None
    files_timeout_seconds: float = 30.0
    # Local mirror of the Node daemon's downloadDir; must point at the same
    # directory the daemon writes completed downloads into.
    download_dir: str = ""
    # Hardening
    cors_origins: list[str] = Field(default_factory=list)  # explicit allow-list
    max_body_bytes: int = 1_048_576  # 1 MiB cap for JSON bodies
    rate_limit_enabled: bool = True
    database_url: str = Field(
        default="postgresql+asyncpg://torlink:torlink@localhost:5432/torlink"
    )
    db_echo: bool = False
    # Auth
    jwt_secret: str = Field(
        default="dev-secret-change-me-0123456789abcdef0123456789abcdef"
    )  # must be >=32 bytes for HS256; override in .env for any real use
    jwt_algorithm: str = "HS256"
    access_token_minutes: float = 15.0
    refresh_token_days: float = 14.0

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
