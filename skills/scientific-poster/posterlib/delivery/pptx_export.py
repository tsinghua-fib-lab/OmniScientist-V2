"""Browser capture and native PowerPoint export orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from posterlib.runtime.capability import (
    CAPABILITY_REQUIREMENTS,
    install_argv,
    probe_python_packages,
)
from posterlib.paths import SKILL_ROOT
from .pptx_rubric import evaluate_scene
from .pptx_scene import SceneError, normalize_scene

SCRIPTS_DIR = SKILL_ROOT / "scripts"
_CNVPR_ID_RE = re.compile(rb'(<p:cNvPr\b[^>]*\bid=")\d+("[^>]*>)')


class ExportError(RuntimeError):
    """Editable PPTX export failed at a named, recoverable stage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def export_editable_pptx(source_html: Path, output_dir: Path) -> dict[str, Any]:
    """Capture one validated poster HTML file and write native PPTX artifacts."""

    source = source_html.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        scene = normalize_scene(_capture_scene(source))
    except SceneError as exc:
        raise ExportError(
            "pptx_export_failed", str(exc), details={"stage": "scene"}
        ) from exc

    scene_path = output / "poster-scene.json"
    rubric_path = output / "poster-pptx-rubric.json"
    pptx_path = output / "poster.pptx"
    scene_path.write_text(
        json.dumps(scene, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rubric = evaluate_scene(scene)
    rubric_path.write_text(
        json.dumps(rubric, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if rubric["status"] != "ok":
        raise ExportError(
            "pptx_export_failed",
            "Editable PPTX rubric has hard failures.",
            details={
                "stage": "rubric",
                "scene_path": str(scene_path),
                "rubric_path": str(rubric_path),
                "rubric": rubric,
            },
        )
    _render_scene(scene_path, pptx_path)
    _normalize_slide_object_ids(pptx_path)
    openxml = _verify_openxml(pptx_path, expected_objects=scene["objects"])
    return {
        "pptx_path": str(pptx_path),
        "scene_path": str(scene_path),
        "rubric_path": str(rubric_path),
        "rubric": rubric,
        "scene": scene,
        "openxml": openxml,
    }


def _capture_scene(source_html: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPTS_DIR / "capture_pptx_scene.py"),
        "--html",
        str(source_html),
    ]
    result = _run(command, timeout=90, stage="capture")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExportError(
            "pptx_export_failed",
            "DOM capture did not return JSON.",
            details={"stage": "capture", "stderr": result.stderr[-2000:]},
        ) from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        message = str(
            payload.get("error") if isinstance(payload, dict) else "capture failed"
        )
        raise ExportError(
            "pptx_export_failed",
            message,
            details={"stage": "capture", "capture": payload},
        )
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        raise ExportError(
            "pptx_export_failed",
            "DOM capture returned no scene object.",
            details={"stage": "capture"},
        )
    return scene


def _render_scene(scene_path: Path, output_path: Path) -> None:
    dependencies = list(CAPABILITY_REQUIREMENTS["pptx-export"])
    available = probe_python_packages()
    missing = [name for name in dependencies if not available.get(name, False)]
    if missing:
        raise ExportError(
            "missing_capability",
            "Editable PPTX export requires python-pptx, mathml2omml, and "
            "PyMuPDF>=1.24 in the active Python environment.",
            details={
                "stage": "render",
                "dependencies": dependencies,
                "missing": missing,
                "install_argv": install_argv("pptx-export"),
            },
        )
    _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "render_pptx_scene.py"),
            str(scene_path),
            str(output_path),
        ],
        timeout=90,
        stage="render",
    )
    if not output_path.is_file() or output_path.stat().st_size < 1000:
        raise ExportError(
            "pptx_export_failed",
            "python-pptx did not create a usable deck.",
            details={"stage": "render", "pptx_path": str(output_path)},
        )


