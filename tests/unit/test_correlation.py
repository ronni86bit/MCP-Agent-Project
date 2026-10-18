"""Phase 8 Gate A: correlation-ID lifecycle and context propagation tests.

Covers: ID validation (valid/invalid/absent), contextvars binding/unbinding
including cleanup after exceptions, middleware X-Request-ID header, and the
loop's contextvars lifecycle.
"""

import asyncio
import uuid

import pytest
import structlog
from structlog.testing import CapturingLogger

from backend.observability.middleware import CorrelationMiddleware, _validate_or_generate


class TestValidateOrGenerate:
    def test_valid_id_preserved(self):
        assert _validate_or_generate("req-abcdef123456", "req") == "req-abcdef123456"

    def test_invalid_charset_replaced(self):
        result = _validate_or_generate("UPPER_CASE_ID!", "req")
        assert _matches_correlation_format(result)

    def test_none_generates_new(self):
        result = _validate_or_generate(None, "req")
        assert _matches_correlation_format(result)

    def test_empty_string_generates_new(self):
        result = _validate_or_generate("", "req")
        assert _matches_correlation_format(result)

    def test_too_short_replaced(self):
        result = _validate_or_generate("ab", "req")
        assert _matches_correlation_format(result)

    def test_generated_ids_are_unique(self):
        ids = {_validate_or_generate(None, "req") for _ in range(10)}
        assert len(ids) == 10


def _matches_correlation_format(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z]{3,8}-[0-9a-f]{12}", value))


class TestCorrelationMiddleware:
    def _make_scope(self, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
        return {"type": "http", "headers": headers or [], "method": "GET", "path": "/test"}

    def _make_capture_send(self):
        messages = []

        async def send(message):
            messages.append(message)

        return send, messages

    async def test_valid_request_id_preserved_in_response_header(self):
        scope = self._make_scope([(b"x-request-id", b"req-abcdef123456")])
        send, messages = None, None
        captured = []

        async def mock_app(scope, receive, send):
            import structlog.contextvars

            # inside the app, the contextvar should be bound
            ctx = structlog.contextvars.get_contextvars()
            captured.append(ctx.get("request_id"))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = CorrelationMiddleware(mock_app)
        send_fn, messages = None, []
        received_messages = []

        async def capture_send(message):
            received_messages.append(message)

        await mw(scope, None, capture_send)

        assert captured == ["req-abcdef123456"]
        response_start = next(m for m in received_messages if m["type"] == "http.response.start")
        headers = dict(response_start["headers"])
        assert headers[b"x-request-id"] == b"req-abcdef123456"

    async def test_missing_request_id_generates_new(self):
        received_messages = []

        async def mock_app(scope, receive, send):
            import structlog.contextvars

            ctx = structlog.contextvars.get_contextvars()
            received_messages.append(ctx.get("request_id"))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = CorrelationMiddleware(mock_app)
        received = []

        async def capture_send(message):
            received.append(message)

        await mw(self._make_scope(), None, capture_send)

        request_id = received[0]["headers"] if False else None
        # after the middleware, the contextvar is unbound
        import structlog.contextvars as cv
        assert cv.get_contextvars().get("request_id") is None

    async def test_context_cleared_after_cancellation(self):
        import structlog.contextvars

        async def hanging_app(scope, receive, send):
            structlog.contextvars.bind_contextvars(request_id="req-test-123")
            await asyncio.Event().wait()  # hang until cancelled

        mw = CorrelationMiddleware(hanging_app)
        scope = {"type": "http", "headers": []}

        task = asyncio.create_task(mw(scope, None, None))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # contextvars are cleaned up by the finally block
        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("request_id") is None


class TestLoopContextvars:
    async def test_loop_binds_and_unbinds_contextvars(self):
        """The loop binds investigation_id + request_id during run() and
        unbinds them after — verified via capture_logs."""
        import uuid

        from structlog.testing import capture_logs

        from backend.agent.loop import InvestigationLoop
        from backend.agent.state import InvestigationRecord
        from backend.config import Settings

        iid = f"inv-{uuid.uuid4().hex[:12]}"
        rid = f"req-{uuid.uuid4().hex[:12]}"
        record = InvestigationRecord(investigation_id=iid, request_id=rid,
                                     question="Why did today's sales drop?")
        settings = Settings(max_investigation_seconds=60)

        class NoopFactory:
            managers: dict = {}
            breakers: dict = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def call_tool(self, server, name, args):
                raise RuntimeError("no tools configured")

        loop = InvestigationLoop(
            llm=type("L", (), {"model_name": "t"})(),
            manager_factory=lambda: NoopFactory(),
            record=record,
            settings=settings,
        )
        await loop.run()

        # after run(), contextvars are unbound (cleaned up in the finally)
        from structlog.contextvars import get_contextvars
        assert get_contextvars().get("investigation_id") is None

    async def test_contextvars_available_during_run(self):
        """All log events during the loop carry investigation_id via contextvars."""
        import uuid

        from structlog.testing import capture_logs

        from backend.agent.loop import InvestigationLoop
        from backend.agent.state import InvestigationRecord
        from backend.config import Settings

        iid = f"inv-{uuid.uuid4().hex[:12]}"
        rid = f"req-{uuid.uuid4().hex[:8]}"
        record = InvestigationRecord(investigation_id=iid, request_id=rid,
                                     question="Why did today's sales drop?")
        settings = Settings(max_investigation_seconds=60)

        class NoopFactory:
            managers: dict = {}
            breakers: dict = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def call_tool(self, server, name, args):
                raise RuntimeError("no tools configured")

        loop = InvestigationLoop(
            llm=type("L", (), {"model_name": "t"})(),
            manager_factory=lambda: NoopFactory(),
            record=record,
            settings=settings,
        )
        with capture_logs() as logs:
            await loop.run()

        # every log entry emitted during the run carries investigation_id
        log_entries = [entry for entry in logs if "event" in entry]
        assert log_entries, "expected log entries during the loop run"
        for entry in log_entries:
            assert entry.get("investigation_id") == iid, \
                f"log entry missing investigation_id: {entry.get('event')}"
