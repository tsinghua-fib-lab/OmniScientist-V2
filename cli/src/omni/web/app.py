"""Starlette app: ``/api/*`` plus the built SPA (packaged or ``web/dist``)."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omni.web.protocol import (
    WEB_REQUEST_HEADER,
    WEB_REQUEST_HEADER_VALUE,
    RpcError,
    biz_error,
    is_json_content_type,
    transport_error,
)
from omni.web.rpc import dispatch
from omni.web.static import (
    MISSING_UI_BROWSER,
    is_spa,
    package_version,
    spa_version,
    web_dist_dir,
)
from omni.web.workspace import WorkspaceHub, close_workspace_hub

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def _host_port(value: str) -> tuple[str, int | None] | None:
    """Parse a Host/authority value without accepting userinfo or paths."""
    text = value.strip()
    if text == "::1":
        return text, None
    if not text or any(char in text for char in ("@", "/", "\\", ",", "?", "#")):
        return None
    try:
        parsed = urlsplit(f"//{text}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.username is not None or parsed.password is not None:
        return None
    return hostname.casefold(), port


def _origin_key(value: str) -> tuple[str, str, int] | None:
    """Normalize an HTTP Origin for exact comparisons."""
    text = value.strip()
    if not text or text.casefold() == "null":
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, hostname.casefold(), port or (443 if scheme == "https" else 80)


class LocalApiSecurityMiddleware:
    """Keep mutable Web RPC reachable only from the local Omni browser UI."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_hosts: Iterable[str],
        allowed_origins: Iterable[str],
    ) -> None:
        self.app = app
        normalized_hosts: set[str] = set()
        for value in trusted_hosts:
            parsed = _host_port(str(value))
            if parsed is None:
                raise ValueError(f"invalid trusted Web host: {value!r}")
            normalized_hosts.add(parsed[0])
        self.trusted_hosts = frozenset(normalized_hosts)
        self.allowed_origins = frozenset(
            key
            for value in allowed_origins
            if (key := _origin_key(str(value))) is not None
            and key[1] in _LOOPBACK_HOSTS
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        headers = Headers(scope=scope)
        host = _host_port(headers.get("host", ""))
        if host is None or host[0] not in self.trusted_hosts:
            await transport_error(403, "forbidden", "Web API host is not allowed")(
                scope, receive, send_no_store
            )
            return

        origin_value = headers.get("origin")
        if origin_value is not None:
            origin = _origin_key(origin_value)
            scheme = str(scope.get("scheme") or "http").casefold()
            request_origin = (
                scheme,
                host[0],
                host[1] or (443 if scheme == "https" else 80),
            )
            if origin is None or (
                origin != request_origin and origin not in self.allowed_origins
            ):
                await transport_error(403, "forbidden", "Web API origin is not allowed")(
                    scope, receive, send_no_store
                )
                return

        if headers.get("sec-fetch-site", "").strip().casefold() == "cross-site":
            await transport_error(403, "forbidden", "Cross-site Web API requests are not allowed")(
                scope, receive, send_no_store
            )
            return

        request_method = str(scope.get("method") or "").upper()
        if request_method not in {"POST", "OPTIONS"}:
            response = transport_error(405, "method_not_allowed", "Web RPC requires POST")
            response.headers["Allow"] = "POST"
            await response(scope, receive, send_no_store)
            return

        # Browser CORS preflight has no custom request header of its own. CORS
        # validates the requested header list before the actual guarded POST.
        if request_method != "OPTIONS":
            if headers.get(WEB_REQUEST_HEADER) != WEB_REQUEST_HEADER_VALUE:
                await transport_error(403, "forbidden", "Missing Omni Web request header")(
                    scope, receive, send_no_store
                )
                return
            content_type = headers.get("content-type")
            media_type = str(content_type or "").partition(";")[0].strip().casefold()
            if path == "/api/attachment.upload" and media_type != "multipart/form-data":
                await transport_error(
                    415,
                    "unsupported_media_type",
                    "Attachment upload requires multipart/form-data",
                )(scope, receive, send_no_store)
                return
            if path != "/api/attachment.upload" and not is_json_content_type(content_type):
                await transport_error(
                    415,
                    "unsupported_media_type",
                    "Web RPC requires application/json",
                )(scope, receive, send_no_store)
                return

        await self.app(scope, receive, send_no_store)


async def health(request: Request) -> JSONResponse:
    dist = request.app.state.web_dist
    return JSONResponse(
        {
            "ok": True,
            "surface": "web",
            "version": package_version(),
            "ui_version": spa_version(dist),
        }
    )


async def api_root(request: Request) -> Response:
    return await dispatch(request)


async def api_named(request: Request) -> Response:
    method = request.path_params.get("method") or ""
    return await dispatch(request, method)


async def upload(request: Request) -> Response:
    from omni.web.projectors import save_attachment
    from omni.web.protocol import ok

    hub: WorkspaceHub = request.app.state.hub
    form = await request.form()
    workspace = (
        str(form.get("workspace") or "")
        or request.headers.get("X-Omni-Workspace")
        or ""
    )
    try:
        rec = await hub.resolve(workspace or None, method="attachment.upload")
        hub.require_writable(rec, "attachment.upload")
    except RpcError as exc:
        return biz_error(exc.code, exc.message, **exc.extra)
    upload_file = form.get("file")
    if upload_file is None or not hasattr(upload_file, "read"):
        return biz_error("invalid_params", "multipart field 'file' is required")
    data = await upload_file.read()
    filename = getattr(upload_file, "filename", None) or "upload.bin"
    path = await save_attachment(rec, filename=str(filename), data=data)
    return ok(uri=path, path=path, name=str(filename), size=len(data))


async def spa_or_hint(request: Request) -> Response:
    dist = request.app.state.web_dist
    if dist is None or not is_spa(dist):
        return PlainTextResponse(MISSING_UI_BROWSER, status_code=503)
    return FileResponse(
        dist / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


class HashedAssets(StaticFiles):
    """Vite emits content-hashed filenames; browsers may cache them forever."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app(
    *,
    cors_origins: list[str] | None = None,
    trusted_hosts: Iterable[str] | None = None,
) -> Starlette:
    """Build the ASGI app. Tests pass a hub via ``app.state.hub`` afterwards."""
    hub = WorkspaceHub()
    dist = web_dist_dir()
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/api", api_root, methods=["POST"]),
        Route("/api/attachment.upload", upload, methods=["POST"]),
        Route("/api/{method:path}", api_named, methods=["POST"]),
    ]
    if dist is not None and (dist / "assets").is_dir():
        routes.append(Mount("/assets", HashedAssets(directory=dist / "assets"), name="assets"))
    routes.append(Route("/{path:path}", spa_or_hint, methods=["GET"]))

    @asynccontextmanager
    async def lifespan(_app: Starlette):  # noqa: ANN202
        try:
            yield
        finally:
            await close_workspace_hub(hub)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.hub = hub
    app.state.web_dist = dist
    try:
        from omni.web.home_guard import resolved_home

        app.state.web_home = resolved_home()
    except Exception:  # first successful RPC will bind the Home instead
        app.state.web_home = None
    origins = cors_origins if cors_origins is not None else list(_DEFAULT_DEV_ORIGINS)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", WEB_REQUEST_HEADER, "X-Omni-Workspace"],
        )
    app.add_middleware(
        LocalApiSecurityMiddleware,
        trusted_hosts=(*_LOOPBACK_HOSTS, *(trusted_hosts or ())),
        allowed_origins=origins,
    )
    return app
