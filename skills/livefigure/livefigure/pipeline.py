"""One-pass VLM-to-PPTX pipeline without image rendering or visual critique."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sys
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .icon_pipeline import IconAssetResult, prepare_icon_assets
from .prompts import build_generation_prompt, build_repair_prompt
from .vlm import VlmError, reference_as_data_url
from .vlm import reference_bytes as decode_reference_bytes

# ``(stage, pct)`` at minimum; a host that speaks the stage contract also takes
# the ``stage_id``/``milestone``/``stats`` keywords, so the shape is negotiated
# per call rather than fixed here.
Progress = Callable[..., Awaitable[None]]


class LiveFigureError(RuntimeError):
    """A generation error that can be reported as a structured skill result."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "livefigure_failed",
        category: str = "generation",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable


class VlmTextGenerator(Protocol):
    """Portable port implemented by Omni's host service or the env runner."""

    async def generate_text(
        self,
        prompt: str,
        *,
        reference_image_uri: str | None = None,
    ) -> str: ...

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1536x1024",
        aspect_ratio: str = "16:9",
        image_size: str = "",
    ) -> bytes: ...


@dataclass(frozen=True)
class PipelineConfig:
    vlm: VlmTextGenerator | None = None
    max_code_retries: int = 1
    reference_roots: tuple[Path, ...] = field(default_factory=tuple)
    reference_files: tuple[Path, ...] = field(default_factory=tuple)
    generate_reference: bool = True
    enable_icon_pipeline: bool = True
    # A trusted host may provide this narrow, one-run environment so generated
    # code can create and crop isolated icon assets through tools.py.
    image_environment: dict[str, str] = field(default_factory=dict, repr=False)
    # OS-level confinement argv prefix (e.g. seatbelt / bwrap) prepended to the
    # child that runs generated python-pptx source. Empty = run unwrapped; the
    # host adapter supplies it, keeping this core portable.
    sandbox_prefix: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PipelineResult:
    title: str
    pptx_path: Path
    code_path: Path
    input_path: Path
    reference_path: Path | None
    icon_assets: IconAssetResult
    attempts: int


