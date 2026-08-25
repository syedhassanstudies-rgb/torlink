from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import router
from app.core.config import get_settings
from app.realtime.poller import DownloadStatusPoller, StatusCache
from app.realtime.ws import register_ws_routes
from app.torlink.client import TorlinkClient


def create_app() -> FastAPI:
    settings = get_settings()

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
    app.include_router(router)
    register_ws_routes(app)
    return app


app = create_app()
