from fastapi import FastAPI

from app.api.router import router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.include_router(router)
    return app


app = create_app()
