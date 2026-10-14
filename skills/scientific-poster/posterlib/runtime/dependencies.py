"""Feature-scoped runtime dependency detection for scientific-poster."""

from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import sys
from typing import Any

CAPABILITY_REQUIREMENTS = {
    "html-validation": ("tinycss2",),
    "pdf-reading": ("pymupdf",),
    "structured-equation-source": ("lxml",),
    "equation-rendering": ("latex2mathml", "pyparsing", "markdown-it-py", "mdit-py-plugins"),
    "browser-inspection": ("playwright", "chromium"),
    "pptx-export": ("python-pptx", "mathml2omml", "pymupdf"),
}
_MINIMUM_PYMUPDF_VERSION = (1, 28)
PYMUPDF_REQUIREMENT = "pymupdf>=" + ".".join(map(str, _MINIMUM_PYMUPDF_VERSION))


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
    if capability == "html-validation":
        return [_package_install_argv(python, "tinycss2>=1.4,<2")]
    if capability == "pdf-reading":
        return [_package_install_argv(python, PYMUPDF_REQUIREMENT)]
    if capability == "structured-equation-source":
        return [_package_install_argv(python, "lxml>=5,<7")]
    if capability == "equation-rendering":
        return [_package_install_argv(
            python, "latex2mathml>=3.81,<4", "pyparsing>=3.2,<4",
            "markdown-it-py>=4,<5", "mdit-py-plugins>=0.6,<0.7"
        )]
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
                PYMUPDF_REQUIREMENT,
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
    """Re-probe importable packages for every caller invocation."""

    # Importing the layout reader changes process-global MuPDF glyph settings.
    # Only the clean PDF worker imports it, after native figure bytes are frozen.
    available: dict[str, bool] = {
        "pymupdf4llm": importlib.util.find_spec("pymupdf4llm") is not None,
    }
    for dependency, module_name in (
        ("tinycss2", "tinycss2"),
        ("pymupdf", "pymupdf"),
        ("latex2mathml", "latex2mathml.converter"),
        ("pyparsing", "pyparsing"),
        ("markdown-it-py", "markdown_it"),
        ("mdit-py-plugins", "mdit_py_plugins"),
        ("lxml", "lxml.html"),
        ("playwright", "playwright.async_api"),
        ("python-pptx", "pptx"),
        ("mathml2omml", "mathml2omml"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - a broken optional import is unavailable
            available[dependency] = False
        else:
            available[dependency] = dependency != "pymupdf" or pymupdf_supported(module)
    return available


__all__ = [
    "CAPABILITY_REQUIREMENTS",
    "PYMUPDF_REQUIREMENT",
    "classify_chromium_failure",
    "install_argv",
    "missing_result",
    "probe_python_packages",
    "pymupdf_supported",
]
