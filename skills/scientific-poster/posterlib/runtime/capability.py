"""Feature-scoped optional dependency detection for scientific-poster."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
from typing import Any

CAPABILITY_REQUIREMENTS = {
    "pdf-reading": ("pymupdf",),
    "browser-inspection": ("playwright", "chromium"),
    "pptx-export": ("python-pptx", "mathml2omml", "pymupdf"),
}
_MINIMUM_PYMUPDF_VERSION = (1, 24)


def pymupdf_supported(module: Any) -> bool:
    """Return whether an imported PyMuPDF module meets the PDF contract."""

    match = re.match(r"^(\d+)\.(\d+)", str(getattr(module, "__version__", "")))
    return bool(
        match
        and tuple(int(value) for value in match.groups()) >= _MINIMUM_PYMUPDF_VERSION
    )


def classify_chromium_failure(error: BaseException | str) -> str:
    """Classify an absent browser binary separately from a launch/runtime failure."""

    detail = str(error).lower()
    missing_markers = (
        "executable doesn't exist",
        "executable does not exist",
        "executable not found",
        "please run the following command to download",
    )
    return "missing" if any(marker in detail for marker in missing_markers) else "error"


def install_argv(
    capability: str, *, python_executable: str | None = None
) -> list[list[str]]:
    """Return explicit installer argv without mutating the caller's environment."""

    python = python_executable or sys.executable
    if capability == "pdf-reading":
        return [_package_install_argv(python, "pymupdf>=1.24")]
    if capability == "browser-inspection":
        return [
            _package_install_argv(python, "playwright"),
            [python, "-m", "playwright", "install", "chromium"],
        ]
    if capability == "pptx-export":
        return [
            _package_install_argv(
                python,
                "python-pptx>=1.0",
                "mathml2omml==0.0.2",
                "pymupdf>=1.24",
            )
        ]
    raise ValueError(f"unknown capability: {capability}")


def _package_install_argv(python: str, *requirements: str) -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", python, *requirements]
    return [python, "-m", "pip", "install", *requirements]


def missing_result(
    capability: str,
    *,
    dependency: str,
    stage: str,
    error: BaseException | str,
) -> dict[str, Any]:
    """Describe one recoverable missing capability through a stable result shape."""

    return {
        "status": "partial",
        "outcome": {"code": "missing_capability"},
        "summary": f"The optional {capability} capability is not configured.",
        "blocking": True,
        "recoverable": True,
        "capability": capability,
        "missing": [dependency],
        "python": sys.executable,
        "install_argv": install_argv(capability),
        "error": str(error),
        "error_info": {"stage": stage, "exception": repr(error)},
    }


def probe_python_packages() -> dict[str, bool]:
    """Re-probe importable optional packages for every caller invocation."""

    available: dict[str, bool] = {}
    for dependency, module_name in (
        ("pymupdf", "pymupdf"),
        ("playwright", "playwright.async_api"),
        ("python-pptx", "pptx"),
        ("mathml2omml", "mathml2omml"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            available[dependency] = False
        else:
            available[dependency] = dependency != "pymupdf" or pymupdf_supported(module)
    return available


__all__ = [
    "CAPABILITY_REQUIREMENTS",
    "classify_chromium_failure",
    "install_argv",
    "missing_result",
    "pymupdf_supported",
    "probe_python_packages",
]
