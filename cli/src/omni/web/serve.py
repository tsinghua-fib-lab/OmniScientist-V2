"""Foreground ``omni web`` process: treat Ctrl+C / Ctrl+D as a normal stop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

from omni.runtime.logging_config import quiet_uvicorn_console

# Browser ``task.watch`` SSE stays open while the UI is open. Omni closes those
# streams before asking Uvicorn to drain requests; this remains a last-resort
# bound for a genuinely stuck ASGI request rather than the normal exit path.
GRACEFUL_SHUTDOWN_S = 2.0

logger = logging.getLogger(__name__)


def apply_stdin_chunk(server: Any, data: bytes) -> bool:
    """Treat an empty stdin read (Ctrl+D / EOF) as a normal stop request."""
    if data:
        return False
    server.should_exit = True
    return True


def quiet_web_server_class(uvicorn: Any) -> type:
    """Build the uvicorn.Server subclass after the [web] extra is imported."""

    class OmniWebServer(uvicorn.Server):
        def __init__(
            self,
            config: Any,
            *,
            on_ready: Callable[[], None] | None = None,
            before_shutdown: Callable[[], None] | None = None,
        ) -> None:
            super().__init__(config)
            self._on_ready = on_ready
            self._before_shutdown = before_shutdown
            self._stdin_fd: int | None = None

        async def startup(self, sockets=None):  # noqa: ANN001
            await super().startup(sockets=sockets)
            if self._on_ready is not None and self.started:
                self._on_ready()
            self._install_stdin_eof_watcher()

        async def shutdown(self, sockets=None):  # noqa: ANN001
            self._remove_stdin_eof_watcher()
            if self._before_shutdown is not None:
                try:
                    self._before_shutdown()
                except Exception:  # noqa: BLE001 - full shutdown must still run
                    logger.exception(
                        "web request-drain hook failed",
                        extra={"event": "server.shutdown_hook_failed"},
                    )
            await self._quiet_shutdown(sockets=sockets)

        async def _quiet_shutdown(self, sockets=None):  # noqa: ANN001
            """Uvicorn shutdown without treating leftover SSE as a crash.

            After the drain hook, a still-open ``task.watch`` is cancelled. That
            is a normal Ctrl+C, not ``ERROR: Exception in ASGI application``.
            """
            uv_log = logging.getLogger("uvicorn.error")
            uv_log.info("Shutting down")
            for server in self.servers:
                server.close()
            for sock in sockets or []:
                sock.close()
            for connection in list(self.server_state.connections):
                connection.shutdown()
            await asyncio.sleep(0.1)
            try:
                await asyncio.wait_for(
                    self._wait_tasks_to_complete(),
                    timeout=self.config.timeout_graceful_shutdown,
                )
            except TimeoutError:
                leftover = len(self.server_state.tasks)
                logger.info(
                    "forcing %s leftover web request(s) to end",
                    leftover,
                    extra={"event": "server.shutdown_forced", "tasks": leftover},
                )
                for task in self.server_state.tasks:
                    task.cancel(msg="omni web stopping")
            if not self.force_exit:
                await self.lifespan.shutdown()

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


def run_web_server(
    app: Any,
    *,
    host: str,
    port: int,
    on_ready: Callable[[], None],
    log_level: str | int = "WARNING",
) -> None:
    import uvicorn

    quiet_uvicorn_console()
    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level=log_level,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    hub = getattr(getattr(app, "state", None), "hub", None)
    before_shutdown = getattr(hub, "begin_shutdown", None)
    quiet_web_server_class(uvicorn)(
        config,
        on_ready=on_ready,
        before_shutdown=before_shutdown if callable(before_shutdown) else None,
    ).run()
