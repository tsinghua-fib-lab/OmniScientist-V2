"""Workspace ``inputs/`` is the shared drop for CLI, web, and WeChat files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from omni.core.file_mentions import format_mention
from omni.core.user_inputs import (
    USER_INPUT_DIRNAME,
    bind_input_mentions,
    clipboard_input_filename,
    ensure_inputs_dir,
    inputs_dir_for,
    next_image_placeholder,
    unique_input_path,
    write_user_input,
)


def test_inputs_dir_is_sibling_of_artifacts(tmp_path: Path) -> None:
    paths = SimpleNamespace(project_dir=tmp_path / "store")
    dest = inputs_dir_for(paths)
    assert dest == paths.project_dir / USER_INPUT_DIRNAME
    assert dest.name == "inputs"
    created = ensure_inputs_dir(paths)
    assert created.is_dir()
    assert created == dest


def test_write_user_input_keeps_name_and_avoids_collisions(tmp_path: Path) -> None:
    first = write_user_input(tmp_path, b"one", filename="photo.png")
    second = write_user_input(tmp_path, b"two", filename="photo.png")
    assert first.name == "photo.png"
    assert second.name == "photo-2.png"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_unique_input_path_rejects_traversal(tmp_path: Path) -> None:
    path = unique_input_path(tmp_path, "../secret.png")
    assert path.parent == tmp_path
    assert path.name == "secret.png"


def test_clipboard_filename_is_png() -> None:
    name = clipboard_input_filename()
    assert name.startswith("clipboard-")
    assert name.endswith(".png")


def test_bind_input_mentions_adds_image_placeholder(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = bind_input_mentions("look at this", [image])
    assert "[Image #1]" in text
    assert format_mention(image) in text
    again = bind_input_mentions(text, [image])
    assert again == text
    assert next_image_placeholder(text) == "[Image #2]"
