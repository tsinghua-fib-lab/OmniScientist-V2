"""Prepare a pinned PDF fallback parser in Omni's private cache.

The fallback is deliberately installed with ``--target``.  It never mutates
the Python environment owned by uv, pipx, conda, or the operating system.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

PYPDF_VERSION = "6.14.2"
PYPDF_SPEC = f"pypdf=={PYPDF_VERSION}"
PYPDF_WHEEL_SHA256 = (
    "3f07891af76dc002657e04993ab9b4de81de29f9013b9761d0b7968bff12e946"
)
_READY_MARKER = ".omni-pypdf-ready"
_INSTALL_LOCK = threading.Lock()


class PdfRuntimeInstallError(RuntimeError):
    """The private fallback parser could not be prepared safely."""


def pypdf_runtime_dir(cache_dir: Path) -> Path:
    """Return the versioned, owner-private parser directory."""

    return (
        Path(cache_dir).expanduser().resolve()
        / "skill-runtimes"
        / "paper-review"
        / f"pypdf-{PYPDF_VERSION}"
    )


def _runtime_ready(runtime_dir: Path) -> bool:
    return (
        (runtime_dir / _READY_MARKER).is_file()
        and (runtime_dir / "pypdf" / "__init__.py").is_file()
    )


def activate_pypdf_runtime(cache_dir: Path) -> bool:
    """Put a verified cached runtime first on this process's import path."""

    runtime_dir = pypdf_runtime_dir(cache_dir)
    if not _runtime_ready(runtime_dir):
        return False
    value = str(runtime_dir)
    if value not in sys.path:
        sys.path.insert(0, value)
    importlib.invalidate_caches()
    return True


def _install_argv(target: Path, requirements: Path) -> list[str]:
    uv = shutil.which("uv") or shutil.which("uv.exe")
    if uv:
        return [
            uv,
            "pip",
            "install",
            "--target",
            str(target),
            "--python",
            sys.executable,
            "--only-binary",
            ":all:",
            "--no-deps",
            "--require-hashes",
            "--quiet",
            "--requirements",
            str(requirements),
        ]
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(target),
        "--only-binary=:all:",
        "--no-deps",
        "--require-hashes",
        "--quiet",
        "--disable-pip-version-check",
        "--requirements",
        str(requirements),
    ]


def _remove_staging(path: Path) -> None:
    """Remove only the uniquely-created staging directory."""

    if path.name.startswith(".pypdf-install-") and path.parent.name == "paper-review":
        shutil.rmtree(path, ignore_errors=True)


def ensure_pypdf_runtime(
    cache_dir: Path,
    *,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Install, verify, activate, and describe the cached pypdf runtime.

    Concurrent processes install into separate staging directories.  The final
    directory is published atomically, so a reader never imports a half-written
    package.
    """

    runtime_dir = pypdf_runtime_dir(cache_dir)
    with _INSTALL_LOCK:
        if activate_pypdf_runtime(cache_dir):
            return {
                "installed": False,
                "package": PYPDF_SPEC,
                "runtime_dir": str(runtime_dir),
            }

        parent = runtime_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".pypdf-install-", dir=parent)
        ).resolve()
        requirements = staging / "requirements.txt"
        requirements.write_text(
            f"{PYPDF_SPEC} --hash=sha256:{PYPDF_WHEEL_SHA256}\n",
            encoding="utf-8",
        )
        argv = _install_argv(staging, requirements)
        try:
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, float(timeout_seconds)),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise PdfRuntimeInstallError(
                    f"could not start the private pypdf installer ({type(exc).__name__})"
                ) from exc
            if completed.returncode != 0:
                raise PdfRuntimeInstallError(
                    "the private pypdf installer exited with code "
                    f"{completed.returncode}"
                )
            requirements.unlink(missing_ok=True)
            if not (staging / "pypdf" / "__init__.py").is_file():
                raise PdfRuntimeInstallError(
                    "the installer completed without an importable pypdf package"
                )
            (staging / _READY_MARKER).write_text(
                f"{PYPDF_SPEC}\nsha256:{PYPDF_WHEEL_SHA256}\n",
                encoding="utf-8",
            )

            # Another process may have won the same atomic publication race.
            if runtime_dir.exists():
                if _runtime_ready(runtime_dir):
                    _remove_staging(staging)
                else:
                    shutil.rmtree(runtime_dir)
                    os.replace(staging, runtime_dir)
            else:
                os.replace(staging, runtime_dir)
        except Exception:
            _remove_staging(staging)
            raise

        if not activate_pypdf_runtime(cache_dir):
            raise PdfRuntimeInstallError(
                "the verified pypdf runtime could not be activated"
            )
        return {
            "installed": True,
            "package": PYPDF_SPEC,
            "runtime_dir": str(runtime_dir),
        }


__all__ = [
    "PYPDF_SPEC",
    "PdfRuntimeInstallError",
    "activate_pypdf_runtime",
    "ensure_pypdf_runtime",
    "pypdf_runtime_dir",
]
