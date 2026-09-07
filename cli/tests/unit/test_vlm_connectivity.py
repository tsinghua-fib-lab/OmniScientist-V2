"""Offline contracts for VLM endpoint validation and connectivity checks."""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

import httpx
import pytest

from omni.config.settings import LiveFigureGeminiCfg, VlmCfg
from omni.core.vlm import (
    VlmGateway,
    VlmServiceError,
    check_vlm_connectivity,
    check_vlm_images_connectivity,
    gemini_generate_content_url,
    images_generation_url,
    resolve_vlm_request_url,
    validate_vlm_endpoint,
)


def _path(request: httpx.Request) -> str:
    return urlsplit(str(request.url)).path


def _config(**overrides: object) -> VlmCfg:
    values: dict[str, object] = {
        "enabled": True,
        "model": "vision-test-model",
        "endpoint": "https://vision.example/v1/chat/completions",
        "api_key": "vlm-secret-value",
        "protocol": "openai_compatible_chat",
        "timeout_s": 5.0,
    }
    values.update(overrides)
    return VlmCfg(**values)


def test_disabled_vlm_does_not_pretend_configured_fields_are_missing() -> None:
    complete = VlmGateway(_config(enabled=False))
    assert complete.missing == ()
    assert complete.available is False
    assert complete.error_code == "vlm_not_configured"

    empty = VlmGateway(VlmCfg())
    assert empty.missing == ("model", "endpoint", "api_key")
    assert empty.available is False
    assert empty.error_code == "vlm_not_configured"


def test_endpoint_policy_requires_https_except_for_loopback() -> None:
    assert validate_vlm_endpoint("https://vision.example/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://localhost:11434/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://127.0.0.1:8080/v1/chat/completions") is None
    assert validate_vlm_endpoint("http://[::1]:8080/v1/chat/completions") is None

    with pytest.raises(ValueError, match="HTTPS"):
        validate_vlm_endpoint("http://vision.example/v1/chat/completions")
    with pytest.raises(ValueError, match="complete"):
        validate_vlm_endpoint("vision.example/v1/chat/completions")
    assert validate_vlm_endpoint("https://zgc.apihy.com") is None
    assert validate_vlm_endpoint("https://zgc.apihy.com/") is None
    assert validate_vlm_endpoint("https://zgc.apihy.com/v1") is None


def test_resolve_vlm_request_url_expands_base_urls_like_claude_code() -> None:
    assert (
        resolve_vlm_request_url("https://zgc.apihy.com")
        == "https://zgc.apihy.com/v1/chat/completions"
    )
    assert (
        resolve_vlm_request_url("https://zgc.apihy.com/")
        == "https://zgc.apihy.com/v1/chat/completions"
    )
    assert (
        resolve_vlm_request_url("https://zgc.apihy.com/v1")
        == "https://zgc.apihy.com/v1/chat/completions"
    )
    assert (
        resolve_vlm_request_url("https://vision.example/v1/chat/completions")
        == "https://vision.example/v1/chat/completions"
    )
    assert (
        resolve_vlm_request_url("https://vision.example/v1/chat/completions/")
        == "https://vision.example/v1/chat/completions"
    )


def test_images_generation_url_is_the_v1_sibling_of_chat() -> None:
    assert (
        images_generation_url("https://zgc.apihy.com")
        == "https://zgc.apihy.com/v1/images/generations"
    )
    assert (
        images_generation_url("https://zgc.apihy.com/")
        == "https://zgc.apihy.com/v1/images/generations"
    )
    assert (
        images_generation_url("https://zgc.apihy.com/v1")
        == "https://zgc.apihy.com/v1/images/generations"
    )
    assert (
        images_generation_url("https://vision.example/v1/chat/completions")
        == "https://vision.example/v1/images/generations"
    )
    assert (
        images_generation_url("https://vision.example/v1/images/generations")
        == "https://vision.example/v1/images/generations"
    )
    assert images_generation_url("https://zgc.apihy.com") != (
        "https://zgc.apihy.com/images/generations"
    )


