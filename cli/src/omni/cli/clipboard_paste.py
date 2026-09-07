"""System-clipboard image paste, matching Codex's TUI contract.

Codex (``codex-rs/tui/src/clipboard_paste.rs``) does **not** use the OS paste
chord. On macOS ``Cmd+V`` stays the terminal's text paste (bracketed paste).
``Ctrl+V`` and ``Ctrl+Alt+V`` are reserved: they read the *system* clipboard
via a native API, accept either image bytes (Chrome / screenshots) or a file
list (Finder / Explorer), and always persist a PNG.

This module keeps that chord and those error strings on macOS, Linux, Windows,
and WSL. The TUI never invents a second shortcut per platform.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from omni.core.image_files import (
    EncodedImageFormat,
    PastedImageInfo,
    encode_png,
    inspect_image,
    inspect_image_bytes,
    pasted_image_format,
)
from omni.core.user_inputs import clipboard_input_filename, write_user_input

_COMMAND_TIMEOUT_S = 8.0
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Codex ``PasteImageError`` Display strings. Surfaces must prefix
# ``Failed to paste image: `` and otherwise leave these intact.
_UNAVAILABLE = "clipboard unavailable: "
_NO_IMAGE = "no image on clipboard: "
_ENCODE_FAILED = "could not encode image: "
_IO_ERROR = "io error: "


class PasteImageError(Exception):
    """Clipboard image paste failed. ``str(self)`` is the Codex error body."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}{detail}")

    @classmethod
    def unavailable(cls, detail: str) -> PasteImageError:
        return cls(_UNAVAILABLE, detail)

    @classmethod
    def no_image(cls, detail: str) -> PasteImageError:
        return cls(_NO_IMAGE, detail)

    @classmethod
    def encode_failed(cls, detail: str) -> PasteImageError:
        return cls(_ENCODE_FAILED, detail)

    @classmethod
    def io_error(cls, detail: str) -> PasteImageError:
        return cls(_IO_ERROR, detail)


def failed_paste_message(error: BaseException) -> str:
    """User-visible line. Codex: ``Failed to paste image: {err}``."""
    return f"Failed to paste image: {error}"


def paste_image_shortcut(*, is_wsl: bool | None = None) -> str:
    """Footer chord. Codex shows Ctrl+Alt+V under WSL, otherwise Ctrl+V."""
    if is_wsl is None:
        is_wsl = is_probably_wsl()
    return "Ctrl+Alt+V" if is_wsl else "Ctrl+V"


def paste_image_hint(*, is_wsl: bool | None = None) -> str:
    """Idle-footer token: ``Ctrl+V paste image`` (or Ctrl+Alt+V under WSL)."""
    return f"{paste_image_shortcut(is_wsl=is_wsl)} paste image"


def paste_image_as_png() -> tuple[bytes, PastedImageInfo]:
    """Read the system clipboard and return PNG bytes plus pixel size."""
    data, source_format = _read_system_clipboard_image()
    encoded = encode_png(data, source_format)
    if encoded is None:
        raise PasteImageError.encode_failed("unrecognised image bytes")
    return encoded


def paste_image_to_temp_png(
    dest_dir: Path | None = None,
) -> tuple[Path, PastedImageInfo]:
    """Persist a clipboard PNG and return its path.

    Codex writes ``codex-clipboard-*.png`` into the OS temp dir. When
    ``dest_dir`` is set (the workspace ``inputs/`` folder) Omni writes there
    instead so CLI, web, and WeChat share one durable location. Without a
    destination the Codex tempfile fallback remains, for tests and callers
    that have no workspace yet. Failures on Linux/WSL retry through
    PowerShell when the native clipboard cannot see Windows.
    """
    try:
        png, info = paste_image_as_png()
    except PasteImageError as exc:
        if sys.platform.startswith("linux"):
            fallback = _try_wsl_clipboard_fallback(exc)
            if fallback is not None:
                path, info = fallback
                if dest_dir is not None:
                    return _persist_existing(path, dest_dir), info
                return path, info
        raise
    return _persist_png(png, dest_dir), info


def normalize_pasted_path(pasted: str) -> Path | None:
    """Turn a pasted token into one filesystem path, or ``None``.

    Accepts ``file://`` URLs, quoted paths, a single shell-escaped path, and
    Windows drive / UNC paths (mapped to ``/mnt/<drive>`` under WSL).
    """
    pasted = pasted.strip()
    if not pasted:
        return None
    unquoted = _strip_wrapping_quotes(pasted)

    if unquoted.lower().startswith("file:"):
        parsed = urlparse(unquoted)
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            raw = url2pathname(unquote(parsed.path))
            if raw:
                return Path(raw)

    windows = _normalize_windows_path(unquoted)
    if windows is not None:
        return windows

    parts = _shell_split_one(pasted)
    if parts is None:
        return None
    windows = _normalize_windows_path(parts)
    if windows is not None:
        return windows
    return Path(parts)


