"""Small atomic writers for owner-only configuration files."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import tomli_w


def write_private_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write TOML with owner-only permissions on POSIX systems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            tomli_w.dump(data, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = ["write_private_toml"]
