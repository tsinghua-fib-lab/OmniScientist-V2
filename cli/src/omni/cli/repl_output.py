"""Route Rich output to the terminal or an active REPL transcript."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from omni.cli.repl_transcript import (
    DataTableData,
    NoticeData,
    TranscriptEvent,
    TranscriptKind,
)
from omni.memory.sanitize import redact_secrets
from omni.runtime.logging_config import (
    attach_rotating_file_handler,
    remove_logging_handler,
    rolled_backup_count,
    rotation_limits,
)

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


class _DiagnosticRecorder:
    """Thread-safe rotating diagnostic target that never writes to the TUI."""

    def __init__(
        self,
        path: Path | None,
        *,
        max_bytes: int | None = None,
        backup_count: int | None = None,
        files: int | None = None,
    ) -> None:
        self.handler: logging.Handler = logging.NullHandler()
        self._handler_logger: logging.Logger | None = None
        if path is not None:
            owner = logging.Logger(
                f"omni.tui.diagnostics.{id(self)}",
                level=logging.INFO,
            )
            owner.propagate = False
            size, resolved_files = rotation_limits(
                max_bytes=max_bytes, files=files, backup_count=backup_count
            )
            self.handler = attach_rotating_file_handler(
                owner,
                path=path,
                component="cli",
                level=logging.INFO,
                max_bytes=size,
                backup_count=rolled_backup_count(resolved_files),
            )
            self._handler_logger = owner
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
        if self._handler_logger is not None:
            remove_logging_handler(self._handler_logger, self.handler)
            self._handler_logger = None
            return
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
    max_bytes: int | None = None,
    backup_count: int | None = None,
    files: int | None = None,
) -> Iterator[None]:
    """Give one TUI exclusive ownership of process output until it closes."""
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    recorder = _DiagnosticRecorder(
        Path(diagnostic_log_path) if diagnostic_log_path is not None else None,
        max_bytes=max_bytes,
        backup_count=backup_count,
        files=files,
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
    elif isinstance(event.payload, NoticeData):
        payload = {
            "title": event.payload.title,
            "message": event.payload.message,
            "actions": list(event.payload.actions),
            "style": event.payload.style,
            "glyph": event.payload.glyph,
            "fallback": event.payload.fallback,
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
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="backslashreplace"
    )
    return _WIRE_PREFIX + encoded + b"\n"


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
            elif isinstance(payload, dict):
                # A card: the only other structured payload, and the kind alone
                # (STATUS / ERROR) does not distinguish it from ordinary markup.
                payload = NoticeData(
                    title=str(payload.get("title", "")),
                    message=str(payload.get("message", "")),
                    actions=tuple(str(value) for value in payload.get("actions", [])),
                    style=str(payload.get("style", "")),
                    glyph=str(payload.get("glyph", "!")),
                    fallback=str(payload.get("fallback", "!")),
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
    if isinstance(event.payload, NoticeData):
        # A sink with no renderer gets the words, not the layout: this stream
        # has no width to lay a card out against.
        lines = [event.payload.title, event.payload.message, *event.payload.actions]
        return "\n".join(line for line in lines if line) + "\n"
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
        stream = self._stream()
        return stream.write(_encodable_text(text, stream))

    def flush(self) -> None:
        if get_output_sink() is None:
            self._stream().flush()


def _encodable_text(text: str, stream: io.TextIOBase) -> str:
    """Replace only characters unsupported by a legacy output encoding."""
    encoding = str(getattr(stream, "encoding", None) or "utf-8")
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding)
    except LookupError:
        return text
    return text


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
