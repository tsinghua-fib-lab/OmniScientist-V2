"""Shared, non-invasive process logging primitives.

This module owns the durable log record format and handler lifecycle.  It never
calls ``basicConfig`` or replaces caller-owned handlers; CLI, service, and Web
entry points remain responsible for choosing a file or managed stream.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from omni.memory.sanitize import redact_secrets

_STANDARD_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}
_STANDARD_LEVEL_VALUES = frozenset(_STANDARD_LEVELS.values())

# CSI sequences cover colours and cursor controls; OSC sequences cover terminal
# titles and hyperlinks.  Removing both keeps terminal presentation bytes out of
# durable logs without altering ordinary Unicode text.
_ANSI_ESCAPE_RE = re.compile(r"(?:\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)|\x1B\[[0-?]*[ -/]*[@-~]|\x1B.)")
_UNSAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:/@+-]+")
_HANDLER_KEY_ATTRIBUTE = "_omni_rotating_handler_key"
_STREAM_HANDLER_KEY_ATTRIBUTE = "_omni_stream_handler_key"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
# kimi-code ``files``: live file plus rolled copies. User default is 10 files.
DEFAULT_LOG_FILES = 10
DEFAULT_LOG_BACKUP_COUNT = DEFAULT_LOG_FILES - 1
_UVICORN_ERROR_LOGGER = "uvicorn.error"


def _safe_default_level(default: int) -> int:
    if isinstance(default, bool) or not isinstance(default, int):
        return logging.INFO
    if default not in _STANDARD_LEVEL_VALUES:
        return logging.INFO
    return default


def rolled_backup_count(files: int) -> int:
    """Map kimi-style total files onto ``RotatingFileHandler.backupCount``."""
    try:
        total = int(files)
    except (TypeError, ValueError):
        total = DEFAULT_LOG_FILES
    return max(0, max(1, total) - 1)


def rotation_limits(
    settings: object | None = None,
    *,
    max_bytes: int | None = None,
    files: int | None = None,
    backup_count: int | None = None,
) -> tuple[int, int]:
    """Resolve ``(max_bytes, files)``.

    ``files`` is the live file plus rolled copies (kimi-code ``KIMI_LOG_*_FILES``).
    ``backup_count`` is accepted as an alias for that same total.
    """
    obs = getattr(settings, "observability", None) if settings is not None else None
    raw_bytes = (
        max_bytes
        if max_bytes is not None
        else getattr(obs, "log_max_bytes", DEFAULT_LOG_MAX_BYTES)
    )
    raw_files = files if files is not None else backup_count
    if raw_files is None and obs is not None:
        raw_files = getattr(obs, "log_files", None)
        if raw_files is None:
            raw_files = getattr(obs, "log_backup_count", None)
    if raw_files is None:
        raw_files = DEFAULT_LOG_FILES
    try:
        size = int(raw_bytes)
    except (TypeError, ValueError):
        size = DEFAULT_LOG_MAX_BYTES
    try:
        count = int(raw_files)
    except (TypeError, ValueError):
        count = DEFAULT_LOG_FILES
    return max(1, size), max(1, count)


class UvicornShutdownFilter(logging.Filter):
    """Drop uvicorn's normal-stop noise so Ctrl+C is not an ASGI crash.

    Cancelling a leftover SSE after the drain hook is how ``omni web`` exits.
    uvicorn logs that as ``ERROR`` plus a traceback; the user asked for a
    quiet terminal and an honest file log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "timeout graceful shutdown exceeded" in message:
            return False
        if message.startswith("Exception in ASGI application"):
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None and issubclass(exc_type, BaseException):
                if exc_type.__name__ in {"CancelledError", "KeyboardInterrupt"}:
                    return False
                if record.exc_info[1] is not None and (
                    "timeout graceful shutdown exceeded" in str(record.exc_info[1])
                    or "omni web stopping" in str(record.exc_info[1])
                ):
                    return False
        return True


