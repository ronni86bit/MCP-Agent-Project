"""Request-correlation ASGI middleware.

Generates or validates a request ID per HTTP request, binds it to structlog
contextvars (so all log entries within the request carry it), adds it as an
``X-Request-ID`` response header, and clears the context in a ``finally``
block so reused async tasks cannot leak stale bindings.
"""

from __future__ import annotations

import re
import uuid

import structlog

_CORRELATION_RE = re.compile(r"^[a-z]{3,8}-[0-9a-f]{12}$")


def _validate_or_generate(value: str | None, prefix: str) -> str:
    if value and _CORRELATION_RE.fullmatch(value):
        return value
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CorrelationMiddleware:
    """ASGI middleware: per-request correlation binding and response header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _validate_or_generate(
            self._header_value(scope, b"x-request-id"), "req"
        )
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

    @staticmethod
    def _header_value(scope: dict, name: bytes) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                return value.decode("latin-1")
        return None
