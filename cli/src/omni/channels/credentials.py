"""Local credential storage helpers for IM channel login.

Channel ``login`` should not write platform secrets into project files. On
macOS we use the system Keychain through the built-in ``security`` tool; other
platforms must explicitly opt into file-based ``secrets.toml`` storage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

_SERVICE = "omniscientist"
_REF_PREFIX = "macos-keychain"
KNOWN_CHANNEL_SECRET_KEYS: dict[str, tuple[str, ...]] = {
    "feishu": ("app_secret",),
    "dingtalk": ("client_secret",),
    "wechat": ("bot_token",),
}


class CredentialStoreError(RuntimeError):
    """Raised when an encrypted credential backend is unavailable."""


def store_channel_secret(
    paths: Any,
    channel: str,
    key: str,
    value: str,
    *,
    backend: str = "auto",
) -> str:
    """Store one channel secret and return a non-secret reference string.

    ``backend='auto'`` defaults to macOS Keychain when available. It refuses to
    silently fall back to plaintext storage; callers must pass
    ``backend='file'`` to use ``secrets.toml`` deliberately.
    """
    value = value.strip()
    if not value:
        return ""
    chosen = _choose_backend(backend)
    if chosen == "macos-keychain":
        _store_macos_keychain(channel, key, value)
        return f"{_REF_PREFIX}:{channel}:{key}"
    if chosen == "file":
        _store_secrets_file(paths.secrets_file, channel, key, value)
        return ""
    raise CredentialStoreError(f"unsupported credential store '{backend}'")


def resolve_secret_ref(ref: str) -> str:
    """Resolve a secret reference produced by :func:`store_channel_secret`."""
    parts = ref.split(":", 2)
    if len(parts) != 3 or parts[0] != _REF_PREFIX:
        return ""
    return _read_macos_keychain(parts[1], parts[2])


def delete_channel_secret(channel: str, key: str) -> bool:
    """Delete one Omni-owned macOS Keychain secret if it exists.

    File-backed channel secrets live under ``OMNI_HOME`` and are removed with
    that data root. This helper intentionally targets the exact service/account
    pair written by :func:`store_channel_secret`; it never enumerates or alters
    unrelated Keychain entries.
    """
    if not _has_macos_keychain():
        return False
    proc = subprocess.run(
        [
            "security",
            "delete-generic-password",
            "-s",
            _SERVICE,
            "-a",
            _account(channel, key),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def purge_known_channel_secrets() -> list[str]:
    """Delete all Keychain accounts Omni can create and return removed names."""
    removed: list[str] = []
    for channel, keys in KNOWN_CHANNEL_SECRET_KEYS.items():
        for key in keys:
            if delete_channel_secret(channel, key):
                removed.append(_account(channel, key))
    return removed


def _choose_backend(backend: str) -> str:
    backend = backend.strip().lower() or "auto"
    if backend in {"keychain", "macos-keychain"}:
        if not _has_macos_keychain():
            raise CredentialStoreError("macOS Keychain is not available on this machine")
        return "macos-keychain"
    if backend == "auto":
        if _has_macos_keychain():
            return "macos-keychain"
        raise CredentialStoreError(
            "no encrypted credential store found; pass --credential-store file to use secrets.toml"
        )
    if backend in {"file", "secrets", "secrets.toml"}:
        return "file"
    raise CredentialStoreError("credential store must be one of: auto, keychain, file")


def _has_macos_keychain() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def keychain_available() -> bool:
    """True when an OS-level encrypted credential store (macOS Keychain) exists.

    Distinguishes "the keychain refused a write at runtime" (store present,
    fall back is reasonable) from "no encrypted store on this platform" (the
    caller should keep its deliberate opt-in to ``secrets.toml``).
    """
    return _has_macos_keychain()


def _account(channel: str, key: str) -> str:
    return f"channel:{channel}:{key}"


def _store_macos_keychain(channel: str, key: str, value: str) -> None:
    proc = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-s",
            _SERVICE,
            "-a",
            _account(channel, key),
            "-w",
            value,
            "-U",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CredentialStoreError((proc.stderr or "failed to write macOS Keychain").strip())


def _read_macos_keychain(channel: str, key: str) -> str:
    proc = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            _SERVICE,
            "-a",
            _account(channel, key),
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.rstrip("\n")


def _store_secrets_file(path: Path, channel: str, key: str, value: str) -> None:
    data: dict[str, Any] = {}
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    data.setdefault("channels", {}).setdefault(channel, {})[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
