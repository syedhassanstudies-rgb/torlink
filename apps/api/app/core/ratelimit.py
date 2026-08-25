"""Simple in-memory sliding-window rate limiter.

Per-process (per worker). For multi-worker deployments swap the counters
for Redis later — the interface here keeps that a one-file change.

Not used for correctness; purely abuse mitigation on expensive or
brute-forceable routes.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, request: Request) -> str:
        # Behind no proxy today; client host is the best identity we have.
        # Authenticated limiters can pass an explicit key instead.
        return request.client.host if request.client else "unknown"

    def check(self, request: Request, key_suffix: str = "") -> None:
        """Raise 429 when the caller exceeds the budget; else record a hit."""

        key = f"{self._key(request)}:{key_suffix}" if key_suffix else self._key(request)
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many requests; slow down",
            )
        hits.append(now)

    def reset(self) -> None:
        """Clear all counters (used by tests)."""

        self._hits.clear()


# Shared limiters (per process).
login_limiter = RateLimiter(max_requests=5, window_seconds=60.0)
search_limiter = RateLimiter(max_requests=30, window_seconds=60.0)
add_download_limiter = RateLimiter(max_requests=10, window_seconds=60.0)