async def generate_pptx(
    requirement: str,
    *,
    title: str,
    output_dir: Path,
    config: PipelineConfig,
    reference_image_uri: str | None = None,
    progress: Progress | None = None,
) -> PipelineResult:
    """Generate a PPTX with the established reference-to-code retry flow."""
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("tools.py"), output_dir / "tools.py")
    reference_path: Path | None = None
    icon_assets = IconAssetResult({})
    vlm_client = config.vlm
    if vlm_client is None:
        raise LiveFigureError(
            "VLM configuration is not available",
            code="vlm_not_configured",
            category="configuration",
            retryable=False,
        )
    try:
        reference_data_url: str | None = None
        await _progress(
            progress,
            "LiveFigure \u5df2\u542f\u52a8 · \u5b8c\u6574\u6d41\u7a0b\u901a\u5e38\u9700\u8981 5–10 \u5206\u949f",
            0.05,
            stage_id="livefigure.start",
            current="\u51c6\u5907\u53c2\u8003\u6784\u56fe",
        )
        if reference_image_uri:
            reference_data_url = reference_as_data_url(
                reference_image_uri,
                allowed_roots=config.reference_roots,
                allowed_files=config.reference_files,
            )
            raw, reference_mime = decode_reference_bytes(reference_data_url)
            suffix = _reference_suffix(reference_mime)
            reference_path = output_dir / f"reference{suffix}"
            reference_path.write_bytes(raw)
        elif config.generate_reference:
            await _progress(
                progress,
                "\u6b63\u5728\u751f\u6210\u6574\u4f53\u53c2\u8003\u56fe · \u89c6\u89c9\u6a21\u578b\u5728\u8ba4\u771f\u6784\u56fe\uff0c\u7ea6 1–2 \u5206\u949f",
                0.15,
                stage_id="livefigure.reference",
                current="\u6574\u4f53\u53c2\u8003\u56fe",
            )
            raw = await vlm_client.generate_image(_reference_prompt(requirement))
            reference_mime = _generated_image_mime(raw)
            suffix = _reference_suffix(reference_mime)
            reference_path = output_dir / f"reference{suffix}"
            reference_path.write_bytes(raw)
            reference_data_url = reference_as_data_url(
                str(reference_path),
                allowed_files=(reference_path,),
            )

        if config.enable_icon_pipeline and reference_data_url:
            await _progress(
                progress,
                "\u6b63\u5728\u8bc6\u522b\u5e76\u5236\u4f5c\u590d\u6742\u56fe\u6807 · \u8fd9\u4e00\u6b65\u6bd4\u8f83\u7ec6\uff0c\u7ea6 2–5 \u5206\u949f",
                0.22,
                stage_id="livefigure.icons",
                current="\u590d\u6742\u56fe\u6807\u7d20\u6750",
            )
            try:
                icon_assets = await prepare_icon_assets(
                    vlm_client,
                    reference_image_uri=reference_data_url,
                    output_dir=output_dir,
                )
            except VlmError:
                # Keep the established workflow's degradation behavior: icon
                # preparation enriches the diagram but does not discard an
                # otherwise usable reference image and code-generation pass.
                icon_assets = IconAssetResult({})
            icon_count = len(icon_assets.asset_map)
            await _progress(
                progress,
                "\u590d\u6742\u56fe\u6807\u7d20\u6750\u51c6\u5907\u5b8c\u6210"
                if icon_count
                else "\u672a\u53d1\u73b0\u9700\u8981\u5355\u72ec\u751f\u6210\u7684\u56fe\u6807\uff0c\u7ee7\u7eed\u4e3b\u4f53\u7ed3\u6784",
                0.32,
                stage_id="livefigure.icons.done",
                milestone="\u590d\u6742\u56fe\u6807\u7d20\u6750\u51c6\u5907\u5b8c\u6210"
                if icon_count
                else "\u56fe\u6807\u68c0\u67e5\u5b8c\u6210",
                stats={"icons": icon_count},
            )

        await _progress(
            progress,
            "\u6b63\u5728\u751f\u6210\u53ef\u7f16\u8f91 PPTX \u4ee3\u7801 · \u518d\u7ed9\u5b83 1–3 \u5206\u949f",
            0.40,
            stage_id="livefigure.generate",
            current="\u53ef\u7f16\u8f91 PPTX \u4ee3\u7801",
        )
        code = await vlm_client.generate_text(
            _code_prompt(requirement, title, icon_assets.asset_map),
            reference_image_uri=reference_data_url,
        )
    except VlmError as exc:
        raise _from_vlm_error(exc) from exc

    input_path = output_dir / "input.txt"
    input_path.write_text(requirement, encoding="utf-8")
    max_attempts = max(1, int(config.max_code_retries) + 1)
    last_error = LiveFigureError("PPTX generation failed")
    for attempt in range(1, max_attempts + 1):
        await _progress(
            progress,
            f"\u6b63\u5728\u6267\u884c\u5e76\u68c0\u67e5 PPTX · \u7b2c {attempt}/{max_attempts} \u8f6e",
            0.45 + (attempt / max_attempts) * 0.45,
            stage_id="livefigure.build",
            current=f"PPTX \u6784\u5efa\u4e0e\u6821\u9a8c\uff08{attempt}/{max_attempts}\uff09",
            stats={"attempt": attempt, "max_attempts": max_attempts},
        )
        code = _strip_fence(code)
        code_path = output_dir / "generated_figure.py"
        code_path.write_text(code, encoding="utf-8")
        pptx_path = output_dir / "livefigure.pptx"
        try:
            _validate_code(code)
            await _execute_code(
                code_path,
                output_dir,
                pptx_path,
                image_environment=config.image_environment,
                sandbox_prefix=config.sandbox_prefix,
            )
            _validate_pptx(pptx_path)
            await _progress(
                progress,
                "LiveFigure \u5df2\u751f\u6210",
                1.0,
                stage_id="livefigure.done",
                milestone="LiveFigure \u5df2\u751f\u6210\uff0c\u53ef\u7f16\u8f91 PPTX \u51c6\u5907\u597d\u4e86",
                stats={"attempts": attempt},
            )
            return PipelineResult(
                title,
                pptx_path,
                code_path,
                input_path,
                reference_path,
                icon_assets,
                attempt,
            )
        except LiveFigureError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            try:
                await _progress(
                    progress,
                    "\u9996\u8f6e\u6784\u5efa\u9047\u5230\u4e00\u70b9\u5c0f\u95ee\u9898 · \u6b63\u5728\u81ea\u52a8\u4fee\u590d\uff0c\u7ea6 1–2 \u5206\u949f",
                    0.70,
                    stage_id="livefigure.repair",
                    current="\u81ea\u52a8\u4fee\u590d\u751f\u6210\u4ee3\u7801",
                    stats={"next_attempt": attempt + 1, "max_attempts": max_attempts},
                )
                repair_prompt = _repair_prompt(requirement, title, code, str(last_error))
                code = await vlm_client.generate_text(repair_prompt)
            except VlmError as repair_exc:
                last_error = _from_vlm_error(repair_exc)
                break
    raise LiveFigureError(
        f"PPTX generation failed: {last_error}",
        code=last_error.code,
        category=last_error.category,
        retryable=last_error.retryable,
    )


