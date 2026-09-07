"""Codex-parity clipboard image paste: paths, errors, and temp PNG persist."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from omni.cli.clipboard_paste import (
    EncodedImageFormat,
    PasteImageError,
    failed_paste_message,
    normalize_pasted_path,
    paste_image_as_png,
    paste_image_hint,
    paste_image_shortcut,
    paste_image_to_temp_png,
    pasted_image_format,
)
from omni.cli.composer_images import (
    ComposerImages,
    bind_composer_images,
    next_image_placeholder,
)
from omni.core.image_files import inspect_image

# 1×1 PNG (same fixture the VLM probe uses).
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_error_strings_match_codex() -> None:
    assert str(PasteImageError.unavailable("arboard down")) == (
        "clipboard unavailable: arboard down"
    )
    assert str(PasteImageError.no_image("empty")) == "no image on clipboard: empty"
    assert str(PasteImageError.encode_failed("rgba")) == "could not encode image: rgba"
    assert str(PasteImageError.io_error("disk")) == "io error: disk"
    assert failed_paste_message(PasteImageError.no_image("empty")) == (
        "Failed to paste image: no image on clipboard: empty"
    )


def test_shortcut_is_ctrl_v_except_under_wsl() -> None:
    assert paste_image_shortcut(is_wsl=False) == "Ctrl+V"
    assert paste_image_shortcut(is_wsl=True) == "Ctrl+Alt+V"
    assert paste_image_hint(is_wsl=False) == "Ctrl+V paste image"
    assert paste_image_hint(is_wsl=True) == "Ctrl+Alt+V paste image"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX file URL")
def test_normalize_file_url() -> None:
    assert normalize_pasted_path("file:///tmp/example.png") == Path("/tmp/example.png")


def test_normalize_quoted_and_escaped_unix_path() -> None:
    assert normalize_pasted_path('"/home/user/My File.png"') == Path(
        "/home/user/My File.png"
    )
    assert normalize_pasted_path("'/home/user/My File.png'") == Path(
        "/home/user/My File.png"
    )
    assert normalize_pasted_path("/home/user/My\\ File.png") == Path(
        "/home/user/My File.png"
    )


def test_normalize_multiple_tokens_is_not_a_path() -> None:
    assert normalize_pasted_path("/home/a.png /home/b.png") is None


def test_wsl_maps_windows_drive_path() -> None:
    from omni.cli.clipboard_paste import _convert_windows_path_to_wsl

    assert _convert_windows_path_to_wsl(r"C:\Users\Alice\Pictures\a.png") == Path(
        "/mnt/c/Users/Alice/Pictures/a.png"
    )
    assert _convert_windows_path_to_wsl(r"\\\\server\\share\\a.png") is None


def test_normalize_windows_drive_path() -> None:
    result = normalize_pasted_path(r"C:\Temp\example.png")
    assert result is not None
    assert "example.png" in str(result)


def test_pasted_image_format_png_jpeg_other() -> None:
    assert pasted_image_format(Path("/a/b/c.PNG")) is EncodedImageFormat.PNG
    assert pasted_image_format(Path("/a/b/c.jpg")) is EncodedImageFormat.JPEG
    assert pasted_image_format(Path("/a/b/c.webp")) is EncodedImageFormat.OTHER


def test_paste_image_to_temp_png_persists(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    monkeypatch.setattr(
        mod,
        "_read_system_clipboard_image",
        lambda: (_TINY_PNG, EncodedImageFormat.PNG),
    )
    monkeypatch.setattr(mod.tempfile, "tempdir", str(tmp_path))

    path, info = paste_image_to_temp_png()
    try:
        assert path.exists()
        assert path.name.startswith("omni-clipboard-")
        assert path.suffix == ".png"
        assert info.width == 1 and info.height == 1
        assert inspect_image(path) is not None
    finally:
        path.unlink(missing_ok=True)


def test_paste_image_writes_into_workspace_inputs(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    monkeypatch.setattr(
        mod,
        "_read_system_clipboard_image",
        lambda: (_TINY_PNG, EncodedImageFormat.PNG),
    )
    dest = tmp_path / "inputs"
    path, info = paste_image_to_temp_png(dest)
    assert path.parent == dest
    assert path.name.startswith("clipboard-")
    assert path.suffix == ".png"
    assert info.width == 1 and info.height == 1
    assert inspect_image(path) is not None


def test_paste_image_as_png_rejects_empty(monkeypatch) -> None:
    from omni.cli import clipboard_paste as mod

    def _empty() -> tuple[bytes, EncodedImageFormat]:
        raise PasteImageError.no_image("empty clipboard")

    monkeypatch.setattr(mod, "_read_system_clipboard_image", _empty)
    with pytest.raises(PasteImageError, match="no image on clipboard"):
        paste_image_as_png()


def test_image_placeholders_number_and_expand(tmp_path: Path) -> None:
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(_TINY_PNG)
    second.write_bytes(_TINY_PNG)

    assert next_image_placeholder("") == "[Image #1]"
    assert next_image_placeholder("see [Image #1] and [Image #3]") == "[Image #4]"

    pending = ComposerImages()
    one = pending.attach(first, "")
    two = pending.attach(second, one)
    assert one == "[Image #1]"
    assert two == "[Image #2]"

    draft = f"{one} look at this {two}"
    expanded = bind_composer_images(draft, pending.items)
    assert "[Image #1]" in expanded
    assert str(first) in expanded
    assert str(second) in expanded

    dropped = bind_composer_images("[Image #2] only", pending.items)
    assert str(first) not in dropped
    assert str(second) in dropped


def test_normalize_blank_and_unbalanced_quote() -> None:
    assert normalize_pasted_path("   ") is None
    assert normalize_pasted_path('"unterminated-path') == Path('"unterminated-path')


def test_paste_image_as_png_encode_failed(monkeypatch) -> None:
    from omni.cli import clipboard_paste as mod

    monkeypatch.setattr(
        mod,
        "_read_system_clipboard_image",
        lambda: (b"not-an-image", EncodedImageFormat.OTHER),
    )
    with pytest.raises(PasteImageError, match="could not encode"):
        paste_image_as_png()


def test_read_clipboard_dispatches_platforms(monkeypatch) -> None:
    from omni.cli import clipboard_paste as mod

    monkeypatch.setattr(mod.sys, "platform", "freebsd")
    with pytest.raises(PasteImageError, match="unsupported platform"):
        mod._read_system_clipboard_image()

    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        mod, "_macos_clipboard_image", lambda: (_TINY_PNG, EncodedImageFormat.PNG)
    )
    assert mod._read_system_clipboard_image()[1] is EncodedImageFormat.PNG

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(
        mod, "_windows_clipboard_image", lambda: (_TINY_PNG, EncodedImageFormat.PNG)
    )
    assert mod._read_system_clipboard_image()[0] == _TINY_PNG

    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(
        mod, "_linux_clipboard_image", lambda: (_TINY_PNG, EncodedImageFormat.PNG)
    )
    assert mod._read_system_clipboard_image()[0] == _TINY_PNG

    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod, "_macos_clipboard_image", lambda: "empty clipboard")
    with pytest.raises(PasteImageError, match="no image on clipboard"):
        mod._read_system_clipboard_image()


def test_macos_clipboard_image_file_png_and_errors(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    img = tmp_path / "clip.png"
    img.write_bytes(_TINY_PNG)
    monkeypatch.setattr(mod, "_macos_clipboard_file", lambda: img)
    assert mod._macos_clipboard_image()[0] == _TINY_PNG

    monkeypatch.setattr(mod, "_macos_clipboard_file", lambda: None)
    monkeypatch.setattr(mod, "_macos_clipboard_pngf", lambda: _TINY_PNG)
    assert mod._macos_clipboard_image()[1] is EncodedImageFormat.PNG

    monkeypatch.setattr(mod, "_macos_clipboard_pngf", lambda: b"")
    assert "no image data" in mod._macos_clipboard_image()

    monkeypatch.setattr(mod, "_macos_clipboard_file", lambda: tmp_path / "missing.png")
    assert "not an image" in mod._macos_clipboard_image()


def test_macos_clipboard_pngf_and_file(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    dest = tmp_path / "osascript.png"
    dest.write_bytes(_TINY_PNG)

    class _Handle:
        name = str(dest)

        def close(self) -> None:
            return None

    monkeypatch.setattr(mod.tempfile, "NamedTemporaryFile", lambda **_k: _Handle())
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: type("R", (), {"stdout": "ok\n"})())
    assert mod._macos_clipboard_pngf() == _TINY_PNG

    dest.write_bytes(b"not-png")
    assert mod._macos_clipboard_pngf() == b""

    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: None)
    assert mod._macos_clipboard_pngf() == b""

    img = tmp_path / "from-finder.png"
    img.write_bytes(_TINY_PNG)
    monkeypatch.setattr(
        mod, "_run", lambda *_a, **_k: type("R", (), {"stdout": f"{img}\n"})()
    )
    assert mod._macos_clipboard_file() == img
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: None)
    assert mod._macos_clipboard_file() is None
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: type("R", (), {"stdout": "\n"})())
    assert mod._macos_clipboard_file() is None


def test_linux_and_windows_clipboard_backends(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    img = tmp_path / "a.png"
    img.write_bytes(_TINY_PNG)
    monkeypatch.setattr(mod, "_linux_uri_list", lambda: [img])
    assert mod._linux_clipboard_image()[0] == _TINY_PNG

    monkeypatch.setattr(mod, "_linux_uri_list", lambda: [])
    monkeypatch.setattr(
        mod,
        "_linux_clipboard_type",
        lambda mime: _TINY_PNG if mime == "image/png" else b"",
    )
    assert mod._linux_clipboard_image()[1] is EncodedImageFormat.PNG

    monkeypatch.setattr(mod, "_linux_clipboard_type", lambda _mime: b"")
    monkeypatch.setattr(mod, "is_probably_wsl", lambda: True)
    assert "WSL" in mod._linux_clipboard_image()

    monkeypatch.setattr(mod, "is_probably_wsl", lambda: False)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    with pytest.raises(PasteImageError, match="wl-paste"):
        mod._linux_clipboard_image()

    monkeypatch.setattr(
        mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xclip" else None
    )
    assert "no image data" in mod._linux_clipboard_image()

    monkeypatch.setattr(mod, "_dump_windows_clipboard_image", lambda: img)
    assert mod._windows_clipboard_image()[0] == _TINY_PNG
    monkeypatch.setattr(mod, "_dump_windows_clipboard_image", lambda: None)
    assert "no image" in mod._windows_clipboard_image()
    junk = tmp_path / "note.txt"
    junk.write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(mod, "_dump_windows_clipboard_image", lambda: junk)
    assert "non-image" in mod._windows_clipboard_image()


def test_linux_uri_list_type_and_windows_dump(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    img = tmp_path / "listed.png"
    img.write_bytes(_TINY_PNG)
    monkeypatch.setattr(
        mod,
        "_linux_clipboard_type",
        lambda mime: f"file://{img}\n# skip\n".encode() if mime == "text/uri-list" else b"",
    )
    assert img in mod._linux_uri_list()
    monkeypatch.setattr(mod, "_linux_clipboard_type", lambda _mime: b"")
    assert mod._linux_uri_list() == []
    monkeypatch.undo()

    class _Bytes:
        stdout = _TINY_PNG

    monkeypatch.setattr(mod.shutil, "which", lambda name: name if name == "wl-paste" else None)
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: _Bytes())
    assert mod._linux_clipboard_type("image/png") == _TINY_PNG

    monkeypatch.setattr(mod.shutil, "which", lambda name: name if name == "xclip" else None)
    assert mod._linux_clipboard_type("image/png") == _TINY_PNG

    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    assert mod._linux_clipboard_type("image/png") == b""

    monkeypatch.setattr(mod, "_powershell_commands", lambda: ["pwsh"])
    monkeypatch.setattr(
        mod, "_run", lambda *_a, **_k: type("R", (), {"stdout": f"{img}\n"})()
    )
    assert mod._dump_windows_clipboard_image() == img
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: None)
    assert mod._dump_windows_clipboard_image() is None
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: type("R", (), {"stdout": ""})())
    assert mod._dump_windows_clipboard_image() is None


def test_wsl_fallback_and_linux_retry(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    img = tmp_path / "wsl.png"
    img.write_bytes(_TINY_PNG)
    monkeypatch.setattr(mod, "is_probably_wsl", lambda: False)
    assert mod._try_wsl_clipboard_fallback(PasteImageError.no_image("empty")) is None
    monkeypatch.setattr(mod, "is_probably_wsl", lambda: True)
    assert mod._try_wsl_clipboard_fallback(PasteImageError.encode_failed("rgba")) is None
    monkeypatch.setattr(mod, "_dump_windows_clipboard_image", lambda: None)
    assert mod._try_wsl_clipboard_fallback(PasteImageError.unavailable("arboard")) is None
    monkeypatch.setattr(mod, "_dump_windows_clipboard_image", lambda: img)
    path, info = mod._try_wsl_clipboard_fallback(PasteImageError.no_image("empty"))
    assert path == img
    assert info.width == 1

    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(
        mod,
        "paste_image_as_png",
        lambda: (_ for _ in ()).throw(PasteImageError.no_image("empty")),
    )
    monkeypatch.setattr(
        mod, "_try_wsl_clipboard_fallback", lambda _exc: (img, inspect_image(img))
    )
    dest = tmp_path / "inputs"
    dest.mkdir()
    persisted, info = paste_image_to_temp_png(dest)
    assert persisted.parent == dest
    assert info.height == 1
    path, _info = paste_image_to_temp_png()
    assert path == img


def test_persist_and_run_error_paths(monkeypatch, tmp_path: Path) -> None:
    from omni.cli import clipboard_paste as mod

    def _disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_user_input", _disk_full)
    with pytest.raises(PasteImageError, match="io error"):
        mod._persist_png(_TINY_PNG, tmp_path)
    img = tmp_path / "src.png"
    img.write_bytes(_TINY_PNG)
    with pytest.raises(PasteImageError, match="io error"):
        mod._persist_existing(img, tmp_path)

    class _Boom:
        name = str(tmp_path / "omni-clipboard-x.png")

        def write(self, _data: bytes) -> None:
            raise OSError("disk full")

        def close(self) -> None:
            return None

    monkeypatch.setattr(mod.tempfile, "NamedTemporaryFile", lambda **_k: _Boom())
    with pytest.raises(PasteImageError, match="io error"):
        mod._write_temp_png(_TINY_PNG)

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=1)

    monkeypatch.setattr(mod.subprocess, "run", _timeout)
    assert mod._run(["osascript"]) is None
    assert mod._load_image_file(tmp_path / "missing.png") is None


def test_wsl_detection_powershell_and_escape(monkeypatch) -> None:
    from omni.cli import clipboard_paste as mod

    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert mod.is_probably_wsl() is False
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.Path, "read_text", lambda self, **_k: "Linux version Microsoft WSL2")
    assert mod.is_probably_wsl() is True
    monkeypatch.setattr(mod.Path, "read_text", lambda self, **_k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    assert mod.is_probably_wsl() is False
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert mod.is_probably_wsl() is True

    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert "powershell" in mod._powershell_commands()
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod._powershell_commands() == ["powershell.exe", "pwsh"]
    assert '\\"' in mod._applescript_escape('say "hi"')
    assert mod._convert_windows_path_to_wsl("no-drive") is None
    monkeypatch.setattr(mod, "is_probably_wsl", lambda: True)
    mapped = mod._normalize_windows_path(r"D:\shots\a.png")
    assert mapped == Path("/mnt/d/shots/a.png")
