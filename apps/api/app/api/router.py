from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import router as auth_router
from app.api.auth import users_router
from app.core.config import Settings, get_settings
from app.torlink.client import (
    TorlinkClient,
    TorlinkClientError,
    TorlinkUnavailableError,
)

router = APIRouter(prefix="/api")
router.include_router(auth_router)
router.include_router(users_router)


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
    try:
        daemon = await client.health()
    except TorlinkUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="torlink daemon is unavailable",
        ) from exc
    except TorlinkClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    daemon_ok = bool(daemon.get("ok", True))
    return {
        "ok": daemon_ok,
        "torlink": daemon,
    }