def _from_vlm_error(exc: VlmError) -> LiveFigureError:
    return LiveFigureError(
        str(exc),
        code=exc.code,
        category=exc.category,
        retryable=exc.retryable,
    )


def _reference_suffix(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime.lower(), ".png")


def _generated_image_mime(raw: bytes) -> str:
    """Identify provider image bytes without trusting a requested format."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    raise VlmError(
        "Generated reference is not a supported PNG, JPEG, GIF, or WebP image",
        code="vlm_invalid_response",
        category="generation",
        retryable=True,
    )


def _code_prompt(requirement: str, title: str, asset_map: dict[str, Path] | None = None) -> str:
    return build_generation_prompt(requirement, title, asset_map=asset_map or {})


def _reference_prompt(requirement: str) -> str:
    """Retain the legacy reference-composition instruction verbatim."""
    return (
        f"A rigorous, publication-quality scientific diagram illustrating: {requirement}. "
        "Style guide: Emulate the aesthetic standard of high-impact journals like Nature or Science. "
        "Constraint: No main title, no placeholders. Pure white background."
    )


def _repair_prompt(requirement: str, title: str, code: str, error: str) -> str:
    return build_repair_prompt(requirement, title, code, error)


def _strip_fence(code: str) -> str:
    text = code.strip()
    fenced = re.search(r"```(?:python)?\s*\n?(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    return text.strip() + "\n"


def _validate_code(code: str) -> None:
    if not code:
        raise LiveFigureError("Generated PPTX code is empty")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise LiveFigureError(f"Generated PPTX code has invalid syntax: {exc.msg}") from exc


def _isolated_script_command(code_path: Path, output_dir: Path) -> list[str]:
    """Run generated code isolated, but keep the sibling helper importable."""
    boot_path = output_dir / "_omni_livefigure_boot.py"
    boot_path.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(output_dir.resolve())!r})\n"
        f"runpy.run_path({str(code_path.resolve())!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return [sys.executable, "-I", str(boot_path)]


async def _execute_code(
    code_path: Path,
    output_dir: Path,
    pptx_path: Path,
    *,
    image_environment: dict[str, str] | None = None,
    sandbox_prefix: tuple[str, ...] = (),
) -> None:
    if pptx_path.exists():
        pptx_path.unlink()
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"}
    for name, value in (image_environment or {}).items():
        if name.startswith(("LIVEFIGURE_IMAGE_", "LIVEFIGURE_GEMINI_")) and value:
            env[name] = value
    # Isolated interpreter (``-I``): ignore env vars / user site / cwd on
    # sys.path. That also hides the trusted ``tools.py`` copied beside the
    # script, so a host-owned boot fragment reinserts only ``output_dir``.
    # When the host supplies a ``sandbox_prefix`` (seatbelt / bwrap), the
    # child additionally runs under kernel write-confinement.
    argv = [*sandbox_prefix, *_isolated_script_command(code_path, output_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(output_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=75)
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise LiveFigureError("PPTX code execution timed out after 75 seconds") from None
    except OSError as exc:
        raise LiveFigureError(f"Could not execute PPTX code: {exc}") from exc
    if proc.returncode != 0:
        raise LiveFigureError(
            stderr.decode("utf-8", "replace")[-3000:] or "PPTX code execution failed"
        )
    if not pptx_path.is_file():
        raise LiveFigureError("Generated code did not create the required livefigure.pptx")


def _validate_pptx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if (
                "[Content_Types].xml" not in archive.namelist()
                or "ppt/presentation.xml" not in archive.namelist()
            ):
                raise LiveFigureError("Generated file is not a valid PPTX")
    except zipfile.BadZipFile as exc:
        raise LiveFigureError("Generated file is not a valid PPTX") from exc
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        presentation = Presentation(str(path))
    except Exception as exc:
        raise LiveFigureError("Generated file could not be parsed as a PPTX") from exc
    if len(presentation.slides) != 1:
        raise LiveFigureError("LiveFigure output must contain exactly one slide")
    shapes = list(presentation.slides[0].shapes)
    if not shapes or not any(shape.shape_type != MSO_SHAPE_TYPE.PICTURE for shape in shapes):
        raise LiveFigureError(
            "LiveFigure output must contain at least one editable PowerPoint shape"
        )


async def _progress(callback: Progress | None, stage: str, pct: float, **data: Any) -> None:
    if callback is not None:
        try:
            await callback(stage, pct, **data)
        except TypeError:
            await callback(stage, pct)