def quiet_uvicorn_console() -> None:
    """Keep uvicorn off stderr. Records still propagate to the process file."""
    error_logger = logging.getLogger(_UVICORN_ERROR_LOGGER)
    if not any(isinstance(item, UvicornShutdownFilter) for item in error_logger.filters):
        error_logger.addFilter(UvicornShutdownFilter())
    for handler in list(error_logger.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            error_logger.removeHandler(handler)
    error_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False


def parse_log_level(value: str | int | None, *, default: int = logging.INFO) -> int:
    """Return a standard logging level, falling back safely for invalid input."""
    fallback = _safe_default_level(default)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value if value in _STANDARD_LEVEL_VALUES else fallback
    if isinstance(value, str):
        return _STANDARD_LEVELS.get(value.strip().upper(), fallback)
    return fallback


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _record_token(value: object, *, fallback: str = "-") -> str:
    text = redact_secrets(_strip_ansi(str(value))).strip()
    text = _UNSAFE_TOKEN_RE.sub("_", text).strip("_")
    return text or fallback


def _record_message(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    message = record.getMessage()
    if record.exc_info:
        message = f"{message}\n{formatter.formatException(record.exc_info)}"
    if record.stack_info:
        message = f"{message}\n{formatter.formatStack(record.stack_info)}"
    normalized = _strip_ansi(message).replace("\r\n", "\n").replace("\r", "\n")
    return redact_secrets(normalized)


class OmniLogFormatter(logging.Formatter):
    """Format one redacted, injection-safe record on one physical line."""

    def __init__(self, *, component: str) -> None:
        super().__init__()
        self._component = _record_token(component)

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, tz=UTC)
        timestamp = created.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created.microsecond // 1000:03d}Z"
        message = json.dumps(
            _record_message(record, self),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            f"{timestamp} {_record_token(record.levelname)} "
            f"component={self._component} logger={_record_token(record.name)} "
            f"pid={_record_token(record.process)} "
            f"event={_record_token(getattr(record, 'event', '-'))} "
            f"message={message}"
        )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep each newly opened POSIX log file private on a best-effort basis."""

    def _open(self) -> TextIO:
        stream = super()._open()
        if os.name == "posix":
            try:
                os.fchmod(stream.fileno(), 0o600)
            except OSError:
                pass
        return stream


def _handler_key(path: str | Path, component: str) -> tuple[str, str]:
    resolved_path = Path(path).expanduser().resolve(strict=False)
    return str(resolved_path), _record_token(component)


def prepare_log_file(path: str | Path) -> Path:
    """Create a UTF-8 log destination and restrict it on POSIX.

    This is used when an OS supervisor or parent process owns stdout/stderr
    redirection instead of a Python file handler.
    """
    log_path = Path(path).expanduser().resolve(strict=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(mode=0o600, exist_ok=True)
    if os.name == "posix":
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
    return log_path


def attach_rotating_file_handler(
    logger: logging.Logger,
    *,
    path: str | Path,
    component: str,
    level: str | int | None = logging.INFO,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    """Attach one owned rotating handler without disturbing existing handlers.

    Repeated calls for the same logger, resolved path, and component return the
    existing handler.  The logger's own level and propagation policy remain
    entirely caller-controlled.
    """
    key = _handler_key(path, component)
    handler_level = parse_log_level(level)
    for existing in logger.handlers:
        if getattr(existing, _HANDLER_KEY_ATTRIBUTE, None) == key:
            existing.setLevel(handler_level)
            if isinstance(existing, RotatingFileHandler):
                return existing

    log_path = prepare_log_file(key[0])
    handler = _PrivateRotatingFileHandler(
        log_path,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    handler.setLevel(handler_level)
    handler.setFormatter(OmniLogFormatter(component=key[1]))
    setattr(handler, _HANDLER_KEY_ATTRIBUTE, key)
    logger.addHandler(handler)
    return handler


def remove_logging_handler(
    logger: logging.Logger,
    handler: logging.Handler,
) -> bool:
    """Detach and close exactly ``handler``, preserving every unrelated handler."""
    if handler not in logger.handlers:
        return False
    logger.removeHandler(handler)
    try:
        handler.flush()
    finally:
        handler.close()
    return True


class ProcessLogging:
    """One reversible component logging attachment.

    The process entry point owns this object and closes it at shutdown. Existing
    caller/pytest handlers are neither removed nor closed.
    """

    def __init__(
        self,
        *,
        component: str,
        level: str | int | None,
        path: str | Path | None = None,
        stream: TextIO | None = None,
        logger: logging.Logger | None = None,
        settings: object | None = None,
        max_bytes: int | None = None,
        files: int | None = None,
        backup_count: int | None = None,
    ) -> None:
        if (path is None) == (stream is None):
            raise ValueError("configure exactly one logging destination")
        self.logger = logger or logging.getLogger()
        self._previous_level = self.logger.level
        parsed_level = parse_log_level(level)
        size, resolved_files = rotation_limits(
            settings, max_bytes=max_bytes, files=files, backup_count=backup_count
        )
        backups = rolled_backup_count(resolved_files)
        existing_handlers = set(self.logger.handlers)
        if path is not None:
            self.handler = attach_rotating_file_handler(
                self.logger,
                path=path,
                component=component,
                level=parsed_level,
                max_bytes=size,
                backup_count=backups,
            )
        else:
            key = (_record_token(component), id(stream))
            self.handler = next(
                (
                    handler
                    for handler in self.logger.handlers
                    if getattr(handler, _STREAM_HANDLER_KEY_ATTRIBUTE, None) == key
                ),
                None,
            )
            if self.handler is None:
                self.handler = logging.StreamHandler(stream)
                self.handler.setFormatter(OmniLogFormatter(component=component))
                setattr(self.handler, _STREAM_HANDLER_KEY_ATTRIBUTE, key)
                self.logger.addHandler(self.handler)
            self.handler.setLevel(parsed_level)
        self._owns_handler = self.handler not in existing_handlers
        self._closed = False
        self._dependency_levels: dict[logging.Logger, int] = {}
        for name in ("httpx", "httpcore"):
            dependency = logging.getLogger(name)
            if dependency.level == logging.NOTSET or dependency.level < logging.WARNING:
                self._dependency_levels[dependency] = dependency.level
                dependency.setLevel(logging.WARNING)
        self._access_logger = logging.getLogger("uvicorn.access")
        self._access_logger_disabled = self._access_logger.disabled
        quiet_uvicorn_console()
        self._access_logger.disabled = True
        if self.logger.level == logging.NOTSET or self.logger.level > parsed_level:
            self.logger.setLevel(parsed_level)

    def close(self) -> None:
        """Restore logger level and remove only a handler created here."""
        if self._closed:
            return
        self._closed = True
        if self._owns_handler:
            remove_logging_handler(self.logger, self.handler)
        for dependency, previous_level in self._dependency_levels.items():
            dependency.setLevel(previous_level)
        self._dependency_levels.clear()
        self._access_logger.disabled = self._access_logger_disabled
        self.logger.setLevel(self._previous_level)

    def __enter__(self) -> ProcessLogging:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def configure_process_logging(
    *,
    component: str,
    level: str | int | None,
    path: str | Path | None = None,
    stream: TextIO | None = None,
    logger: logging.Logger | None = None,
    settings: object | None = None,
    max_bytes: int | None = None,
    files: int | None = None,
    backup_count: int | None = None,
) -> ProcessLogging:
    """Attach the shared schema to one process-owned file or stream."""
    return ProcessLogging(
        component=component,
        level=level,
        path=path,
        stream=stream,
        logger=logger,
        settings=settings,
        max_bytes=max_bytes,
        files=files,
        backup_count=backup_count,
    )


__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_FILES",
    "DEFAULT_LOG_MAX_BYTES",
    "OmniLogFormatter",
    "ProcessLogging",
    "UvicornShutdownFilter",
    "attach_rotating_file_handler",
    "configure_process_logging",
    "parse_log_level",
    "prepare_log_file",
    "quiet_uvicorn_console",
    "remove_logging_handler",
    "rolled_backup_count",
    "rotation_limits",
]
