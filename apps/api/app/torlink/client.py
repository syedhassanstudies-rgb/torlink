"""Async client for the local torlink Node daemon.

This is the ONLY place in the FastAPI app that knows how to talk to the
daemon. Route handlers depend on this adapter; they never build daemon
HTTP calls themselves.

Daemon contract (verified against src/daemon/serve.ts):
    GET  /health              -> {ok, version}                (no auth)
    GET  /downloads           -> {downloads: [...], seeds: [...]}
    POST /add                 {magnet|infohash} -> {ok, outcome}
    POST /control             {id, action, deleteFiles?} -> {ok, id, action}
"""

from typing import Any

import httpx

from app.torlink.models import (
    AddOutcome,
    ControlAction,
    DaemonStatus,
)


class TorlinkClientError(Exception):
    """Base exception for torlink daemon communication failures."""


class TorlinkUnavailableError(TorlinkClientError):
    """Raised when the torlink daemon cannot be reached."""


class TorlinkHTTPError(TorlinkClientError):
    """Raised when the torlink daemon returns a non-success response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"torlink returned HTTP {status_code}: {detail}")


class TorlinkClient:
    """Small async client for the local torlink daemon."""

    def __init__(
        self, base_url: str, token: str | None = None, timeout_seconds: float = 5.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health", authenticated=False)

    # ---- Phase 4 adapter surface -------------------------------------------

    async def list_downloads(self) -> DaemonStatus:
        """Fetch and validate the daemon's full status payload."""

        body = await self._request("GET", "/downloads")
        try:
            return DaemonStatus.model_validate(body)
        except ValueError as exc:
            raise TorlinkClientError(f"unexpected /downloads payload: {exc}") from exc

    async def add_download(self, magnet_or_infohash: str) -> AddOutcome:
        """Hand a magnet URI or info hash to the daemon's queue."""

        if not magnet_or_infohash or not magnet_or_infohash.strip():
            raise TorlinkClientError("empty magnet/info hash")
        body = await self._request(
            "POST", "/add", body={"magnet": magnet_or_infohash.strip()}
        )
        outcome = body.get("outcome")
        return AddOutcome(
            ok=bool(body.get("ok", False)),
            outcome=str(outcome) if outcome else None,
            raw=body,
        )

    async def control(
        self,
        torrent_id: str,
        action: ControlAction | str,
        delete_files: bool = False,
    ) -> dict[str, Any]:
        """Apply pause/resume/seed/remove/delete to a daemon torrent."""

        action_str = action.value if isinstance(action, ControlAction) else str(action)
        valid = {a.value for a in ControlAction}
        if action_str not in valid:
            raise TorlinkClientError(f"invalid control action: {action_str}")
        return await self._request(
            "POST",
            "/control",
            body={"id": torrent_id, "action": action_str, "deleteFiles": delete_files},
        )

    # ---- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers() if authenticated else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", headers=headers, json=body
                )
        except httpx.RequestError as exc:
            raise TorlinkUnavailableError("torlink daemon is unavailable") from exc

        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    detail = payload["error"]
            except ValueError:
                pass
            raise TorlinkHTTPError(response.status_code, detail)

        try:
            payload = response.json()
        except ValueError as exc:
            raise TorlinkClientError("torlink returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TorlinkClientError("torlink returned an unexpected payload")
        return payload
