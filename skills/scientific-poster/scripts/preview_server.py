#!/usr/bin/env python3
"""Serve one loopback poster working copy with ephemeral selection tooling."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import math
import os
import re
import secrets
import sys
import tempfile
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core  # noqa: E402

_MAX_SELECTION_BYTES = 64 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_INSPECTOR_SCRIPT_PATH = (
    Path(__file__).resolve().parent / "browser" / "preview_inspector.js"
)
_INSPECTOR_PLACEHOLDERS = (
    "__POSTER_PREVIEW_TOKEN_JSON__",
    "__POSTER_SOURCE_HASH_JSON__",
    "__POSTER_PANEL_ID_JSON__",
    "__POSTER_HIGHLIGHT_ID_JSON__",
)
_STYLE_NAMES = {
    "align-items",
    "background-color",
    "border-radius",
    "color",
    "display",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "gap",
    "grid-template-columns",
    "grid-template-rows",
    "justify-content",
    "letter-spacing",
    "line-height",
    "opacity",
    "overflow",
    "position",
    "text-align",
    "transform",
}


class PosterPreviewServer(http.server.ThreadingHTTPServer):
    """One loopback live working copy and its latest selection state."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        *,
        root: Path,
        token: str,
    ) -> None:
        self.root = root
        self.poster_path = root / "poster.html"
        self.state_path = root / "selection-state.json"
        self.token = token
        super().__init__(server_address, handler_class)
        self.allowed_host = f"127.0.0.1:{self.server_address[1]}"
        self.url = f"http://{self.allowed_host}"


class PreviewHandler(http.server.BaseHTTPRequestHandler):
    """Serve only poster.html, state hash, and authenticated selections."""

    server: PosterPreviewServer

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def end_headers(self) -> None:
        self.send_header(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
        )
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self) -> None:
        self._route_get()

    def do_POST(self) -> None:
        parsed = self._request_target()
        if parsed is None:
            return
        if _decoded_path(parsed.path) != "/__poster_selection":
            self._json_error(404, "not_found", "Endpoint not found.")
            return
        if not self._valid_inspector_token(parsed.query):
            return
        if self.headers.get("Origin", "").strip() != self.server.url:
            self._json_error(
                403, "invalid_origin", "Origin does not match this preview."
            )
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json_error(
                400, "invalid_length", "Content-Length must be an integer."
            )
            return
        if not 0 <= length <= _MAX_SELECTION_BYTES:
            status = 413 if length > _MAX_SELECTION_BYTES else 400
            self._json_error(
                status, "invalid_length", "Selection payload size is invalid."
            )
            return
        if (
            not self.headers.get("Content-Type", "")
            .lower()
            .startswith("application/json")
        ):
            self._json_error(
                400, "invalid_content_type", "Selection payload must be JSON."
            )
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            claimed_hash = _selection_source_hash(payload)
            current_hash = _file_sha256(self.server.poster_path)
            if claimed_hash != current_hash:
                self._json_error(
                    409, "stale_selection", "Poster changed after selection."
                )
                return
            state = _normalize_selection(payload)
            source_text = self.server.poster_path.read_text(encoding="utf-8")
            _validate_selection_dom(state, source_text)
            if _file_sha256(self.server.poster_path) != current_hash:
                self._json_error(
                    409, "stale_selection", "Poster changed after selection."
                )
                return
            _write_json_atomic(state, self.server.state_path)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self._json_error(400, "invalid_selection", str(exc))
            return
        except OSError as exc:
            self._json_error(500, "state_write_failed", str(exc))
            return
        self._send_json(200, {"status": "ok", "source_html_sha256": current_hash})

    def _route_get(self) -> None:
        parsed = self._request_target()
        if parsed is None:
            return
        decoded = _decoded_path(parsed.path)
        if decoded == "/poster.html":
            self._serve_poster(parsed.query)
            return
        if decoded == "/__poster_state":
            if not self._valid_inspector_token(parsed.query):
                return
            try:
                source_hash = _file_sha256(self.server.poster_path)
            except OSError as exc:
                self._json_error(500, "file_read_failed", str(exc))
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "source_html_sha256": source_hash,
                },
            )
            return
        self._json_error(404, "not_found", "Preview route not found.")

    def _serve_poster(self, query_string: str) -> None:
        if not self._valid_inspector_token(query_string):
            return
        try:
            source = self.server.poster_path.read_bytes()
        except OSError as exc:
            self._json_error(404, "poster_not_found", str(exc))
            return
        try:
            source_text = source.decode("utf-8")
        except UnicodeDecodeError:
            self._json_error(400, "invalid_encoding", "Poster HTML must be UTF-8.")
            return
        nonce = secrets.token_urlsafe(18)
        body = _inject_inspector(
            source_text,
            token=self.server.token,
            nonce=nonce,
            source_hash=hashlib.sha256(source).hexdigest(),
        ).encode("utf-8")
        csp = (
            "default-src 'none'; img-src data: blob:; font-src data:; "
            f"style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; sandbox allow-same-origin allow-scripts"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def _valid_inspector_token(self, query: str) -> bool:
        supplied = urllib.parse.parse_qs(query, keep_blank_values=True).get(
            "token", [""]
        )
        valid = len(supplied) == 1 and secrets.compare_digest(
            supplied[0], self.server.token
        )
        if not valid:
            self._json_error(403, "forbidden", "A valid inspector token is required.")
        return valid

    def _request_target(self) -> urllib.parse.SplitResult | None:
        if self.headers.get("Host", "").strip().lower() != self.server.allowed_host:
            self._json_error(421, "invalid_host", "Host does not match this preview.")
            return None
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self._json_error(
                400, "invalid_target", "Absolute request targets are forbidden."
            )
            return None
        return parsed

    def _json_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"status": "error", "code": code, "error": message})

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_preview_server(
    root: str | Path,
    *,
    token: str | None = None,
    port: int = 0,
) -> PosterPreviewServer:
    """Create a fixed-route IPv4 loopback server without starting it."""

    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"root is not a directory: {root_path}")
    if not 0 <= int(port) <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    session_token = str(token) if token is not None else secrets.token_urlsafe(24)
    if not _TOKEN_RE.fullmatch(session_token):
        raise ValueError("inspect token must be 20-200 URL-safe characters")
    return PosterPreviewServer(
        ("127.0.0.1", int(port)),
        PreviewHandler,
        root=root_path,
        token=session_token,
    )


