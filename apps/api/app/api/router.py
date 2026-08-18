from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.torlink.client import TorlinkClient

router = APIRouter(prefix="/api")


def get_torlink_client(settings: Settings = Depends(get_settings)) -> TorlinkClient:
    return TorlinkClient(
        base_url=str(settings.torlink_base_url),
        token=settings.torlink_token,
        timeout_seconds=settings.torlink_timeout_seconds,
    )


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "ok": True,
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }


@router.get("/torlink/health")
async def torlink_health(client: TorlinkClient = Depends(get_torlink_client)) -> dict[str, object]:
    return {"ok": True, "torlink": await client.health()}
