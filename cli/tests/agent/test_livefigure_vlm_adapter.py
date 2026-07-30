"""Contracts for LiveFigure's portable OpenAI-compatible VLM adapter.

These tests deliberately exercise only an offline ``httpx.MockTransport`` and
temporary local files.  The VLM credential is owner-controlled input and must
never cross the generated-code sandbox boundary or appear in a skill result.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "livefigure"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

_VLM_ENV_KEYS = (
    "OMNI_VLM_MODEL",
    "OMNI_VLM_ENDPOINT",
    "OMNI_VLM_API_KEY",
)


def _vlm_module() -> Any:
    """Import the proposed portable adapter from the skill package."""
    return importlib.import_module("livefigure.vlm")


def _load_engine() -> Any:
    path = SKILL_DIR / "engine.py"
    module_name = "livefigure_vlm_contract_engine"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_runner() -> Any:
    path = SKILL_DIR / "scripts" / "run.py"
    module_name = "livefigure_vlm_contract_runner"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _set_vlm_env(monkeypatch: pytest.MonkeyPatch, *, api_key: str) -> None:
    monkeypatch.setenv("OMNI_VLM_MODEL", "vision-test-model")
    monkeypatch.setenv("OMNI_VLM_ENDPOINT", "https://vlm.invalid/v1/chat/completions")
    monkeypatch.setenv("OMNI_VLM_API_KEY", api_key)


def _clear_vlm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _VLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _ForbiddenSettings:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"LiveFigure engine must not read ctx.settings.{name}")


class _FakeVlmHost:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.missing = () if available else ("model", "endpoint", "api_key")
        self.calls: list[tuple[str, str | None]] = []

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str:
        self.calls.append((prompt, reference_image_uri))
        return "generated-code"


def _context(
    tmp_path: Path,
    *,
    api_key: str,
    available: bool = True,
) -> SimpleNamespace:
    del api_key  # The injected host port never exposes owner credentials.
    return SimpleNamespace(
        settings=_ForbiddenSettings(),
        vlm=_FakeVlmHost(available=available),
        paths=SimpleNamespace(artifacts_dir=tmp_path / "artifacts"),
        artifacts=None,
        db=None,
        session_id="",
        task_id="",
        subtask_id="",
    )


async def _fake_pipeline_result(requirement: str, **kwargs: Any) -> SimpleNamespace:
    output_dir = Path(kwargs["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = output_dir / "livefigure.pptx"
    code_path = output_dir / "livefigure.py"
    input_path = output_dir / "input.txt"
    pptx_path.write_bytes(b"offline-test-pptx")
    code_path.write_text("# offline test\n", encoding="utf-8")
    input_path.write_text(requirement, encoding="utf-8")
    return SimpleNamespace(
        title=str(kwargs["title"]),
        pptx_path=pptx_path,
        code_path=code_path,
        input_path=input_path,
        reference_path=None,
        attempts=1,
    )


def _config(module: Any, *, api_key: str = "vlm-secret-value") -> Any:
    return module.VlmConfig(
        model="vision-test-model",
        endpoint="https://vlm.invalid/v1/chat/completions",
        api_key=api_key,
        timeout_s=5.0,
    )


def _response(code: str = "from pptx import Presentation") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": code}}]})


def test_vlm_config_reads_generic_omni_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_vlm_env(monkeypatch)
    _set_vlm_env(monkeypatch, api_key="generic-vlm-secret")

    config = _vlm_module().VlmConfig.from_env()

    assert config.model == "vision-test-model"
    assert config.endpoint == "https://vlm.invalid/v1/chat/completions"
    assert config.api_key == "generic-vlm-secret"


def test_vlm_recognizes_windows_drive_paths_as_local_references() -> None:
    module = _vlm_module()

    assert module._is_windows_drive_path(r"C:\Users\runner\reference.png")
    assert module._is_windows_drive_path("D:/figures/reference.png")
    assert not module._is_windows_drive_path("https://example.invalid/reference.png")


def test_vlm_file_uri_is_percent_decoded_exactly_once(tmp_path: Path) -> None:
    module = _vlm_module()
    image = tmp_path / "reference%20.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nlocal-reference")

    encoded = module.reference_as_data_url(image.as_uri(), allowed_files=(image,))

    assert encoded.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_vlm_client_uses_fixed_openai_multimodal_contract_without_reference() -> None:
    module = _vlm_module()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response("generated-code")

    client = module.VlmClient(
        _config(module),
        transport=httpx.MockTransport(handler),
    )
    result = await client.generate_text("Create an editable RAG diagram")

    assert result == "generated-code"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://vlm.invalid/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer vlm-secret-value"
    payload = json.loads(request.content)
    assert payload["model"] == "vision-test-model"
    assert payload["messages"][0]["role"] == "user"
    content = payload["messages"][0]["content"]
    assert any(
        item == {"type": "text", "text": "Create an editable RAG diagram"} for item in content
    )
    assert not any(item.get("type") == "image_url" for item in content)


@pytest.mark.asyncio
async def test_vlm_client_preserves_reference_data_url() -> None:
    module = _vlm_module()
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _response()

    reference = "data:image/png;base64,iVBORw0KGgo="
    client = module.VlmClient(_config(module), transport=httpx.MockTransport(handler))
    await client.generate_text("Use this visual reference", reference_image_uri=reference)

    content = payloads[0]["messages"][0]["content"]
    images = [item for item in content if item.get("type") == "image_url"]
    assert images == [{"type": "image_url", "image_url": {"url": reference}}]


@pytest.mark.asyncio
async def test_vlm_client_encodes_local_reference_without_fetching_it(tmp_path: Path) -> None:
    module = _vlm_module()
    payloads: list[dict[str, Any]] = []
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nlocal-reference")

    def handler(request: httpx.Request) -> httpx.Response:
        # The only network-shaped operation is the configured VLM POST.  A
        # client-side fetch of the local reference would be an extra request.
        payloads.append(json.loads(request.content))
        return _response()

    config = module.VlmConfig(
        model="vision-test-model",
        endpoint="https://vlm.invalid/v1/chat/completions",
        api_key="vlm-secret-value",
        timeout_s=5.0,
        reference_roots=(tmp_path,),
    )
    client = module.VlmClient(config, transport=httpx.MockTransport(handler))
    await client.generate_text("Use the local image", reference_image_uri=str(image))

    assert len(payloads) == 1
    content = payloads[0]["messages"][0]["content"]
    image_url = next(
        item["image_url"]["url"] for item in content if item.get("type") == "image_url"
    )
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    assert image_url == f"data:image/png;base64,{encoded}"
    assert not image_url.startswith("file:")


@pytest.mark.asyncio
async def test_vlm_client_rejects_local_reference_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    module = _vlm_module()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    config = module.VlmConfig(
        model="vision-test-model",
        endpoint="https://vlm.invalid/v1/chat/completions",
        api_key="vlm-secret-value",
        timeout_s=5.0,
        reference_roots=(allowed,),
    )
    client = module.VlmClient(config, transport=httpx.MockTransport(lambda _r: _response()))

    with pytest.raises(module.VlmError, match="allowed") as caught:
        await client.generate_text("Use this image", reference_image_uri=str(outside))

    assert caught.value.code == "reference_image_forbidden"


@pytest.mark.asyncio
async def test_vlm_client_rejects_non_image_bytes_with_image_extension(tmp_path: Path) -> None:
    module = _vlm_module()
    fake = tmp_path / "not-an-image.png"
    fake.write_text("private text that must not be uploaded", encoding="utf-8")
    config = module.VlmConfig(
        model="vision-test-model",
        endpoint="https://vlm.invalid/v1/chat/completions",
        api_key="vlm-secret-value",
        reference_roots=(tmp_path,),
    )
    client = module.VlmClient(config, transport=httpx.MockTransport(lambda _r: _response()))

    with pytest.raises(module.VlmError, match="image") as caught:
        await client.generate_text("Use this image", reference_image_uri=str(fake))

    assert caught.value.code == "reference_image_invalid"


@pytest.mark.asyncio
async def test_vlm_client_requires_https_except_for_loopback() -> None:
    module = _vlm_module()
    config = module.VlmConfig(
        model="vision-test-model",
        endpoint="http://vision.example/v1/chat/completions",
        api_key="vlm-secret-value",
    )
    client = module.VlmClient(config, transport=httpx.MockTransport(lambda _r: _response()))

    with pytest.raises(module.VlmError, match="HTTPS") as caught:
        await client.generate_text("Generate a diagram")

    assert caught.value.code == "vlm_endpoint_insecure"


def test_livefigure_pptx_contract_requires_one_slide_with_editable_shape(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from livefigure.pipeline import LiveFigureError, _validate_pptx

    blank_layout_index = 6
    valid = pptx.Presentation()
    slide = valid.slides.add_slide(valid.slide_layouts[blank_layout_index])
    slide.shapes.add_textbox(100, 100, 800, 300).text = "Editable"
    valid_path = tmp_path / "valid.pptx"
    valid.save(valid_path)
    _validate_pptx(valid_path)

    two_slides = pptx.Presentation()
    for _ in range(2):
        current = two_slides.slides.add_slide(two_slides.slide_layouts[blank_layout_index])
        current.shapes.add_textbox(100, 100, 800, 300).text = "Editable"
    two_path = tmp_path / "two-slides.pptx"
    two_slides.save(two_path)
    with pytest.raises(LiveFigureError, match="exactly one slide"):
        _validate_pptx(two_path)

    empty = pptx.Presentation()
    empty.slides.add_slide(empty.slide_layouts[blank_layout_index])
    empty_path = tmp_path / "empty.pptx"
    empty.save(empty_path)
    with pytest.raises(LiveFigureError, match="editable"):
        _validate_pptx(empty_path)


@pytest.mark.asyncio
async def test_vlm_http_error_redacts_api_key() -> None:
    module = _vlm_module()
    secret = "vlm-super-secret-123"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text=f'{{"error": "credential {secret} was rejected"}}',
        )

    client = module.VlmClient(
        _config(module, api_key=secret),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(module.VlmError) as caught:
        await client.generate_text("Generate a diagram")

    assert "401" in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_engine_reports_internal_invariant_when_vlm_port_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_vlm_env(monkeypatch)
    module = _load_engine()
    engine = module.LiveFigureEngine()
    engine.ctx = _context(tmp_path, api_key="")
    engine.ctx.vlm = SimpleNamespace(available=False)

    result = await engine.execute(input="Create an editable architecture diagram")

    assert result["status"] == "error"
    assert result["blocking"] is True
    info = result["error_info"]
    assert info == {
        "code": "vlm_host_service_missing",
        "category": "internal",
        "retryable": False,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "omni config vlm" not in serialized
    assert "setup_command" not in result
    assert "next_actions" not in result


@pytest.mark.asyncio
async def test_engine_never_requests_generated_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "vlm-default-secret"
    _set_vlm_env(monkeypatch, api_key=secret)
    module = _load_engine()
    captured: dict[str, Any] = {}

    async def fake_generate(requirement: str, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return await _fake_pipeline_result(requirement, **kwargs)

    monkeypatch.setattr(module, "generate_pptx", fake_generate)
    engine = module.LiveFigureEngine()
    engine.ctx = _context(tmp_path, api_key=secret)

    result = await engine.execute(input="Create an editable architecture diagram")

    assert result["status"] == "ok"
    assert captured["reference_image_uri"] is None
    assert "generate_reference" not in captured


@pytest.mark.asyncio
async def test_engine_consumes_only_the_injected_vlm_host_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_vlm_env(monkeypatch)
    module = _load_engine()
    ctx = _context(tmp_path, api_key="run-scoped-secret", available=False)
    config = module._pipeline_config(ctx)  # noqa: SLF001

    assert config is not None and config.vlm is not None
    assert not hasattr(ctx.vlm, "config")
    assert not hasattr(config.vlm, "api_key")
    assert await config.vlm.generate_text("prompt") == "generated-code"
    assert ctx.vlm.calls == [("prompt", None)]
    assert all(name not in __import__("os").environ for name in _VLM_ENV_KEYS)


def test_portable_runner_reads_only_generic_vlm_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_vlm_env(monkeypatch)
    monkeypatch.setenv("OMNI_LIVEFIGURE_GEMINI_BASE_URL", "https://legacy.invalid/v1beta")
    monkeypatch.setenv("OMNI_LIVEFIGURE_GEMINI_API_KEY", "legacy-secret")
    monkeypatch.setenv("OMNI_LIVEFIGURE_GEMINI_VISION_MODEL", "legacy-model")
    module = _load_runner()

    config, secret = module._pipeline_config({"adapter": "gemini"}, tmp_path)

    assert config is None
    assert secret == ""
    assert module._config_error()["error_info"] == {  # noqa: SLF001
        "code": "vlm_not_configured",
        "category": "configuration",
        "retryable": False,
    }


def test_livefigure_manifest_has_portable_runtime_contract_without_gemini() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    helixforge = frontmatter["metadata"]["helixforge"]
    properties = helixforge["input_schema"]["properties"]
    requirements = helixforge["runtime_requirements"]

    assert "generate_reference" not in properties
    assert "gemini" not in text.lower()
    assert requirements["python_modules"] == ["pptx"]
    assert "livefigure" in requirements["dependency_setup_command"]


@pytest.mark.asyncio
async def test_engine_forwards_optional_reference_image_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "vlm-reference-secret"
    reference = "data:image/png;base64,iVBORw0KGgo="
    _set_vlm_env(monkeypatch, api_key=secret)
    module = _load_engine()
    captured: dict[str, Any] = {}

    async def fake_generate(requirement: str, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return await _fake_pipeline_result(requirement, **kwargs)

    monkeypatch.setattr(module, "generate_pptx", fake_generate)
    engine = module.LiveFigureEngine()
    engine.ctx = _context(tmp_path, api_key=secret)

    result = await engine.execute(
        input="Create an editable architecture diagram",
        reference_image_uri=reference,
    )

    assert result["status"] == "ok"
    assert captured["reference_image_uri"] == reference


@pytest.mark.asyncio
async def test_engine_preserves_reference_image_format_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_engine()

    async def fake_generate(requirement: str, **kwargs: Any) -> SimpleNamespace:
        result = await _fake_pipeline_result(requirement, **kwargs)
        reference_path = Path(kwargs["output_dir"]) / "reference.jpg"
        reference_path.write_bytes(b"jpeg-reference")
        result.reference_path = reference_path
        return result

    monkeypatch.setattr(module, "generate_pptx", fake_generate)
    engine = module.LiveFigureEngine()
    engine.ctx = _context(tmp_path, api_key="vlm-reference-format-secret")

    result = await engine.execute(input="Create an editable architecture diagram")

    reference = next(item for item in result["artifacts"] if item["title"].endswith("Reference"))
    assert reference["format"] == "jpg"
    assert reference["mime"] == "image/jpeg"


@pytest.mark.asyncio
async def test_engine_never_returns_vlm_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "vlm-result-secret-987"
    _set_vlm_env(monkeypatch, api_key=secret)
    module = _load_engine()

    async def fail_with_provider_detail(*_args: Any, **_kwargs: Any) -> None:
        raise module.LiveFigureError(f"provider rejected Authorization: Bearer {secret}")

    monkeypatch.setattr(module, "generate_pptx", fail_with_provider_detail)
    engine = module.LiveFigureEngine()
    engine.ctx = _context(tmp_path, api_key=secret)

    result = await engine.execute(input="Create an editable architecture diagram")

    assert result["status"] == "error"
    assert secret not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_generated_code_process_does_not_inherit_vlm_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from livefigure.pipeline import _execute_code

    secret = "vlm-child-process-secret"
    monkeypatch.setenv("OMNI_VLM_API_KEY", secret)
    code_path = tmp_path / "livefigure.py"
    pptx_path = tmp_path / "livefigure.pptx"
    code_path.write_text(
        "# execution is replaced by the offline process double\n", encoding="utf-8"
    )
    captured_env: dict[str, str] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*_args: Any, **kwargs: Any) -> FakeProcess:
        captured_env.update(kwargs["env"])
        pptx_path.write_bytes(b"offline-test-pptx")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await _execute_code(code_path, tmp_path, pptx_path)

    assert "OMNI_VLM_API_KEY" not in captured_env
    assert secret not in captured_env.values()
