"""Phase 6 audit-fix tests: cancellation during MCP startup, and half-open
probe reservation release on cancellation (manager level).

All deterministic — fakes replace the stdio transport and session; no
subprocesses are spawned.
"""

import asyncio
from types import SimpleNamespace

import pytest

import backend.mcp.client_manager as client_manager_module
from backend.mcp.client_manager import MCPClientManager, MultiMCPManager
from backend.mcp.errors import TransportError
from backend.reliability import CircuitBreaker, CircuitState


class FakeTransport:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return object(), object()

    async def __aexit__(self, *exc_info):
        self.exited += 1
        return False


class FakeSession:
    def __init__(self, initialize_behavior):
        self.initialize_calls = 0
        self._behavior = initialize_behavior

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        self.initialize_calls += 1
        await self._behavior()

    async def list_tools(self):
        return SimpleNamespace(tools=[])


def _install(monkeypatch, transport: FakeTransport, session: FakeSession):
    monkeypatch.setattr(client_manager_module, "stdio_client", lambda params: transport)
    monkeypatch.setattr(
        client_manager_module,
        "ClientSession",
        lambda read, write, read_timeout_seconds=None: session,
    )


def _manager() -> MCPClientManager:
    return MCPClientManager("audit", "python", ["-c", "pass"], call_timeout_seconds=5)


async def _cancel_soon(task: asyncio.Task, after: float = 0.1):
    await asyncio.sleep(after)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_startup_cancellation_during_initialize_propagates_and_cleans_up(monkeypatch):
    transport = FakeTransport()
    init_calls = {"n": 0}

    async def hang_initialize():
        init_calls["n"] += 1
        await asyncio.Event().wait()  # hangs until cancelled

    _install(monkeypatch, transport, FakeSession(hang_initialize))
    mgr = _manager()

    task = asyncio.create_task(mgr.start())
    await _cancel_soon(task)

    assert init_calls["n"] == 1, "cancelled startup must not be retried"
    assert transport.exited == 1, "spawned transport must be cleaned up on cancellation"
    assert mgr._session is None and mgr._stack is None


async def test_startup_cancellation_during_backoff_does_not_trigger_retry(monkeypatch):
    transport = FakeTransport()
    init_calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fail_then_backoff_hangs():
        init_calls["n"] += 1
        if init_calls["n"] == 1:
            raise RuntimeError("transient spawn failure")
        await asyncio.Event().wait()

    async def fake_sleep(seconds: float) -> None:
        if seconds > 0.5:
            await asyncio.Event().wait()  # hang inside the retry backoff
        await real_sleep(0)

    _install(monkeypatch, transport, FakeSession(fail_then_backoff_hangs))
    monkeypatch.setattr(client_manager_module.asyncio, "sleep", fake_sleep)
    mgr = _manager()

    task = asyncio.create_task(mgr.start())
    await _cancel_soon(task)

    assert init_calls["n"] == 1, "cancellation during backoff must not trigger another attempt"
    assert transport.exited == 1


async def test_transient_startup_failure_still_gets_bounded_retry(monkeypatch):
    transport = FakeTransport()
    init_calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fail_once_then_succeed():
        init_calls["n"] += 1
        if init_calls["n"] == 1:
            raise RuntimeError("transient spawn failure")

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(min(seconds, 0.01))

    _install(monkeypatch, transport, FakeSession(fail_once_then_succeed))
    monkeypatch.setattr(client_manager_module.asyncio, "sleep", fast_sleep)
    mgr = _manager()

    await mgr.start()
    assert init_calls["n"] == 2, "one bounded retry for a transient failure"
    assert mgr._session is not None
    await mgr.stop()
    assert transport.exited == 2, "each attempt's transport is cleaned up"


async def test_existing_transport_error_classification_unchanged(monkeypatch):
    transport = FakeTransport()
    real_sleep = asyncio.sleep

    async def always_fails():
        raise RuntimeError("permanent configuration error")

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(min(seconds, 0.01))

    _install(monkeypatch, transport, FakeSession(always_fails))
    monkeypatch.setattr(client_manager_module.asyncio, "sleep", fast_sleep)
    mgr = _manager()
    with pytest.raises(TransportError):
        await mgr.start()
    assert transport.exited == 2


class _HangingCallManager:
    name = "audit"

    def __init__(self):
        self.hang = True
        self.calls: list[str] = []

    def tools(self):
        return []

    async def refresh_tools(self):
        return self.tools()

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if self.hang:
            await asyncio.Event().wait()
        return SimpleNamespace(structured_content={"ok": True}, content=[])


class _FactoryManager:
    """Async context manager returning the current inner manager."""

    def __init__(self, holder: dict):
        self._holder = holder

    async def __aenter__(self):
        return self._holder["current"]

    async def __aexit__(self, *exc_info):
        return False


async def test_cancelled_half_open_probe_releases_reservation_via_manager():
    """Cancellation during a half-open probe must free the reservation without
    recording success or failure; the next call acquires a fresh probe."""
    holder = {"current": _HangingCallManager()}
    manager = MultiMCPManager({"audit": lambda: _FactoryManager(holder)})
    async with manager:
        breaker = manager.breakers["audit"]
        breaker.record_failure()  # threshold default 3; force the state instead
        breaker.state = CircuitState.open
        # cooldown 60s has not elapsed -> open rejects; shrink for the test
        breaker.cooldown_seconds = 0.0

        call_task = asyncio.create_task(manager.call_tool("audit", "t", {}))
        await asyncio.sleep(0.05)
        assert breaker.state == CircuitState.half_open
        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

        # reservation released, state preserved, nothing counted
        assert breaker.state == CircuitState.half_open
        assert breaker.failures == 1  # unchanged by the cancelled probe

        # a subsequent call acquires a fresh probe and closes the breaker
        holder["current"].hang = False
        result = await manager.call_tool("audit", "t", {})
        assert result.structured_content == {"ok": True}
        assert breaker.state == CircuitState.closed


async def test_cancelled_probe_is_neither_success_nor_failure():
    holder = {"current": _HangingCallManager()}
    manager = MultiMCPManager({"audit": lambda: _FactoryManager(holder)})
    async with manager:
        breaker = manager.breakers["audit"]
        breaker.state = CircuitState.open  # force open; cooldown 60s pending
        breaker.cooldown_seconds = 0.0

        call_task = asyncio.create_task(manager.call_tool("audit", "t", {}))
        await asyncio.sleep(0.05)
        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

        assert breaker.state == CircuitState.half_open
        failures_after_cancel = breaker.failures
        call_task2 = asyncio.create_task(manager.call_tool("audit", "t", {}))
        await asyncio.sleep(0.05)
        call_task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task2
        assert breaker.failures == failures_after_cancel, "cancelled probes never count"
        assert breaker.state == CircuitState.half_open
