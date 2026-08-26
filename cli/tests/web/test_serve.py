"""Ctrl+C / Ctrl+D must stop ``omni web`` without ASGI crash dumps."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import time
from types import SimpleNamespace

import pytest

from omni.runtime.logging_config import quiet_uvicorn_console
from omni.web.serve import apply_stdin_chunk, quiet_web_server_class
from omni.web.workspace import close_workspace_hub

uvicorn = pytest.importorskip("uvicorn")

# SQLAlchemy aiosqlite teardown from an earlier test can GC a pending
# ``_terminate_graceful_close`` task during this file's SSE shutdown. That
# asyncio ERROR is not a web-server crash.
_FOREIGN_ERROR_LOGGERS = ("asyncio", "sqlalchemy")


def _assert_no_web_shutdown_errors(caplog: pytest.LogCaptureFixture) -> None:
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Cancel 1 running task(s)" not in messages
    assert "Exception in ASGI application" not in messages
    errors = [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and not record.name.startswith(_FOREIGN_ERROR_LOGGERS)
    ]
    assert errors == []


def test_empty_stdin_chunk_is_a_normal_stop() -> None:
    server = SimpleNamespace(should_exit=False)
    assert apply_stdin_chunk(server, b"typed text") is False
    assert server.should_exit is False
    assert apply_stdin_chunk(server, b"") is True
    assert server.should_exit is True


def test_capture_signals_does_not_reraise_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    raised: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", lambda sig: raised.append(sig))
    config = uvicorn.Config(lambda *_a, **_k: None, host="127.0.0.1", port=0, log_level="warning")
    server = quiet_web_server_class(uvicorn)(config)
    server._captured_signals.append(signal.SIGINT)
    with server.capture_signals():
        pass
    assert raised == []


def test_run_swallows_keyboard_interrupt_from_asyncio() -> None:
    config = uvicorn.Config(lambda *_a, **_k: None, host="127.0.0.1", port=0, log_level="warning")
    server = quiet_web_server_class(uvicorn)(config)

    async def _boom(sockets=None):  # noqa: ANN001
        raise KeyboardInterrupt

    server.serve = _boom  # type: ignore[method-assign]
    server.run()


@pytest.mark.asyncio
async def test_shutdown_runs_application_hook_before_uvicorn_drains_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    config = uvicorn.Config(
        lambda *_a, **_k: None,
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = quiet_web_server_class(uvicorn)(
        config,
        before_shutdown=lambda: order.append("application"),
    )

    async def _quiet(sockets=None):  # noqa: ANN001
        order.append("uvicorn")

    server._quiet_shutdown = _quiet  # type: ignore[method-assign]
    await server.shutdown()

    assert order == ["application", "uvicorn"]


@pytest.mark.asyncio
async def test_live_sse_finishes_before_uvicorn_forces_request_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    stop_stream = asyncio.Event()

    async def app(scope, receive, send):  # noqa: ANN001
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: ready\n\n",
                "more_body": True,
            }
        )
        await stop_stream.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 0))
    except PermissionError:
        listener.close()
        pytest.skip("this sandbox does not permit loopback sockets")
    listener.listen()
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=0.1,
    )
    server = quiet_web_server_class(uvicorn)(config, before_shutdown=stop_stream.set)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"data: ready\n\n"), timeout=1)
        assert b"200 OK" in response

        server.should_exit = True
        await asyncio.wait_for(serving, timeout=2)
        writer.close()
        await writer.wait_closed()
    finally:
        if not serving.done():
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=2)
        listener.close()

    _assert_no_web_shutdown_errors(caplog)


@pytest.mark.asyncio
async def test_stuck_sse_force_cancel_is_not_an_asgi_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_uvicorn_console()
    caplog.clear()
    caplog.set_level(logging.INFO)

    async def app(scope, receive, send):  # noqa: ANN001
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: ready\n\n",
                "more_body": True,
            }
        )
        await asyncio.Event().wait()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 0))
    except PermissionError:
        listener.close()
        pytest.skip("this sandbox does not permit loopback sockets")
    listener.listen()
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=0.1,
    )
    server = quiet_web_server_class(uvicorn)(config)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"data: ready\n\n"), timeout=1)
        assert b"200 OK" in response
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=2)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        if not serving.done():
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=2)
        listener.close()

    _assert_no_web_shutdown_errors(caplog)


@pytest.mark.asyncio
async def test_close_workspace_hub_does_not_block_on_a_slow_store() -> None:
    class SlowHub:
        async def aclose(self) -> None:
            await asyncio.sleep(10)

    started = time.monotonic()
    await close_workspace_hub(SlowHub(), timeout=0.05)  # type: ignore[arg-type]
    assert time.monotonic() - started < 1.0
