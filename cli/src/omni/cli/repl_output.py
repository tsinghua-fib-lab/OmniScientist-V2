"""Route Rich output to the terminal or an active REPL transcript."""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from omni.cli.repl_transcript import DataTableData, TranscriptEvent, TranscriptKind
from omni.memory.sanitize import redact_secrets

TRANSCRIPT_PROTOCOL_ENV = "OMNI_TRANSCRIPT_PROTOCOL"
_WIRE_PREFIX = b"\x1eOMNI_EVENT "


class OutputSink(Protocol):
    """Minimal sink contract used by the interactive TUI."""

    def write(self, text: str) -> None: ...

    def publish_event(self, event: TranscriptEvent) -> None: ...

    def set_status(self, text: str) -> None: ...

    def clear(self) -> None: ...

    def redraw(self) -> None: ...


_lock = threading.RLock()
_active_sink: OutputSink | None = None
_output_turn_id: ContextVar[str] = ContextVar("omni_output_turn_id", default="")


def get_output_sink() -> OutputSink | None:
    """Return the process-wide interactive sink, if one is active."""
    with _lock:
        return _active_sink


@contextmanager
def use_output_sink(sink: OutputSink | None) -> Iterator[None]:
    """Temporarily route all shared Rich consoles to ``sink``."""
    global _active_sink
    with _lock:
        previous = _active_sink
        _active_sink = sink
    try:
        yield
    finally:
        with _lock:
            _active_sink = previous


@contextmanager
def use_output_turn(turn_id: str) -> Iterator[None]:
    """Associate all output in this context with one submitted user turn."""
    token = _output_turn_id.set(str(turn_id or ""))
    try:
        yield
    finally:
        _output_turn_id.reset(token)


def current_output_turn_id() -> str:
    """Return the turn currently responsible for process output."""
    return _output_turn_id.get()


def bind_event_to_output_turn(event: TranscriptEvent) -> TranscriptEvent:
    """Apply the active output turn while preserving event semantics."""
    turn_id = current_output_turn_id()
    if not turn_id or event.turn_id == turn_id:
        return event
    return replace(event, turn_id=turn_id)


def set_output_status(text: str) -> bool:
    """Publish transient status to the active TUI; return whether it was handled."""
    sink = get_output_sink()
    if sink is None:
        return False
    sink.set_status(text)
    return True


def clear_active_output() -> bool:
    """Clear the active transcript without emitting terminal control codes."""
    sink = get_output_sink()
    if sink is None:
        return False
    sink.clear()
    return True


