"""HTTP vs business errors for the web RPC.

Carrier failures use HTTP status codes (bad JSON, unknown method). Business
failures stay on HTTP 200 with ``{ok: false, error}`` so the SPA can handle
them without treating every refusal as a transport crash.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

WEB_REQUEST_HEADER = "X-Omni-Web"
WEB_REQUEST_HEADER_VALUE = "1"


def is_json_content_type(value: str | None) -> bool:
    """Return whether *value* is the JSON media type accepted by Web RPC."""
    media_type = str(value or "").partition(";")[0].strip().casefold()
    return media_type == "application/json"


def transport_error(status_code: int, code: str, message: str) -> JSONResponse:
    """Build an HTTP-level RPC refusal without exposing request details."""
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


class RpcError(Exception):
    """A business-level refusal that serializes as ``{ok: false}``."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def utc_iso(value: Any) -> str | None:
    """Serialize datetimes as UTC (``Z``). Naive values are treated as UTC.

    SQLite often returns UTC walls without tzinfo; a naive ISO string is then
    parsed as local time in the browser and shows up hours off.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text = value.isoformat() if callable(getattr(value, "isoformat", None)) else str(value)
    if not text:
        return None
    if text.endswith("+00:00"):
        return f"{text[:-6]}Z"
    if text.endswith("Z") or text.endswith("z"):
        return f"{text[:-1]}Z"
    # Naive ISO-8601 (``2026-08-19T03:10:00``) — stamp UTC rather than local.
    if len(text) >= 19 and text[10] == "T" and "+" not in text[10:] and text[-6] != "-":
        return f"{text}Z"
    return text


def jsonable(value: Any) -> Any:
    """Best-effort JSON projection for ORM rows, paths, and event objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return jsonable(to_dict())
        except TypeError:
            pass
    return str(value)


def ok(**data: Any) -> JSONResponse:
    payload = {"ok": True, **{k: jsonable(v) for k, v in data.items()}}
    return JSONResponse(payload)


def biz_error(code: str, message: str, **extra: Any) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    error.update({k: jsonable(v) for k, v in extra.items()})
    return JSONResponse({"ok": False, "error": error})


async def read_json(request: Request) -> dict[str, Any]:
    # Defense in depth: routes and the local-browser guard enforce these at the
    # HTTP boundary. Keeping the parser POST/JSON-only prevents a future route
    # change from turning query parameters or form input into mutable RPC args.
    if request.method != "POST":
        raise RpcError("invalid_request", "Web RPC requires POST")
    if not is_json_content_type(request.headers.get("content-type")):
        raise RpcError("invalid_request", "Web RPC requires application/json")
    body = await request.body()
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RpcError("invalid_json", f"request body is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RpcError("invalid_json", "request body must be a JSON object")
    return data


def params_of(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("params")
    if isinstance(raw, dict):
        return dict(raw)
    return {k: v for k, v in body.items() if k not in {"method", "params"}}
