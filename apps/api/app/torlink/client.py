from typing import Any

import httpx


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

    def __init__(self, base_url: str, token: str | None = None, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health", authenticated=False)

    async def _request(self, method: str, path: str, *, authenticated: bool = True) -> dict[str, Any]:
        headers = self._headers() if authenticated else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(method, f"{self.base_url}{path}", headers=headers)
        except httpx.RequestError as exc:
            raise TorlinkUnavailableError("torlink daemon is unavailable") from exc

        if response.status_code >= 400:
            detail = response.text
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("error"), str):
                    detail = body["error"]
            except ValueError:
                pass
            raise TorlinkHTTPError(response.status_code, detail)

        try:
            body = response.json()
        except ValueError as exc:
            raise TorlinkClientError("torlink returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise TorlinkClientError("torlink returned an unexpected payload")
        return body
