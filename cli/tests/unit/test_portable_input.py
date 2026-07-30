"""UTF-8 --json-file / stdin loading (BUG-21, BUG-22)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from omni.compat.portable_input import PortableJsonError, load_json_object


def test_json_file_keeps_chinese_output_dir(tmp_path: Path) -> None:
    out_dir = tmp_path / "图表输出"
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"input": "RAG", "output_dir": str(out_dir)}, ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_json_object(json_file=str(payload))

    assert loaded["output_dir"] == str(out_dir)
    assert "图表输出" in loaded["output_dir"]


def test_json_file_wins_over_json_text(tmp_path: Path) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text('{"from":"file"}', encoding="utf-8")

    loaded = load_json_object(json_text='{"from":"arg"}', json_file=str(payload))

    assert loaded == {"from": "file"}


def test_stdin_decodes_utf8_bytes_not_console_encoding() -> None:
    raw = json.dumps({"output_dir": "C:/用户/输出"}, ensure_ascii=False).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252")

    loaded = load_json_object(stdin=stdin)

    assert loaded["output_dir"] == "C:/用户/输出"


def test_empty_payload_is_object() -> None:
    assert load_json_object(json_text="  ") == {}
    assert load_json_object(json_text="{}") == {}


def test_invalid_json_and_non_object_fail_closed() -> None:
    with pytest.raises(PortableJsonError) as invalid:
        load_json_object(json_text="{bad")
    assert invalid.value.code == "invalid_json"

    with pytest.raises(PortableJsonError) as payload:
        load_json_object(json_text="[]")
    assert payload.value.code == "invalid_payload"


def test_missing_json_file_is_structured(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(PortableJsonError) as exc:
        load_json_object(json_file=str(missing))
    assert exc.value.code == "json_file_unreadable"
