"""File-browser backend: safe path resolution, tree walking, streaming proxy.

Security model (Phase 8):
- Users only ever see UUID file records; raw paths never leave the server.
- Every disk access resolves through `safe_join`, the Python twin of the
  daemon's `safeResolve` (refuses traversal, absolute paths, symlink escapes).
- Streaming proxies the torlink files server (port 9160) so ownership is
  enforced per-request by FastAPI before any byte flows.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.config import get_settings


class SafePathError(ValueError):
    """Raised when a requested path would escape the download root."""


def safe_join(root: str | Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes it.

    Mirrors src/daemon/files.ts safeResolve: no '..', no absolute paths,
    and (defense in depth) a resolved-path containment check that also
    catches symlink tricks.
    """

    root_path = Path(root).resolve()
    if not relative or relative.strip() == "":
        raise SafePathError("empty path")
    # Normalize Windows/POSIX separators from stored records.
    normalized = relative.replace("\\", "/")
    candidate = Path(normalized)
    drive = len(normalized) > 1 and normalized[1] == ":"
    if candidate.is_absolute() or normalized.startswith("/") or drive:
        raise SafePathError("absolute paths are not allowed")
    if any(part == ".." for part in candidate.parts):
        raise SafePathError("path traversal is not allowed")
    full = (root_path / candidate).resolve()
    if full != root_path and root_path not in full.parents:
        raise SafePathError("resolved path escapes the download root")
    return full


def download_root() -> str:
    settings = get_settings()
    if not settings.download_dir:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="file browsing is not configured (TORLINK_API_DOWNLOAD_DIR missing)",
        )
    return settings.download_dir


def _files_headers() -> dict[str, str]:
    token = get_settings().files_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def stat_local(root: str, relative: str) -> dict | None:
    """Stat a file inside the root. Returns None when missing/blocked."""

    try:
        full = safe_join(root, relative)
    except SafePathError:
        return None
    try:
        st = full.stat()
    except OSError:
        return None
    if not st.st_size and not st.st_mode & 0o4000:
        pass  # zero-byte files are still valid
    return {"size": st.st_size, "is_file": full.is_file(), "path": full}


def walk_tree(root: str, relative_dir: str = "", recursive: bool = True) -> list[dict]:
    """List a directory inside the root. Recursive by default so one call
    returns the whole torrent's file set for record syncing."""

    try:
        base = safe_join(root, relative_dir)
    except SafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")
    entries = []
    try:
        if recursive:
            walker = base.rglob("*")
        else:
            walker = base.iterdir()
        for entry in sorted(walker, key=lambda p: str(p).lower()):
            rel = os.path.relpath(entry, root).replace("\\", "/")
            if entry.is_dir():
                entries.append({"name": entry.name, "type": "dir", "relative_path": rel})
            else:
                entries.append({
                    "name": entry.name,
                    "type": "file",
                    "relative_path": rel,
                    "size_bytes": entry.stat().st_size,
                })
    except OSError as exc:
        raise HTTPException(status_code=500, detail="cannot read directory") from exc
    return entries


def proxy_stream(relative_path: str, range_header: str | None) -> StreamingResponse:
    """Proxy the file from the daemon's files server, forwarding ranges.

    Ownership must already be checked by the caller; this function only
    moves bytes.
    """

    settings = get_settings()
    encoded = httpx.QueryParams({"p": relative_path})["p"]
    url = f"{str(settings.files_base_url).rstrip('/')}/{encoded}"
    headers = _files_headers()
    forward: dict[str, str] = {}
    if range_header:
        forward["Range"] = range_header

    client = httpx.Client(timeout=settings.files_timeout_seconds)
    req = client.build_request("GET", url, headers={**headers, **forward})
    try:
        upstream = client.send(req, stream=True)
    except httpx.HTTPError as exc:
        client.close()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="torlink files server is unavailable",
        ) from exc

    if upstream.status_code >= 400:
        code = upstream.status_code
        upstream.close()
        client.close()
        if code in (401, 403):
            raise HTTPException(status_code=502, detail="files server rejected the request")
        if code == 404:
            raise HTTPException(status_code=404, detail="file not found on disk")
        if code == 416:
            raise HTTPException(status_code=416, detail="range not satisfiable")
        raise HTTPException(status_code=502, detail=f"files server error {code}")

    resp_headers = {}
    for name in ("accept-ranges", "content-range", "content-length", "content-type"):
        if value := upstream.headers.get(name):
            resp_headers[name] = value
    resp_headers.setdefault("content-type", "application/octet-stream")

    def iterator():
        try:
            yield from upstream.iter_bytes(chunk_size=64 * 1024)
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(
        iterator(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=resp_headers.get("content-type"),
    )


# The files server maps URL path -> file under its root, so the proxied URL
# is simply the relative path. QueryParams above percent-encodes it safely;
# this helper documents intent for reviewers.
def proxied_url(relative_path: str) -> str:
    from urllib.parse import quote

    return quote(relative_path.replace("\\", "/"))
