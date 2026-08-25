"""Autouse fixture: reset rate-limit counters between tests."""

from __future__ import annotations

import pytest

from app.core.ratelimit import (
    add_download_limiter,
    login_limiter,
    search_limiter,
)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    login_limiter.reset()
    search_limiter.reset()
    add_download_limiter.reset()
    yield
    login_limiter.reset()
    search_limiter.reset()
    add_download_limiter.reset()
