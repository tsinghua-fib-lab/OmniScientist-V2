"""Locate and, in a source checkout only, prepare the loopback SPA.

Published wheels ship the Vite output under ``omni/data/web`` (see
``cli/hatch_build.py``). Editable checkouts resolve ``<repo>/web/dist``.
Ordinary ``omni`` commands never run Node; ``omni web`` and ``omni doctor``
only *check* that the files are present. ``omni web`` may build once when
this is a git checkout and a frontend toolchain is already on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SPA_VERSION_NAME = "version.json"

MISSING_UI_HINT = (
    "omni web UI is not available.\n"
    "A packaged install should already include it (omni/data/web in the wheel).\n"
    "From a git checkout, build once:\n"
    "  ./cli/scripts/build_web_ui.sh\n"
    "  # Windows: powershell -ExecutionPolicy Bypass -File cli/scripts/build_web_ui.ps1\n"
    "Or set OMNI_WEB_DIST to a directory that contains index.html.\n"
    "Then run `omni web` again. `omni doctor` reports whether the UI is present.\n"
)

MISSING_UI_BROWSER = (
    "omni web UI is not available.\n"
    "A packaged install should already include it. From a git checkout run:\n"
    "  ./cli/scripts/build_web_ui.sh\n"
    "Then reload this page. The JSON API at /api is already running.\n"
)


class WebUiMissing(RuntimeError):
    """The SPA files are not on disk and were not prepared."""


def packaged_web_dir() -> Path:
    """Wheel layout: ``omni/data/web`` beside this package."""
    return Path(__file__).resolve().parent.parent / "data" / "web"


def is_spa(path: Path | None) -> bool:
    """True when ``path`` is a directory that contains ``index.html``."""
    return path is not None and path.is_dir() and (path / "index.html").is_file()


def package_version() -> str:
    """Installed OmniScientist version the running process was launched from."""
    try:
        from omni import __version__
    except ImportError:
        return ""
    return str(__version__ or "")


def spa_version(dist: Path | None = None) -> str:
    """Version stamped into the SPA at package time (``version.json``)."""
    root = dist if dist is not None else web_dist_dir()
    if root is None:
        return ""
    path = root / SPA_VERSION_NAME
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("version") or "")


def web_dist_dir() -> Path | None:
    """Locate the built SPA without assuming the process CWD.

    Order: ``$OMNI_WEB_DIST`` (exclusive when set), packaged ``omni/data/web``,
    then a checkout ``web/dist`` walking toward the repository root.
    """
    env = os.environ.get("OMNI_WEB_DIST", "").strip()
    if env:
        candidate = Path(env).expanduser()
        return candidate if is_spa(candidate) else None
    packaged = packaged_web_dir()
    if is_spa(packaged):
        return packaged
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web" / "dist"
        if is_spa(candidate):
            return candidate
        if (parent / ".git").exists():
            break
    return None


def checkout_web_root() -> Path | None:
    """Return the sibling ``web/`` source tree when this is a git checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web"
        if (candidate / "package.json").is_file() and (
            (candidate / "vite.config.ts").is_file()
            or (candidate / "vite.config.js").is_file()
        ):
            return candidate
        if (parent / ".git").exists():
            break
    return None


def spa_build_commands(web_root: Path) -> list[list[str]] | None:
    """Frontend commands that produce ``web/dist`` without requiring a network model."""
    node = shutil.which("node") or shutil.which("node.exe")
    if node is None:
        return None
    vite_js = web_root / "node_modules" / "vite" / "bin" / "vite.js"
    if vite_js.is_file():
        return [[node, str(vite_js), "build"]]
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [[pnpm, "install"], [pnpm, "exec", "vite", "build"]]
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        return [[npm, "install"], [npm, "exec", "--", "vite", "build"]]
    return None


def prepare_web_ui(*, timeout: float = 180) -> Path | None:
    """Build ``web/dist`` once when this is a checkout and a toolchain exists.

    Installed wheels never reach this path: they already have ``omni/data/web``.
    Tests must not call this against a live network; they mock the commands.
    """
    existing = web_dist_dir()
    if existing is not None:
        return existing
    root = checkout_web_root()
    if root is None:
        return None
    commands = spa_build_commands(root)
    if not commands:
        return None
    env = os.environ.copy()
    env.pop("npm_config_ignore_scripts", None)
    env.setdefault("PNPM_ALLOW_BUILD_SCRIPTS", "esbuild")
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
    built = root / "dist"
    if not is_spa(built):
        return None
    version = package_version().strip()
    if version:
        try:
            (built / SPA_VERSION_NAME).write_text(
                json.dumps({"version": version}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
    return built


def ensure_web_ui(*, build: bool = True) -> Path:
    """Return a ready SPA directory or raise :class:`WebUiMissing`.

    ``omni web`` calls this *before* binding the port so a missing UI is a
    terminal error, not a browser 503 after advertising a URL.
    """
    dist = web_dist_dir()
    if dist is not None:
        return dist
    if build:
        dist = prepare_web_ui()
        if dist is not None:
            return dist
    raise WebUiMissing(MISSING_UI_HINT)
