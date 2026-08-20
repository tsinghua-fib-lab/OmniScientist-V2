"""Foreground ``omni web`` process: treat Ctrl+C / Ctrl+D as a normal stop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

# Browser ``task.watch`` SSE stays open while the UI is open. Uvicorn's default
# is to wait forever for those connections (until a second Ctrl+C). A short
# bound keeps the first interrupt snappy.
GRACEFUL_SHUTDOWN_S = 0.5


def apply_stdin_chunk(server: Any, data: bytes) -> bool:
    """Treat an empty stdin read (Ctrl+D / EOF) as a normal stop request."""
    if data:
        return False
    server.should_exit = True
    return True


def quiet_web_server_class(uvicorn: Any) -> type:
    """Build the uvicorn.Server subclass after the [web] extra is imported."""

    class OmniWebServer(uvicorn.Server):
        def __init__(self, config: Any, *, on_ready: Callable[[], None] | None = None) -> None:
            super().__init__(config)
            self._on_ready = on_ready
            self._stdin_fd: int | None = None

        async def startup(self, sockets=None):  # noqa: ANN001
            await super().startup(sockets=sockets)
            if self._on_ready is not None and self.started:
                self._on_ready()
            self._install_stdin_eof_watcher()

        async def shutdown(self, sockets=None):  # noqa: ANN001
            self._remove_stdin_eof_watcher()
            await super().shutdown(sockets=sockets)

        def _install_stdin_eof_watcher(self) -> None:
            if self._stdin_fd is not None:
                return
            try:
                if not sys.stdin or not sys.stdin.isatty():
                    return
                fd = sys.stdin.fileno()
            except (AttributeError, OSError, ValueError):
                return
            loop = asyncio.get_running_loop()

            def _on_readable() -> None:
                try:
                    chunk = os.read(fd, 1024)
                except OSError:
                    chunk = b""
                if apply_stdin_chunk(self, chunk):
                    self._remove_stdin_eof_watcher()

            try:
                loop.add_reader(fd, _on_readable)
            except (OSError, RuntimeError, NotImplementedError):
                return
            self._stdin_fd = fd

        def _remove_stdin_eof_watcher(self) -> None:
            fd = self._stdin_fd
            if fd is None:
                return
            self._stdin_fd = None
            try:
                asyncio.get_running_loop().remove_reader(fd)
            except (OSError, RuntimeError, ValueError):
                return

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            """Install the stop handler without re-raising SIGINT afterwards.

            uvicorn re-raises the captured SIGINT after ``serve()`` returns so
            the process looks like it died from Ctrl+C. On Python 3.13,
            ``asyncio.run`` maps that late SIGINT to ``KeyboardInterrupt``,
            cancels the lifespan / SSE tasks, and httptools logs
            ``ERROR: Exception in ASGI application``. Stopping ``omni web`` is
            a normal user action — exit quietly instead.
            """
            from uvicorn.server import HANDLED_SIGNALS

            if threading.current_thread() is not threading.main_thread():
                yield
                return
            original_handlers = {
                sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS
            }
            try:
                yield
            finally:
                for sig, handler in original_handlers.items():
                    signal.signal(sig, handler)

        def run(self, sockets=None):  # noqa: ANN001
            # Delegate loop setup to uvicorn: 0.36+ removed
            # ``Config.setup_event_loop`` in favor of ``get_loop_factory``.
            try:
                super().run(sockets=sockets)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                return

    return OmniWebServer


def run_web_server(app: Any, *, host: str, port: int, on_ready: Callable[[], None]) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level="warning",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    quiet_web_server_class(uvicorn)(config, on_ready=on_ready).run()
