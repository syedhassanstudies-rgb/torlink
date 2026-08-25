import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.core.config import get_settings
from app.core.hardening import BodySizeLimitMiddleware, TokenScrubFilter
from app.realtime.poller import DownloadStatusPoller, StatusCache
from app.realtime.ws import register_ws_routes
from app.torlink.client import TorlinkClient

_DEV_SECRET = "dev-secret-change-me-0123456789abcdef0123456789abcdef"


def _validate_production_settings() -> None:
    """Fail fast on unsafe production configuration."""

    settings = get_settings()
    if settings.environment == "production":
        if settings.jwt_secret == _DEV_SECRET:
            raise RuntimeError(
                "TORLINK_API_JWT_SECRET is still the development default; "
                "refusing to start in production. Set a strong random secret."
            )
        if not settings.torlink_token or not settings.files_token:
            raise RuntimeError(
                "production requires TORLINK_API_TORLINK_TOKEN and "
                "TORLINK_API_FILES_TOKEN (daemon and files server auth)."
            )


def create_app() -> FastAPI:
    settings = get_settings()
    _validate_production_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.status_cache = StatusCache()
        client = TorlinkClient(
            base_url=str(settings.torlink_base_url),
            token=settings.torlink_token,
            timeout_seconds=settings.torlink_timeout_seconds,
        )
        poller = DownloadStatusPoller(client, app.state.status_cache, interval_seconds=1.0)
        app.state.download_poller = poller
        poller.start()
        yield
        await poller.stop()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    # CORS: explicit allow-list only. Empty list = no cross-origin browser
    # access at all (safe default); set TORLINK_API_CORS_ORIGINS for the
    # frontend origin(s) in deployment.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.add_middleware(BodySizeLimitMiddleware)

    # Scrub ?token=... (WS auth) from access logs before they hit disk.
    logging.getLogger("uvicorn.access").addFilter(TokenScrubFilter())

    app.include_router(router)
    register_ws_routes(app)
    return app


app = create_app()
