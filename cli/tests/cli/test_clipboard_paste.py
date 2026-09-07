"""Codex-parity clipboard image paste: paths, errors, and temp PNG persist."""

from __future__ import annotations

import base64
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