def redraw_active_output() -> bool:
    """Force a full repaint without deleting the active transcript."""
    sink = get_output_sink()
    if sink is None:
        return False
    redraw = getattr(sink, "redraw", None)
    if not callable(redraw):
        return False
    redraw()
    return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class _DiagnosticRecorder:
    """Thread-safe rotating diagnostic target that never writes to the TUI."""

    def __init__(self, path: Path | None) -> None:
        self.handler: logging.Handler = logging.NullHandler()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            handler.setFormatter(
                _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            self.handler = handler
        self._lock = threading.RLock()

    def write(self, text: str) -> None:
        value = redact_secrets(str(text)).rstrip("\n")
        if not value or isinstance(self.handler, logging.NullHandler):
            return
        with self._lock:
            for line in value.splitlines():
                record = logging.LogRecord(
                    name="omni.tui.stderr",
                    level=logging.WARNING,
                    pathname="",
                    lineno=0,
                    msg=line,
                    args=(),
                    exc_info=None,
                )
                self.handler.handle(record)

    def flush(self) -> None:
        self.handler.flush()

    def close(self) -> None:
        self.handler.close()


class _DiagnosticTextIO(io.TextIOBase):
    """Send raw stderr to diagnostics while a managed TUI owns the terminal."""

    def __init__(self, target: io.TextIOBase, recorder: _DiagnosticRecorder) -> None:
        super().__init__()
        self._target = target
        self._recorder = recorder

    @property
    def encoding(self) -> str:
        return str(getattr(self._target, "encoding", None) or "utf-8")

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False if get_output_sink() is not None else bool(self._target.isatty())

    def fileno(self) -> int:
        return self._target.fileno()

    def write(self, text: str) -> int:
        if not text:
            return 0
        if get_output_sink() is not None:
            self._recorder.write(text)
            return len(text)
        return self._target.write(text)

    def flush(self) -> None:
        if get_output_sink() is not None:
            self._recorder.flush()
        else:
            self._target.flush()


class _ManagedLogging:
    """Temporarily keep Python logging handlers away from the managed terminal."""

    def __init__(self, handler: logging.Handler) -> None:
        self._handler = handler
        self._root = logging.getLogger()
        self._root_handlers: list[logging.Handler] = []
        self._root_level = self._root.level
        self._named_handlers: list[tuple[logging.Logger, list[logging.Handler]]] = []

    def __enter__(self) -> None:
        self._root_handlers = list(self._root.handlers)
        durable = [handler for handler in self._root_handlers if _is_durable_handler(handler)]
        self._root.handlers = [*durable, self._handler]
        if self._root.level == logging.NOTSET or self._root.level > logging.INFO:
            self._root.setLevel(logging.INFO)

        for candidate in logging.Logger.manager.loggerDict.values():
            if not isinstance(candidate, logging.Logger):
                continue
            handlers = list(candidate.handlers)
            filtered = [handler for handler in handlers if not _is_terminal_handler(handler)]
            if filtered == handlers:
                continue
            self._named_handlers.append((candidate, handlers))
            candidate.handlers = filtered
            if not candidate.propagate:
                candidate.handlers.append(self._handler)

    def __exit__(self, *_exc: object) -> None:
        for logger, handlers in self._named_handlers:
            logger.handlers = handlers
        self._named_handlers.clear()
        self._root.handlers = self._root_handlers
        self._root.setLevel(self._root_level)


def _is_terminal_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.StreamHandler) and not isinstance(
        handler, logging.FileHandler
    )


def _is_durable_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.FileHandler)


@contextmanager
def use_managed_output_sink(
    sink: OutputSink,
    *,
    diagnostic_log_path: str | os.PathLike[str] | None = None,
) -> Iterator[None]:
    """Give one TUI exclusive ownership of process output until it closes."""
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    recorder = _DiagnosticRecorder(
        Path(diagnostic_log_path) if diagnostic_log_path is not None else None
    )
    stdout_proxy = RoutedTextIO(lambda: original_stdout)
    stderr_proxy = _DiagnosticTextIO(original_stderr, recorder)
    logging_context = _ManagedLogging(recorder.handler)
    with use_output_sink(sink):
        sys.stdout = stdout_proxy
        sys.stderr = stderr_proxy
        try:
            with logging_context:
                yield
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            recorder.close()