def _decoded_path(value: str) -> str:
    try:
        return urllib.parse.unquote(value, errors="strict")
    except UnicodeDecodeError:
        return "\x00"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selection_source_hash(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Selection payload must be a JSON object.")
    value = str(payload.get("source_html_sha256") or "")
    if _HASH_RE.fullmatch(value) is None:
        raise ValueError("source_html_sha256 must be a lowercase SHA-256 digest.")
    return value


def _normalize_selection(payload: Any) -> dict[str, Any]:
    source_hash = _selection_source_hash(payload)
    poster_id = _bounded_text(payload.get("poster_id"), 200)
    if poster_core.POSTER_ID_RE.fullmatch(poster_id) is None:
        raise ValueError("poster_id is required and must be stable.")
    poster_module = _bounded_text(payload.get("poster_module"), 200)
    if poster_module and poster_core.POSTER_MODULE_RE.fullmatch(poster_module) is None:
        raise ValueError("poster_module is invalid.")
    semantic_roles = _bounded_text(payload.get("semantic_roles"), 200)
    role_tokens = semantic_roles.split()
    if len(role_tokens) != len(set(role_tokens)) or any(
        role not in poster_core.SEMANTIC_ROLES for role in role_tokens
    ):
        raise ValueError("semantic_roles is invalid.")
    module_priority = _bounded_text(payload.get("module_priority"), 32)
    if module_priority and module_priority not in poster_core.MODULE_PRIORITIES:
        raise ValueError("module_priority is invalid.")
    rect_raw = payload.get("rect") or {}
    if not isinstance(rect_raw, dict):
        raise ValueError("rect must be an object.")
    rect: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        if name not in rect_raw:
            continue
        value = float(rect_raw[name])
        if not math.isfinite(value):
            raise ValueError("rect values must be finite.")
        rect[name] = value
    styles_raw = payload.get("styles") or {}
    if not isinstance(styles_raw, dict):
        raise ValueError("styles must be an object.")
    styles = {
        str(name): _bounded_text(value, 300)
        for name, value in styles_raw.items()
        if str(name) in _STYLE_NAMES
    }
    state: dict[str, Any] = {
        "source_html_sha256": source_hash,
        "poster_id": poster_id,
        "poster_module": poster_module,
        "semantic_roles": " ".join(role_tokens),
        "module_priority": module_priority,
        "text_sample": _bounded_text(payload.get("text_sample"), 500),
        "rect": rect,
        "styles": styles,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    return state


def _validate_selection_dom(state: dict[str, Any], html_text: str) -> None:
    elements = poster_core.poster_identity_map(html_text)
    poster_id = str(state["poster_id"])
    if poster_id not in elements:
        raise ValueError("poster_id does not exist in the current poster HTML.")
    identity = elements[poster_id]
    for name in ("poster_module", "semantic_roles", "module_priority"):
        if str(state.get(name) or "") != identity.get(name, ""):
            raise ValueError(f"{name} does not match the selected DOM element.")


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if any(ord(character) < 32 for character in text):
        raise ValueError("Selection text contains control characters.")
    return text[:limit]


def _write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _inject_inspector(
    html_text: str,
    *,
    token: str,
    nonce: str,
    source_hash: str,
) -> str:
    nonce_attribute = html.escape(nonce, quote=True)
    inspector_prefix = "__sp_" + hashlib.sha256(nonce.encode("ascii")).hexdigest()[:16]
    panel_id = inspector_prefix + "_panel"
    highlight_id = inspector_prefix + "_highlight"
    inspector_script = _render_inspector_script(
        token=token,
        source_hash=source_hash,
        panel_id=panel_id,
        highlight_id=highlight_id,
    )
    injection = f"""
<style>
#{panel_id}{{position:fixed;z-index:2147483647;top:12px;right:12px;
max-width:420px;padding:10px 12px;border:1px solid #78dce8;background:#08101ded;
color:#f7fbff;font:13px/1.4 ui-monospace,monospace}}
#{highlight_id}{{position:fixed;z-index:2147483646;pointer-events:none;
border:2px solid #ffcc66;background:#ffcc661f;box-sizing:border-box}}
#{panel_id} button{{margin-left:8px;padding:3px 7px}}
</style>
<div id="{highlight_id}" hidden></div>
<div id="{panel_id}"><span>Click a poster element</span>
<button type="button">Copy ID</button></div>
<script nonce="{nonce_attribute}">
{inspector_script}
</script>
"""
    matches = list(re.finditer(r"</body\s*>", html_text, flags=re.IGNORECASE))
    if not matches:
        return f"{html_text}{injection}"
    match = matches[-1]
    return f"{html_text[: match.start()]}{injection}{html_text[match.start() :]}"


def _render_inspector_script(
    *,
    token: str,
    source_hash: str,
    panel_id: str,
    highlight_id: str,
) -> str:
    """Bind request-specific JSON literals into the static inspector script."""

    source = _INSPECTOR_SCRIPT_PATH.read_text(encoding="utf-8")
    replacements = {
        "__POSTER_PREVIEW_TOKEN_JSON__": _json_for_inline_script(token),
        "__POSTER_SOURCE_HASH_JSON__": _json_for_inline_script(source_hash),
        "__POSTER_PANEL_ID_JSON__": _json_for_inline_script(panel_id),
        "__POSTER_HIGHLIGHT_ID_JSON__": _json_for_inline_script(highlight_id),
    }
    for placeholder in _INSPECTOR_PLACEHOLDERS:
        if source.count(placeholder) != 1:
            raise RuntimeError(
                f"preview inspector must contain exactly one {placeholder} placeholder"
            )
        source = source.replace(placeholder, replacements[placeholder])
    return source


def _json_for_inline_script(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
        raise SystemExit(2)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Directory containing poster.html")
    parser.add_argument(
        "--port", type=int, default=0, help="Bind port; 0 chooses a free port"
    )
    parser.add_argument("--token", help="Optional inspector token; random by default")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        server = create_preview_server(
            root,
            token=args.token,
            port=args.port,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    preview_url = f"{server.url}/poster.html?" + urllib.parse.urlencode(
        {"token": server.token}
    )
    payload = {
        "status": "ok",
        "root": str(server.root),
        "url": preview_url,
        "state_path": str(server.state_path),
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