def is_probably_wsl() -> bool:
    """True on WSL. Codex checks ``/proc/version`` then ``WSL_*`` env vars."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        version = ""
    lowered = version.lower()
    if "microsoft" in lowered or "wsl" in lowered:
        return True
    return bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


# ── clipboard backends ───────────────────────────────────────────────────


def _read_system_clipboard_image() -> tuple[bytes, EncodedImageFormat]:
    """Return clipboard image bytes. Prefers a file list, then image data."""
    errors: list[str] = []
    if sys.platform == "darwin":
        result = _macos_clipboard_image()
    elif sys.platform == "win32":
        result = _windows_clipboard_image()
    elif sys.platform.startswith("linux"):
        result = _linux_clipboard_image()
    else:
        raise PasteImageError.unavailable(f"unsupported platform: {sys.platform}")
    if isinstance(result, tuple):
        return result
    errors.append(result)
    raise PasteImageError.no_image("; ".join(errors) if errors else "empty clipboard")


def _macos_clipboard_image() -> tuple[bytes, EncodedImageFormat] | str:
    file_path = _macos_clipboard_file()
    if file_path is not None:
        loaded = _load_image_file(file_path)
        if loaded is not None:
            return loaded
    png = _macos_clipboard_pngf()
    if png:
        return png, EncodedImageFormat.PNG
    if file_path is None:
        return "macOS clipboard has no image data or image file"
    return f"clipboard file is not an image: {file_path}"


def _macos_clipboard_pngf() -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="omni-clip-src-", suffix=".png", delete=False)
    dest = Path(handle.name)
    handle.close()
    script = (
        f'set outPath to "{_applescript_escape(str(dest))}"\n'
        "try\n"
        "    set pngData to the clipboard as «class PNGf»\n"
        "    set fileRef to open for access (POSIX file outPath) with write permission\n"
        "    set eof fileRef to 0\n"
        "    write pngData to fileRef\n"
        "    close access fileRef\n"
        '    return "ok"\n'
        "on error errText\n"
        "    try\n"
        "        close access (POSIX file outPath)\n"
        "    end try\n"
        '    return "err:" & errText\n'
        "end try"
    )
    try:
        completed = _run(["osascript", "-e", script], text=True)
        if completed is None or not (completed.stdout or "").strip().startswith("ok"):
            return b""
        data = dest.read_bytes()
        return data if data.startswith(_PNG_MAGIC) else b""
    except OSError:
        return b""
    finally:
        dest.unlink(missing_ok=True)


def _macos_clipboard_file() -> Path | None:
    completed = _run(
        ["osascript", "-e", 'POSIX path of (the clipboard as «class furl»)'],
        text=True,
    )
    if completed is None:
        return None
    raw = (completed.stdout or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def _linux_clipboard_image() -> tuple[bytes, EncodedImageFormat] | str:
    for uri in _linux_uri_list():
        loaded = _load_image_file(uri)
        if loaded is not None:
            return loaded
    for mime, fmt in (
        ("image/png", EncodedImageFormat.PNG),
        ("image/jpeg", EncodedImageFormat.JPEG),
        ("image/gif", EncodedImageFormat.OTHER),
        ("image/webp", EncodedImageFormat.OTHER),
        ("image/bmp", EncodedImageFormat.OTHER),
    ):
        data = _linux_clipboard_type(mime)
        if data:
            return data, fmt
    if is_probably_wsl():
        return "Linux clipboard empty (WSL cannot see the Windows clipboard via arboard)"
    tools = []
    if shutil.which("wl-paste"):
        tools.append("wl-paste")
    if shutil.which("xclip"):
        tools.append("xclip")
    if not tools:
        raise PasteImageError.unavailable("install wl-paste (Wayland) or xclip (X11)")
    return "no image data or image file on the clipboard"


def _linux_uri_list() -> list[Path]:
    raw = _linux_clipboard_type("text/uri-list")
    if not raw:
        return []
    out: list[Path] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        path = normalize_pasted_path(token)
        if path is not None and path.exists():
            out.append(path)
    return out


def _linux_clipboard_type(mime: str) -> bytes:
    if shutil.which("wl-paste"):
        completed = _run(["wl-paste", "--no-newline", "--type", mime])
        if completed is not None and completed.stdout:
            return completed.stdout
    if shutil.which("xclip"):
        completed = _run(
            ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
        )
        if completed is not None and completed.stdout:
            return completed.stdout
    return b""


def _windows_clipboard_image() -> tuple[bytes, EncodedImageFormat] | str:
    dumped = _dump_windows_clipboard_image()
    if dumped is None:
        return "Windows clipboard has no image (Get-Clipboard -Format Image)"
    loaded = _load_image_file(dumped)
    if loaded is None:
        return f"PowerShell wrote a non-image: {dumped}"
    return loaded


def _dump_windows_clipboard_image() -> Path | None:
    """Codex's PowerShell: save ``Get-Clipboard -Format Image`` as a PNG."""
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$img = Get-Clipboard -Format Image; "
        "if ($img -ne $null) { "
        "$p=[System.IO.Path]::GetTempFileName(); "
        "$p = [System.IO.Path]::ChangeExtension($p,'png'); "
        "$img.Save($p,[System.Drawing.Imaging.ImageFormat]::Png); "
        "Write-Output $p "
        "} else { exit 1 }"
    )
    for cmd in _powershell_commands():
        completed = _run([cmd, "-NoProfile", "-Command", script], text=True)
        if completed is None:
            continue
        raw = (completed.stdout or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if path.exists():
            return path
    return None


def _try_wsl_clipboard_fallback(
    error: PasteImageError,
) -> tuple[Path, PastedImageInfo] | None:
    if not is_probably_wsl():
        return None
    if error.kind not in {_UNAVAILABLE, _NO_IMAGE}:
        return None
    win_path = _dump_windows_clipboard_image()
    if win_path is None:
        return None
    mapped = _convert_windows_path_to_wsl(str(win_path)) or win_path
    info = inspect_image(mapped)
    if info is None:
        return None
    return mapped, info


def _powershell_commands() -> list[str]:
    if sys.platform == "win32":
        return ["powershell.exe", "pwsh", "powershell"]
    return ["powershell.exe", "pwsh"]


def _load_image_file(path: Path) -> tuple[bytes, EncodedImageFormat] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if inspect_image_bytes(data) is None:
        return None
    return data, pasted_image_format(path)


def _persist_png(png: bytes, dest_dir: Path | None) -> Path:
    if dest_dir is not None:
        try:
            return write_user_input(
                dest_dir, png, filename=clipboard_input_filename()
            )
        except OSError as exc:
            raise PasteImageError.io_error(str(exc)) from exc
    return _write_temp_png(png)


def _persist_existing(path: Path, dest_dir: Path) -> Path:
    try:
        return write_user_input(
            dest_dir, path.read_bytes(), filename=clipboard_input_filename()
        )
    except OSError as exc:
        raise PasteImageError.io_error(str(exc)) from exc


def _write_temp_png(png: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="omni-clipboard-", suffix=".png", delete=False
    )
    path = Path(handle.name)
    try:
        handle.write(png)
        handle.close()
    except OSError as exc:
        handle.close()
        path.unlink(missing_ok=True)
        raise PasteImageError.io_error(str(exc)) from exc
    return path


# ── path helpers ─────────────────────────────────────────────────────────


def _strip_wrapping_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _shell_split_one(text: str) -> str | None:
    """One shell token, or ``None`` when the paste is more than one path."""
    import shlex

    try:
        parts = shlex.split(text)
    except ValueError:
        stripped = _strip_wrapping_quotes(text)
        return stripped or None
    if len(parts) != 1:
        return None
    return parts[0]


def _normalize_windows_path(text: str) -> Path | None:
    drive = (
        bool(text)
        and text[0].isalpha()
        and text[1:2] == ":"
        and text[2:3] in {"\\", "/"}
    )
    unc = text.startswith("\\\\")
    if not drive and not unc:
        return None
    if is_probably_wsl() and not unc:
        converted = _convert_windows_path_to_wsl(text)
        if converted is not None:
            return converted
    return Path(text)


def _convert_windows_path_to_wsl(text: str) -> Path | None:
    if text.startswith("\\\\"):
        return None
    if len(text) < 2 or not text[0].isalpha() or text[1] != ":":
        return None
    rest = text[2:].lstrip("\\/")
    parts = [part for part in re.split(r"[\\/]+", rest) if part]
    return Path("/mnt") / text[0].lower() / Path(*parts)


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run(
    argv: Sequence[str], *, text: bool = False
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed clipboard helpers, no user argv
            list(argv),
            capture_output=True,
            text=text,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
