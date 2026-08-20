"""Local-browser security boundary for the loopback Web RPC."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.responses import StreamingResponse

pytest.importorskip("starlette")

from omni.web.app import create_app  # noqa: E402

WEB_HEADERS = {"X-Omni-Web": "1"}


def _app():  # noqa: ANN202
    return create_app(cors_origins=["http://127.0.0.1:5173", "http://localhost:5173"])


@pytest.mark.asyncio
async def test_api_get_cannot_reach_rpc_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def _dispatch(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal called
        called = True
        raise AssertionError("GET must not enter RPC dispatch")

    monkeypatch.setattr("omni.web.app.dispatch", _dispatch)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
        headers=WEB_HEADERS,
    ) as client:
        response = await client.get("/api/config.set?key=model.model&value=unsafe")

    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"
    assert called is False


@pytest.mark.asyncio
async def test_api_requires_web_header_and_json_content_type() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
    ) as client:
        missing_header = await client.post(
            "/api",
            json={"method": "workspace.list", "params": {}},
        )
        wrong_header = await client.post(
            "/api",
            headers={"X-Omni-Web": "true"},
            json={"method": "workspace.list", "params": {}},
        )
        wrong_type = await client.post(
            "/api",
            headers=WEB_HEADERS,
            content='{"method":"workspace.list","params":{}}',
        )

    assert missing_header.status_code == 403
    assert wrong_header.status_code == 403
    assert wrong_type.status_code == 415
    assert missing_header.headers["cache-control"] == "no-store"
    assert wrong_type.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_api_rejects_untrusted_host_external_origin_and_cross_site_fetch() -> None:
    app = _app()

    async def _post(base_url: str, headers: dict[str, str]) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            return await client.post(
                "/api",
                headers={**WEB_HEADERS, **headers},
                json={"method": "workspace.list", "params": {}},
            )

    untrusted_host = await _post("http://attacker.invalid", {})
    external_origin = await _post(
        "http://127.0.0.1:1088",
        {"Origin": "https://attacker.invalid"},
    )
    null_origin = await _post("http://127.0.0.1:1088", {"Origin": "null"})
    cross_site = await _post(
        "http://127.0.0.1:1088",
        {
            "Origin": "http://localhost:5173",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert untrusted_host.status_code == 403
    assert external_origin.status_code == 403
    assert null_origin.status_code == 403
    assert cross_site.status_code == 403


@pytest.mark.asyncio
async def test_api_accepts_loopback_same_origin_and_vite_dev_origin() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
        headers=WEB_HEADERS,
    ) as client:
        same_origin = await client.post(
            "/api",
            headers={"Origin": "http://127.0.0.1:1088"},
            json={"method": "workspace.list", "params": {}},
        )
        vite_origin = await client.post(
            "/api",
            headers={
                "Origin": "http://localhost:5173",
                "Sec-Fetch-Site": "same-origin",
            },
            json={"method": "workspace.list", "params": {}},
        )

    assert same_origin.status_code == 200
    assert same_origin.json()["ok"] is True
    assert vite_origin.status_code == 200
    assert vite_origin.json()["ok"] is True
    assert same_origin.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["http://localhost:1088", "http://[::1]:1088"])
async def test_api_accepts_all_supported_loopback_host_forms(base_url: str) -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url=base_url,
        headers=WEB_HEADERS,
    ) as client:
        response = await client.post(
            "/api",
            json={"method": "workspace.list", "params": {}},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_custom_loopback_host_needs_explicit_trust() -> None:
    untrusted = create_app(cors_origins=[])
    trusted = create_app(cors_origins=[], trusted_hosts=["127.0.0.2"])

    async def _post(app: object) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.2:1088",
            headers={**WEB_HEADERS, "Origin": "http://127.0.0.2:1088"},
        ) as client:
            return await client.post(
                "/api",
                json={"method": "workspace.list", "params": {}},
            )

    forbidden = await _post(untrusted)
    allowed = await _post(trusted)

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True


@pytest.mark.asyncio
async def test_api_can_explicitly_allow_test_transport_hosts() -> None:
    app = create_app(cors_origins=[], trusted_hosts={"omni.test", "testserver"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://omni.test",
        headers=WEB_HEADERS,
    ) as client:
        response = await client.post(
            "/api/workspace.list",
            json={},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_vite_cors_preflight_does_not_require_actual_request_header() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
    ) as client:
        response = await client.options(
            "/api",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-omni-web",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-omni-web" in response.headers["access-control-allow-headers"].casefold()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_upload_uses_same_browser_boundary() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
    ) as client:
        missing_header = await client.post(
            "/api/attachment.upload",
            files={"file": ("paper.md", b"paper", "text/markdown")},
        )
        external_origin = await client.post(
            "/api/attachment.upload",
            headers={**WEB_HEADERS, "Origin": "https://attacker.invalid"},
            files={"file": ("paper.md", b"paper", "text/markdown")},
        )
        wrong_type = await client.post(
            "/api/attachment.upload",
            headers=WEB_HEADERS,
            json={"file": "paper"},
        )

    assert missing_header.status_code == 403
    assert external_origin.status_code == 403
    assert wrong_type.status_code == 415
    assert missing_header.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_sse_remains_post_json_and_is_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _events() -> AsyncIterator[bytes]:
        yield b"event: done\ndata: {}\n\n"

    async def _dispatch(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return StreamingResponse(_events(), media_type="text/event-stream")

    monkeypatch.setattr("omni.web.app.dispatch", _dispatch)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:1088",
        headers=WEB_HEADERS,
    ) as client:
        response = await client.post(
            "/api",
            json={"method": "task.watch", "params": {}},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert "event: done" in response.text
