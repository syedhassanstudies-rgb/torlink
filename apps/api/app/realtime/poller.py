"""Shared download-status cache + ONE background poller for WebSockets.

Design (Phase 6, per spec):

    torlink daemon  <--  ONE background poller (1s interval)
                              |
                              v
                      StatusCache (latest snapshot)
                              |
                    broadcast to every connected WS client

Never one poll loop per websocket client. The poller only hits the daemon
while at least one client is connected; the cache keeps the last known
snapshot so newly-connecting clients get an immediate first frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from app.torlink.client import TorlinkClientError
from app.torlink.models import DaemonStatus

logger = logging.getLogger(__name__)


class StatusCache:
    """Holds the latest daemon snapshot + registry of connected clients."""

    def __init__(self) -> None:
        self.snapshot: DaemonStatus | None = None
        self.updated_at: float = 0.0
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, snapshot: DaemonStatus) -> list[asyncio.Queue[dict[str, Any]]]:
        """Store the snapshot and return queues that need a notification."""

        self.snapshot = snapshot
        self.updated_at = time.monotonic()
        # Queue.put_nowait never blocks on unbounded queues; collect the
        # queues so the caller awaits outside any lock-free hot path.
        return [q for q in self.clients if q.empty()]


class DownloadStatusPoller:
    """Single shared poll loop: daemon -> cache -> client queues.

    A zero-interval or negative interval is allowed for tests: with no
    clients connected it still idles, and `poll_once()` can drive it
    manually.
    """

    def __init__(
        self,
        client: Any,
        cache: StatusCache,
        interval_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.cache = cache
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="torlink-status-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def kick(self) -> None:
        """Force an immediate poll cycle (e.g. right after a client joins)."""

        self._wake.set()

    # ---- loop --------------------------------------------------------------

    async def run(self) -> None:
        logger.info("download status poller started (interval=%.2fs)", self.interval_seconds)
        try:
            while True:
                await self.poll_once()
                await self._sleep()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive: keep loop alive
            logger.exception("status poller crashed; stopping")
            raise

    async def _sleep(self) -> None:
        """Sleep the poll interval, or wake early when kicked."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
        self._wake.clear()

    async def poll_once(self) -> bool:
        """Poll the daemon once. Returns True on a fresh successful fetch."""

        if not self.cache.clients:
            # No listeners — do not waste a daemon round-trip.
            return False
        try:
            status = await self.client.list_downloads()
        except TorlinkClientError as exc:
            # Transient daemon problems must not kill the poller.
            logger.warning("status poll failed: %s", exc)
            return False
        await self._broadcast(status)
        return True

    async def _broadcast(self, status: DaemonStatus) -> None:
        queues = self.cache.publish(status)
        message: dict[str, Any] = {
            "type": "snapshot",
            "downloads": [
                {
                    "id": d.id,
                    "name": d.name,
                    "status": d.status,
                    "progress": d.progress,
                    "peers": d.peers,
                    "speed": d.speed,
                }
                for d in status.downloads
            ],
            "seeds": [
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status,
                    "peers": s.peers,
                    "uploaded": s.uploaded,
                }
                for s in status.seeds
            ],
        }
        for q in queues:
            q.put_nowait(message)


# Module-level singleton wired by main.py lifespan; tests override via
# app.state instead of touching this directly where possible.
cache = StatusCache()


def get_poller(app: Any) -> DownloadStatusPoller | None:
    return getattr(app.state, "download_poller", None)
