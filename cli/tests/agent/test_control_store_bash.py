"""Bash refuses writes into frozen Omni control stores, same as write_file."""

from __future__ import annotations

import os
from pathlib import Path

from omni.skills_runtime.control_store_guard import command_writes_frozen_control_store


def _store_db() -> Path:
    home = Path(os.environ["OMNI_HOME"])
    return home / "workspaces" / "demo" / "sessions.sqlite3"


def test_touch_under_the_store_is_a_control_write() -> None:
    path = command_writes_frozen_control_store(f"touch {_store_db()}")
    assert path is not None
    assert path.name == "sessions.sqlite3"


def test_python_against_the_store_is_a_control_write() -> None:
    db = _store_db()
    command = f"python -c \"import sqlite3; sqlite3.connect('{db}')\""
    assert command_writes_frozen_control_store(command) is not None


def test_sqlite_select_against_the_store_is_allowed() -> None:
    db = _store_db()
    command = f'sqlite3 {db} "SELECT id FROM tasks LIMIT 1"'
    assert command_writes_frozen_control_store(command) is None


def test_relative_workspace_file_is_not_the_control_store() -> None:
    assert command_writes_frozen_control_store("touch sessions.sqlite3") is None
    assert command_writes_frozen_control_store("python -c 'print(1)'") is None
