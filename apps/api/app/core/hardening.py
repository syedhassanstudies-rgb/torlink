"""Hardening middleware: body-size cap + token scrubbing access filter."""

from __future__ import annotations

import logging
import re

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than settings.max_body_bytes with 413.

    Only inspects requests that declare a Content-Length; chunked bodies
    are counted while buffered by Starlette anyway, and our JSON routes
    always send Content-Length.
    """

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > get_settings().max_body_bytes:
                from starlette.responses import JSONResponse

                return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)


class TokenScrubFilter(logging.Filter):
    """Strip query-string tokens from uvicorn access logs.

    WS clients pass ?token=<jwt> because browsers cannot set headers on
    WebSocket handshakes; those must never land in log files.
    """

    pattern = re.compile(r"([?&])token=[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()
        scrubbed = self.pattern.sub(r"\1token=REDACTED", msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True