def test_gemini_generate_content_url_is_derived_from_the_site_origin() -> None:
    assert gemini_generate_content_url(
        "https://zgc.apihy.com",
        "gemini-3-pro-image-preview",
    ) == "https://zgc.apihy.com/v1beta/models/gemini-3-pro-image-preview:generateContent"
    assert gemini_generate_content_url(
        "https://zgc.apihy.com/v1/chat/completions",
        "gemini-3-pro-image-preview",
    ) == "https://zgc.apihy.com/v1beta/models/gemini-3-pro-image-preview:generateContent"


@pytest.mark.asyncio
async def test_vlm_gateway_exposes_generation_without_raw_owner_config() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "code"}}]})

    service = VlmGateway(_config(), transport=httpx.MockTransport(handler))

    assert not hasattr(service, "config")
    assert await service.generate_text("make a figure") == "code"
    assert str(requests[0].url) == "https://vision.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer vlm-secret-value"
    assert "vlm-secret-value" not in repr(service)


@pytest.mark.asyncio
async def test_vlm_gateway_expands_a_site_origin_to_chat_completions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    service = VlmGateway(
        _config(endpoint="https://zgc.apihy.com"),
        transport=httpx.MockTransport(handler),
    )
    assert await service.generate_text("ping") == "ok"
    assert str(requests[0].url) == "https://zgc.apihy.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_vlm_gateway_generates_a_base64_reference_image() -> None:
    image = b"reference-png"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://vision.example/v1/images/generations"
        payload = json.loads(request.content)
        assert payload["size"] == "1536x1024"
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(_config(), transport=httpx.MockTransport(handler))

    assert await service.generate_image("make a reference") == image


@pytest.mark.asyncio
async def test_vlm_gateway_posts_origin_to_openai_images_v1() -> None:
    image = b"origin-reference-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(
        _config(endpoint="https://zgc.apihy.com"),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("make a reference") == image
    assert [str(item.url) for item in requests] == [
        "https://zgc.apihy.com/v1/images/generations"
    ]
    payload = json.loads(requests[0].content)
    assert payload["response_format"] == "b64_json"
    assert payload["size"] == "1536x1024"


@pytest.mark.asyncio
async def test_vlm_gateway_uses_gemini_generate_content_for_gemini_image_models() -> None:
    image = b"gemini-native-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert _path(request) == "/v1beta/models/gemini-3-pro-image-preview:generateContent"
        payload = json.loads(request.content)
        assert payload["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"
        assert payload["generationConfig"]["image"]["imageSize"] == "1K"
        assert request.headers["authorization"] == "Bearer vlm-secret-value"
        assert request.headers["x-goog-api-key"] == "vlm-secret-value"
        assert urlsplit(str(request.url)).query.startswith("key=")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"data": base64.b64encode(image).decode("ascii")}}
                            ]
                        }
                    }
                ]
            },
        )

    service = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("compose a figure") == image
    assert service.last_images_model == "gemini-3-pro-image-preview"
    assert [_path(item) for item in requests] == [
        "/v1beta/models/gemini-3-pro-image-preview:generateContent"
    ]
    environment = service.image_generation_environment()
    assert environment["LIVEFIGURE_GEMINI_IMAGE_URL"].endswith(
        "/v1beta/models/gemini-3-pro-image-preview:generateContent"
    )
    assert environment["LIVEFIGURE_IMAGE_MODEL"] == "gpt-image-2"


@pytest.mark.asyncio
async def test_vlm_gateway_falls_back_when_chat_model_is_not_an_images_model() -> None:
    image = b"openai-images-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "generateContent" in str(request.url):
            return httpx.Response(404, json={"error": {"message": "not mounted"}})
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2"
        assert "response_format" not in payload
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("compose a figure") == image
    assert service.last_images_model == "gpt-image-2"
    assert [_path(item) for item in requests] == [
        "/v1beta/models/gemini-3-pro-image-preview:generateContent",
        "/v1/images/generations",
    ]


