"""Ctrl+C / Ctrl+D must stop ``omni web`` without ASGI crash dumps."""

from __future__ import annotations

import asyncio
import signal
import time
from types import SimpleNamespace

import pytest

from omni.web.serve import apply_stdin_chunk, quiet_web_server_class
from omni.web.workspace import close_workspace_hub

uvicorn = pytest.importorskip("uvicorn")


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
async def test_close_workspace_hub_does_not_block_on_a_slow_store() -> None:
    class SlowHub:
        async def aclose(self) -> None:
            await asyncio.sleep(10)

    started = time.monotonic()
    await close_workspace_hub(SlowHub(), timeout=0.05)  # type: ignore[arg-type]
    assert time.monotonic() - started < 1.0