def _verify_openxml(
    pptx_path: Path,
    *,
    expected_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(pptx_path) as archive:
            slide_names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            if len(slide_names) != 1:
                raise ExportError(
                    "pptx_export_failed",
                    f"Editable poster deck must contain one slide, found {len(slide_names)}.",
                    details={"stage": "openxml"},
                )
            root = ElementTree.fromstring(archive.read(slide_names[0]))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ExportError(
            "pptx_export_failed",
            f"Generated deck is not valid Open XML: {exc}",
            details={"stage": "openxml"},
        ) from exc
    namespace = {
        "a14": "http://schemas.microsoft.com/office/drawing/2010/main",
        "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
        "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }
    nodes = root.findall(".//p:cNvPr", namespace)
    names = {str(node.get("name")) for node in nodes if node.get("name")}
    object_ids = [str(node.get("id")) for node in nodes if node.get("id")]
    if len(object_ids) != len(set(object_ids)):
        raise ExportError(
            "pptx_export_failed",
            "Generated deck contains duplicate Open XML object IDs.",
            details={"stage": "openxml"},
        )
    expected = {item["id"] for item in expected_objects}
    missing = sorted(expected - names)
    if missing:
        raise ExportError(
            "pptx_export_failed",
            "Generated deck lost stable object names.",
            details={"stage": "openxml", "missing_object_ids": missing},
        )
    expected_equations = sum(
        item.get("kind") == "equation" for item in expected_objects
    )
    native_equations = root.findall(".//a14:m", namespace)
    equation_fallbacks = root.findall(".//mc:Fallback", namespace)
    office_math = root.findall(".//m:oMath", namespace)
    if (
        len(native_equations) < expected_equations
        or len(office_math) < expected_equations
        or len(equation_fallbacks) < expected_equations
    ):
        raise ExportError(
            "pptx_export_failed",
            "Generated deck lost native equations or their viewer fallbacks.",
            details={
                "stage": "openxml",
                "expected_equation_count": expected_equations,
                "native_equation_count": len(native_equations),
                "office_math_count": len(office_math),
                "equation_fallback_count": len(equation_fallbacks),
            },
        )
    return {
        "slide_count": 1,
        "named_object_count": len(names),
        "verified_object_count": len(expected),
        "unique_object_id_count": len(set(object_ids)),
        "native_equation_count": len(native_equations),
        "equation_fallback_count": len(equation_fallbacks),
    }


def _normalize_slide_object_ids(pptx_path: Path) -> None:
    """Assign unique numeric non-visual IDs within every slide XML part."""

    temporary = pptx_path.with_name(f".{pptx_path.name}.normalize.tmp")
    try:
        with (
            zipfile.ZipFile(pptx_path, "r") as source,
            zipfile.ZipFile(temporary, "w") as destination,
        ):
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename.startswith(
                    "ppt/slides/slide"
                ) and info.filename.endswith(".xml"):
                    next_id = 0

                    def replace_id(match: re.Match[bytes]) -> bytes:
                        nonlocal next_id
                        next_id += 1
                        return (
                            match.group(1)
                            + str(next_id).encode("ascii")
                            + match.group(2)
                        )

                    data = _CNVPR_ID_RE.sub(replace_id, data)
                destination.writestr(info, data)
        os.replace(temporary, pptx_path)
    except (OSError, zipfile.BadZipFile) as exc:
        temporary.unlink(missing_ok=True)
        raise ExportError(
            "pptx_export_failed",
            f"Could not normalize PowerPoint object IDs: {exc}",
            details={"stage": "openxml"},
        ) from exc


def _run(
    command: list[str], *, timeout: int, stage: str
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError(
            "pptx_export_failed",
            f"Editable PPTX {stage} failed: {exc}",
            details={"stage": stage},
        ) from exc
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or completed.stdout.strip() or "unknown failure"
        )
        raise ExportError(
            "pptx_export_failed",
            f"Editable PPTX {stage} failed: {message[-2000:]}",
            details={"stage": stage},
        )
    return completed