@pytest.mark.asyncio
async def test_vlm_gateway_falls_back_when_chat_model_returns_http_503() -> None:
    image = b"openai-images-503-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "generateContent" in str(request.url):
            return httpx.Response(503, text="no available channel")
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2"
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("compose a figure") == image
    assert service.last_images_model == "gpt-image-2"
    assert [_path(item) for item in requests] == [
        "/v1beta/models/gemini-3-pro-image-preview:generateContent",
        "/v1/images/generations",
    ]


@pytest.mark.asyncio
async def test_vlm_gateway_retries_preferred_images_model_when_catalog_omits_it() -> None:
    image = b"catalog-omit-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "custom-draw"}]},
            )
        payload = json.loads(request.content)
        if payload["model"] == "custom-draw":
            return httpx.Response(503, text="no available channel")
        assert payload["model"] == "gpt-image-2"
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="custom-draw",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("compose a figure") == image
    assert service.last_images_model == "gpt-image-2"


@pytest.mark.asyncio
async def test_vlm_gateway_uses_pinned_image_model_without_discovery() -> None:
    image = b"pinned-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2"
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    service = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
            image_model="gpt-image-2",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert await service.generate_image("compose") == image
    assert [str(item.url) for item in requests] == [
        "https://zgc.apihy.com/v1/images/generations"
    ]


