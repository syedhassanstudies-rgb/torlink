from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_health_returns_app_metadata() -> None:
    client = TestClient(create_app())
    settings = get_settings()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }


def test_torlink_health_uses_dependency_override() -> None:
    app = create_app()

    class FakeTorlinkClient:
        async def health(self) -> dict[str, object]:
            return {"ok": True, "version": "test"}

    from app.api.router import get_torlink_client

    app.dependency_overrides[get_torlink_client] = lambda: FakeTorlinkClient()
    client = TestClient(app)

    response = client.get("/api/torlink/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "torlink": {"ok": True, "version": "test"}}


def test_torlink_health_reports_unavailable_daemon() -> None:
    app = create_app()

    class DeadClient:
        async def health(self) -> dict[str, object]:
            from app.torlink.client import TorlinkUnavailableError

            raise TorlinkUnavailableError("torlink daemon is unavailable")

    from app.api.router import get_torlink_client

    app.dependency_overrides[get_torlink_client] = lambda: DeadClient()
    client = TestClient(app)

    response = client.get("/api/torlink/health")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_torlink_health_maps_daemon_error_to_bad_gateway() -> None:
    app = create_app()

    class ErroringClient:
        async def health(self) -> dict[str, object]:
            from app.torlink.client import TorlinkHTTPError

            raise TorlinkHTTPError(500, "boom")

    from app.api.router import get_torlink_client

    app.dependency_overrides[get_torlink_client] = lambda: ErroringClient()
    client = TestClient(app)

    response = client.get("/api/torlink/health")

    assert response.status_code == 502
    assert "boom" in response.json()["detail"]