def publish_transcript_event(
    event: TranscriptEvent,
    *,
    stream: io.TextIOBase | None = None,
) -> bool:
    """Publish to the active TUI or an opt-in child-process event stream."""
    event = bind_event_to_output_turn(event)
    sink = get_output_sink()
    if sink is not None:
        publisher = getattr(sink, "publish_event", None)
        if callable(publisher):
            publisher(event)
        else:
            sink.write(_event_text_fallback(event))
        return True
    if os.environ.get(TRANSCRIPT_PROTOCOL_ENV) != "1":
        return False
    target = sys.stdout if stream is None else stream
    encoded = encode_transcript_event(event)
    binary = getattr(target, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
    else:
        target.write(encoded.decode("utf-8"))
        target.flush()
    return True


def encode_transcript_event(event: TranscriptEvent) -> bytes:
    """Encode one serializable transcript event for a local child process."""
    if isinstance(event.payload, DataTableData):
        payload: str | dict[str, object] = {
            "title": event.payload.title,
            "columns": list(event.payload.columns),
            "rows": [list(row) for row in event.payload.rows],
            "layout": event.payload.layout,
            "row_styles": list(event.payload.row_styles),
        }
    else:
        payload = event.payload
    record = {
        "kind": event.kind.value,
        "payload": payload,
        "turn_id": event.turn_id,
        "replace_key": event.replace_key,
        "state": event.state,
        "final": event.final,
        "foldable": event.foldable,
        "initially_collapsed": event.initially_collapsed,
    }
    return _WIRE_PREFIX + json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class TranscriptWireDecoder:
    """Incrementally decode mixed semantic records and ordinary child output."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[TranscriptEvent]:
        self._buffer.extend(chunk)
        events: list[TranscriptEvent] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[: newline + 1])
            del self._buffer[: newline + 1]
            events.append(_decode_wire_line(line))
        return events

    def finish(self) -> list[TranscriptEvent]:
        if not self._buffer:
            return []
        line = bytes(self._buffer)
        self._buffer.clear()
        return [_decode_wire_line(line)]


def _decode_wire_line(line: bytes) -> TranscriptEvent:
    if line.startswith(_WIRE_PREFIX):
        try:
            data = json.loads(line[len(_WIRE_PREFIX) :])
            kind = TranscriptKind(str(data["kind"]))
            payload = data.get("payload", "")
            if kind == TranscriptKind.DATA_TABLE and isinstance(payload, dict):
                payload = DataTableData(
                    title=str(payload.get("title", "")),
                    columns=tuple(str(value) for value in payload.get("columns", [])),
                    rows=tuple(
                        tuple(str(value) for value in row)
                        for row in payload.get("rows", [])
                    ),
                    layout=str(payload.get("layout", "auto")),
                    row_styles=tuple(
                        str(value) for value in payload.get("row_styles", [])
                    ),
                )
            return TranscriptEvent(
                kind=kind,
                payload=payload,
                turn_id=str(data.get("turn_id") or ""),
                replace_key=str(data.get("replace_key") or ""),
                state=str(data.get("state") or ""),
                final=bool(data.get("final") or False),
                foldable=bool(data.get("foldable") or False),
                initially_collapsed=bool(data.get("initially_collapsed") or False),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return TranscriptEvent(
        kind=TranscriptKind.RAW_COMMAND_OUTPUT,
        payload=line.decode("utf-8", "replace"),
    )


def _event_text_fallback(event: TranscriptEvent) -> str:
    if isinstance(event.payload, str):
        return event.payload
    rows = [event.payload.title, " | ".join(event.payload.columns)]
    rows.extend(" | ".join(row) for row in event.payload.rows)
    return "\n".join(rows) + "\n"


class RoutedTextIO(io.TextIOBase):
    """Text stream that writes to the active sink or a normal terminal stream."""

    def __init__(self, target: Callable[[], io.TextIOBase]) -> None:
        super().__init__()
        self._target = target

    def _stream(self) -> io.TextIOBase:
        """Current fallback stream, with any Rich ``FileProxy`` unwrapped.

        ``console.status(...)`` and ``rich.live.Live`` redirect the *global*
        ``sys.stdout`` to a ``FileProxy`` that writes straight back into this
        console. Because our target is read lazily from ``sys.stdout``, following
        that proxy makes ``write`` recurse forever (console → file → proxy →
        console → …) and wedges the process — a tight loop that even swallows
        Ctrl+C. Unwrapping to the proxied real stream keeps output on the
        terminal and breaks the cycle, while leaving non-Rich stream swaps
        (e.g. pytest capture) untouched.
        """
        stream = self._target()
        seen: set[int] = set()
        while id(stream) not in seen:
            proxied = getattr(stream, "rich_proxied_file", None)
            if proxied is None:
                break
            seen.add(id(stream))
            stream = proxied
        return stream

    @property
    def encoding(self) -> str:
        return str(getattr(self._stream(), "encoding", None) or "utf-8")

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        if get_output_sink() is not None:
            return False
        try:
            return bool(self._stream().isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def fileno(self) -> int:
        return self._stream().fileno()

    def write(self, text: str) -> int:
        if not text:
            return 0
        sink = get_output_sink()
        if sink is not None:
            sink.write(text)
            return len(text)
        return self._stream().write(text)

    def flush(self) -> None:
        if get_output_sink() is None:
            self._stream().flush()


__all__ = [
    "OutputSink",
    "RoutedTextIO",
    "TRANSCRIPT_PROTOCOL_ENV",
    "TranscriptWireDecoder",
    "bind_event_to_output_turn",
    "clear_active_output",
    "current_output_turn_id",
    "encode_transcript_event",
    "get_output_sink",
    "publish_transcript_event",
    "redraw_active_output",
    "set_output_status",
    "use_managed_output_sink",
    "use_output_sink",
    "use_output_turn",
]