@pytest.mark.asyncio
async def test_vlm_gateway_explains_when_no_images_model_is_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gemini-3-pro-image-preview"}]})
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": "not supported model for image generation, only imagen models are supported"
                }
            },
        )

    service = VlmGateway(
        _config(model="gemini-3-pro-image-preview"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VlmServiceError, match="not an image generator") as caught:
        await service.generate_image("compose")
    assert caught.value.retryable is False
    assert "gpt-image-2" in caught.value.safe_message


@pytest.mark.asyncio
async def test_vlm_gateway_prefers_the_legacy_gemini_image_route_when_configured() -> None:
    image = b"legacy-reference-png"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://legacy.example/v1beta/models/image:generateContent"
        payload = json.loads(request.content)
        assert payload["generationConfig"]["responseModalities"] == ["IMAGE"]
        assert payload["generationConfig"]["imageConfig"] == {
            "aspectRatio": "1:1",
            "imageSize": "4K",
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"inlineData": {"data": base64.b64encode(image).decode("ascii")}}]}}
                ]
            },
        )

    service = VlmGateway(
        _config(),
        livefigure_gemini=LiveFigureGeminiCfg(
            enabled=True,
            base_url="https://legacy.example/v1beta/models/image:generateContent",
            api_key="legacy-secret",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert (
        await service.generate_image("make a reference", aspect_ratio="1:1", image_size="4K")
        == image
    )
    assert service.image_generation_environment() == {
        "LIVEFIGURE_GEMINI_IMAGE_URL": "https://legacy.example/v1beta/models/image:generateContent",
        "LIVEFIGURE_GEMINI_API_KEY": "legacy-secret",
    }


def test_vlm_gateway_derives_a_one_run_images_endpoint_for_livefigure() -> None:
    service = VlmGateway(_config())

    environment = service.image_generation_environment()

    assert environment["LIVEFIGURE_IMAGE_ENDPOINT"] == "https://vision.example/v1/images/generations"
    assert environment["LIVEFIGURE_IMAGE_MODEL"] == "vision-test-model"
    assert environment["LIVEFIGURE_IMAGE_API_KEY"] == "vlm-secret-value"
    assert environment["LIVEFIGURE_IMAGE_TIMEOUT_S"] == "5.0"

    origin = VlmGateway(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
            image_model="gpt-image-2",
        )
    )
    origin_env = origin.image_generation_environment()
    assert origin_env["LIVEFIGURE_IMAGE_ENDPOINT"] == "https://zgc.apihy.com/v1/images/generations"
    assert origin_env["LIVEFIGURE_IMAGE_MODEL"] == "gpt-image-2"


@pytest.mark.asyncio
async def test_connectivity_probe_uses_redacted_openai_compatible_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    ok, detail = await check_vlm_connectivity(
        _config(), transport=httpx.MockTransport(handler)
    )

    assert ok is True
    assert "verified" in detail.lower()
    assert "image-input" in detail.lower()
    assert "/v1/images/generations" in detail
    assert "vlm-secret-value" not in detail
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["authorization"] == "Bearer vlm-secret-value"
    payload = json.loads(request.content)
    assert payload["model"] == "vision-test-model"
    assert payload["messages"][0]["content"][0]["type"] == "text"


@pytest.mark.asyncio
async def test_connectivity_probe_omits_provider_body_and_secret_on_auth_failure() -> None:
    secret = "provider-echoed-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"credential {secret} rejected")

    ok, detail = await check_vlm_connectivity(
        _config(api_key=secret), transport=httpx.MockTransport(handler)
    )

    assert ok is False
    assert "401" in detail
    assert secret not in detail
    assert "credential" not in detail


@pytest.mark.asyncio
async def test_connectivity_probe_explains_rejected_image_input() -> None:
    provider_detail = "No endpoints support image input for this text-only model"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": provider_detail}})

    ok, detail = await check_vlm_connectivity(
        _config(model="deepseek/deepseek-chat"),
        transport=httpx.MockTransport(handler),
    )

    assert ok is False
    assert "HTTP 400" in detail
    assert "supports image input" in detail
    assert provider_detail not in detail


@pytest.mark.asyncio
async def test_connectivity_probe_rejects_incomplete_or_unsupported_configuration() -> None:
    ok, detail = await check_vlm_connectivity(_config(model=""))
    assert ok is False
    assert "model" in detail.lower()

    ok, detail = await check_vlm_connectivity(_config(protocol="unknown"))
    assert ok is False
    assert "protocol" in detail.lower()


@pytest.mark.asyncio
async def test_connectivity_probe_posts_origin_to_chat_completions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    ok, detail = await check_vlm_connectivity(
        _config(endpoint="https://zgc.apihy.com"),
        transport=httpx.MockTransport(handler),
    )

    assert ok is True
    assert "verified" in detail.lower()
    assert str(requests[0].url) == "https://zgc.apihy.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_connectivity_probe_explains_non_json_html_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    ok, detail = await check_vlm_connectivity(
        _config(), transport=httpx.MockTransport(handler)
    )

    assert ok is False
    assert "invalid JSON" in detail
    assert "<html>" not in detail


@pytest.mark.asyncio
async def test_images_connectivity_probe_uses_openai_images_path() -> None:
    image = b"probe-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
        )

    ok, detail = await check_vlm_images_connectivity(
        _config(endpoint="https://zgc.apihy.com"),
        transport=httpx.MockTransport(handler),
    )

    assert ok is True
    assert "images/generations" in detail
    assert "vision-test-model" in detail
    assert "vlm-secret-value" not in detail
    assert [str(item.url) for item in requests] == [
        "https://zgc.apihy.com/v1/images/generations"
    ]


@pytest.mark.asyncio
async def test_images_connectivity_probe_uses_gemini_generate_content() -> None:
    image = b"gemini-probe-png"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inline_data": {
                                        "data": base64.b64encode(image).decode("ascii")
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    ok, detail = await check_vlm_images_connectivity(
        _config(
            endpoint="https://zgc.apihy.com",
            model="gemini-3-pro-image-preview",
        ),
        transport=httpx.MockTransport(handler),
    )

    assert ok is True
    assert "generateContent" in detail
    assert "gemini-3-pro-image-preview" in detail
    assert "vlm-secret-value" not in detail
    assert [_path(item) for item in requests] == [
        "/v1beta/models/gemini-3-pro-image-preview:generateContent"
    ]
