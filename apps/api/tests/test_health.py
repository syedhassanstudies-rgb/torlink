from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_app_metadata() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "app": "torlink API",
        "version": "0.1.0",
        "environment": "development",
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
