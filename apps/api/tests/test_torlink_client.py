"""Tests for the torlink daemon adapter (mocked HTTP transport, no live daemon)."""

import httpx
import pytest

from app.torlink.client import (
    TorlinkClient,
    TorlinkClientError,
    TorlinkHTTPError,
    TorlinkUnavailableError,
)
from app.torlink.models import ControlAction


def _patch_transport(monkeypatch, routes: dict, captured: list) -> None:
    """Route httpx.AsyncClient.request to canned responses keyed by (method, path)."""

    async def fake_request(self, method, url, headers=None, json=None, **kw):
        captured.append({"method": method, "url": url, "headers": headers, "json": json})
        path = "/" + url.split("9161", 1)[1].lstrip("/") if "9161" in url else "/"
        key = (method, path.rstrip("/") or "/")
        if key not in routes:
            return httpx.Response(
                404, json={"error": "not found"}, request=httpx.Request(method, url)
            )
        status, body = routes[key]
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


def make_client(token: str | None = "tok") -> TorlinkClient:
    return TorlinkClient("http://127.0.0.1:9161", token=token)


# ---- health -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_sends_no_auth_header(monkeypatch) -> None:
    captured: list = []
    _patch_transport(
        monkeypatch, {("GET", "/health"): (200, {"ok": True, "version": "1.6.0"})}, captured
    )
    body = await make_client().health()
    assert body == {"ok": True, "version": "1.6.0"}
    sent_headers = captured[0]["headers"] or {}
    assert all(k.lower() != "authorization" for k in sent_headers)


@pytest.mark.asyncio
async def test_authenticated_requests_carry_bearer(monkeypatch) -> None:
    captured: list = []
    _patch_transport(
        monkeypatch,
        {("GET", "/downloads"): (200, {"downloads": [], "seeds": []})},
        captured,
    )
    await make_client().list_downloads()
    headers = captured[0]["headers"] or {}
    assert any(
        k.lower() == "authorization" and v == "Bearer tok" for k, v in headers.items()
    )


# ---- list_downloads ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_downloads_parses_payload(monkeypatch) -> None:
    payload = {
        "downloads": [
            {
                "id": "abc123",
                "name": "Ubuntu ISO",
                "status": "downloading",
                "progress": 0.42,
                "peers": 7,
                "speed": 1048576,
            }
        ],
        "seeds": [{"id": "seed1", "name": "Done", "peers": 3, "uploaded": 999}],
    }
    _patch_transport(monkeypatch, {("GET", "/downloads"): (200, payload)}, [])
    status = await make_client().list_downloads()
    assert status.downloads[0].id == "abc123"
    assert status.downloads[0].progress == pytest.approx(0.42)
    assert status.seeds[0].uploaded == 999


@pytest.mark.asyncio
async def test_list_downloads_survives_extra_fields_and_unknown_status(monkeypatch) -> None:
    payload = {
        "downloads": [{"id": "x", "brandNewField": True, "status": "brand-new-state"}],
        "seeds": [],
    }
    _patch_transport(monkeypatch, {("GET", "/downloads"): (200, payload)}, [])
    status = await make_client().list_downloads()
    assert status.downloads[0].status == "brand-new-state"


@pytest.mark.asyncio
async def test_list_downloads_invalid_payload_raises_client_error(monkeypatch) -> None:
    _patch_transport(
        monkeypatch, {("GET", "/downloads"): (200, {"downloads": "not-a-list"})}, []
    )
    with pytest.raises(TorlinkClientError):
        await make_client().list_downloads()


# ---- add_download ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_download_posts_magnet_json(monkeypatch) -> None:
    captured: list = []
    _patch_transport(
        monkeypatch, {("POST", "/add"): (200, {"ok": True, "outcome": "added"})}, captured
    )
    result = await make_client().add_download("magnet:?xt=urn:btih:abcdef")
    assert result.ok is True
    assert result.outcome == "added"
    assert captured[0]["json"] == {"magnet": "magnet:?xt=urn:btih:abcdef"}


@pytest.mark.asyncio
async def test_add_download_rejects_empty_input() -> None:
    client = make_client()
    with pytest.raises(TorlinkClientError):
        await client.add_download("   ")
    with pytest.raises(TorlinkClientError):
        await client.add_download("")


@pytest.mark.asyncio
async def test_add_download_daemon_400_maps_to_http_error(monkeypatch) -> None:
    _patch_transport(monkeypatch, {("POST", "/add"): (400, {"error": "invalid magnet"})}, [])
    with pytest.raises(TorlinkHTTPError) as excinfo:
        await make_client().add_download("garbage")
    assert excinfo.value.status_code == 400
    assert "invalid magnet" in str(excinfo.value)


# ---- control -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_sends_id_action_deletefiles(monkeypatch) -> None:
    captured: list = []
    _patch_transport(
        monkeypatch,
        {("POST", "/control"): (200, {"ok": True, "id": "abc", "action": "pause"})},
        captured,
    )
    body = await make_client().control("abc", ControlAction.PAUSE)
    assert body["ok"] is True
    assert captured[0]["json"] == {"id": "abc", "action": "pause", "deleteFiles": False}


@pytest.mark.asyncio
async def test_control_delete_passes_flag(monkeypatch) -> None:
    captured: list = []
    _patch_transport(monkeypatch, {("POST", "/control"): (200, {"ok": True})}, captured)
    await make_client().control("abc", ControlAction.DELETE, delete_files=True)
    assert captured[0]["json"] == {"id": "abc", "action": "delete", "deleteFiles": True}


@pytest.mark.asyncio
async def test_control_accepts_plain_string_action(monkeypatch) -> None:
    captured: list = []
    _patch_transport(monkeypatch, {("POST", "/control"): (200, {"ok": True})}, captured)
    await make_client().control("abc", "resume")
    assert captured[0]["json"]["action"] == "resume"


@pytest.mark.asyncio
async def test_control_rejects_unknown_action_locally() -> None:
    with pytest.raises(TorlinkClientError):
        await make_client().control("abc", "format-disk")


# ---- failure modes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_error_maps_to_unavailable(monkeypatch) -> None:
    async def boom(self, method, url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)
    with pytest.raises(TorlinkUnavailableError):
        await make_client().list_downloads()


@pytest.mark.asyncio
async def test_non_json_response_maps_to_client_error(monkeypatch) -> None:
    async def html(self, method, url, **kw):
        return httpx.Response(
            200, text="<html>proxy error</html>", request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", html)
    with pytest.raises(TorlinkClientError):
        await make_client().list_downloads()


@pytest.mark.asyncio
async def test_404_from_daemon_maps_to_http_error(monkeypatch) -> None:
    _patch_transport(monkeypatch, {}, [])
    with pytest.raises(TorlinkHTTPError) as e:
        await make_client().list_downloads()
    assert e.value.status_code == 404
