from __future__ import annotations

import asyncio
import io
import logging
import os
from types import SimpleNamespace

from omni.runtime.logging_config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILES,
    DEFAULT_LOG_MAX_BYTES,
    OmniLogFormatter,
    UvicornShutdownFilter,
    attach_rotating_file_handler,
    configure_process_logging,
    parse_log_level,
    remove_logging_handler,
    rolled_backup_count,
    rotation_limits,
)


def test_rotation_defaults_are_ten_megabytes_and_ten_files() -> None:
    assert DEFAULT_LOG_MAX_BYTES == 10 * 1024 * 1024
    assert DEFAULT_LOG_FILES == 10
    assert DEFAULT_LOG_BACKUP_COUNT == 9
    assert rolled_backup_count(10) == 9
    assert rolled_backup_count(1) == 0
    assert rotation_limits() == (10 * 1024 * 1024, 10)
    settings = SimpleNamespace(
        observability=SimpleNamespace(log_max_bytes=2_000_000, log_files=4)
    )
    assert rotation_limits(settings) == (2_000_000, 4)
    assert rotation_limits(settings, max_bytes=99, files=2) == (99, 2)
    alias = SimpleNamespace(
        observability=SimpleNamespace(log_max_bytes=2_000_000, log_backup_count=4)
    )
    assert rotation_limits(alias) == (2_000_000, 4)


def test_uvicorn_shutdown_filter_drops_cancel_noise() -> None:
    filt = UvicornShutdownFilter()
    cancel = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Cancel 1 running task(s), timeout graceful shutdown exceeded",
        args=(),
        exc_info=None,
    )
    asgi = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Exception in ASGI application\n",
        args=(),
        exc_info=(asyncio.CancelledError, asyncio.CancelledError("omni web stopping"), None),
    )
    real = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="bind failed",
        args=(),
        exc_info=None,
    )
    assert filt.filter(cancel) is False
    assert filt.filter(asgi) is False
    assert filt.filter(real) is True


def test_parse_log_level_accepts_only_standard_names_and_numbers() -> None:
    assert parse_log_level("debug") == logging.DEBUG
    assert parse_log_level(" WARNING ") == logging.WARNING
    assert parse_log_level("fatal") == logging.CRITICAL
    assert parse_log_level(logging.ERROR) == logging.ERROR

    assert parse_log_level("__dict__", default=logging.WARNING) == logging.WARNING
    assert parse_log_level(17, default=logging.ERROR) == logging.ERROR
    assert parse_log_level(None, default=logging.INFO) == logging.INFO


def test_formatter_emits_one_redacted_rfc3339_record() -> None:
    formatter = OmniLogFormatter(component="web")
    record = logging.LogRecord(
        name="omni.web.server",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="\x1b[31mfailed\x1b[0m\r\napi_key=sk-1234567890abcdef",
        args=(),
        exc_info=None,
    )
    record.created = 0.123
    record.process = 4321
    record.event = "web.shutdown"

    rendered = formatter.format(record)

    assert rendered == (
        "1970-01-01T00:00:00.123Z WARNING component=web "
        "logger=omni.web.server pid=4321 event=web.shutdown "
        'message="failed\\napi_key=[REDACTED]"'
    )
    assert "\x1b" not in rendered
    assert "\n" not in rendered


def test_rotating_handler_is_idempotent_private_and_preserves_callers(tmp_path) -> None:
    logger = logging.Logger("omni.test.logging-config", level=logging.INFO)
    caller_stream = io.StringIO()
    caller_handler = logging.StreamHandler(caller_stream)
    logger.addHandler(caller_handler)
    logger.propagate = False
    log_path = tmp_path / "logs" / "web.log"

    handler = attach_rotating_file_handler(
        logger,
        path=log_path,
        component="web",
        level="INFO",
        max_bytes=380,
        backup_count=1,
    )
    repeated = attach_rotating_file_handler(
        logger,
        path=log_path,
        component="web",
        level="INFO",
        max_bytes=380,
        backup_count=1,
    )

    assert repeated is handler
    assert logger.handlers == [caller_handler, handler]

    logger.info("first line with 中文")
    logger.warning("second line token=sk-1234567890abcdef")
    logger.error("third line forces rotation")
    handler.flush()

    assert log_path.is_file()
    assert log_path.with_suffix(".log.1").is_file()
    combined = "".join(
        path.read_text(encoding="utf-8") for path in (log_path.with_suffix(".log.1"), log_path)
    )
    assert "中文" in combined
    assert "sk-1234567890abcdef" not in combined
    assert "[REDACTED]" in combined
    if os.name == "posix":
        assert log_path.stat().st_mode & 0o777 == 0o600

    assert remove_logging_handler(logger, handler) is True
    assert remove_logging_handler(logger, handler) is False
    assert logger.handlers == [caller_handler]


def test_process_logging_restores_logger_and_supports_managed_stream() -> None:
    logger = logging.Logger("omni.test.process", level=logging.ERROR)
    caller_stream = io.StringIO()
    caller_handler = logging.StreamHandler(caller_stream)
    logger.addHandler(caller_handler)
    managed_stream = io.StringIO()
    httpx_logger = logging.getLogger("httpx")
    access_logger = logging.getLogger("uvicorn.access")
    original_httpx_level = httpx_logger.level
    original_access_disabled = access_logger.disabled
    httpx_logger.setLevel(logging.NOTSET)
    access_logger.disabled = False

    try:
        with configure_process_logging(
            component="serve",
            level="INFO",
            stream=managed_stream,
            logger=logger,
        ):
            assert logger.level == logging.INFO
            assert httpx_logger.level == logging.WARNING
            assert access_logger.disabled is True
            logger.info("service ready")

        assert httpx_logger.level == logging.NOTSET
        assert access_logger.disabled is False
    finally:
        httpx_logger.setLevel(original_httpx_level)
        access_logger.disabled = original_access_disabled

    assert logger.level == logging.ERROR
    assert logger.handlers == [caller_handler]
    assert "component=serve" in managed_stream.getvalue()
    assert "service ready" in managed_stream.getvalue()
    assert "service ready" in caller_stream.getvalue()
