"""Install bundled scientist personas into SoulAgent's writable scanner root."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni.data import BUILTIN_SKILLS_DIR
from omni.personas.bundle_format import (
    BundledPersonaValidationError,
    validate_builtin_persona_collection,
    validate_persona_directory,
)

_LOCK_TIMEOUT_SECONDS = 15.0


class BuiltinPersonaInstallError(RuntimeError):
    """Raised when bundled personas cannot be validated or installed safely."""


@dataclass(frozen=True)
class BuiltinPersonaInstallResult:
    """Summarize a non-overwriting bundled-persona convergence pass."""

    installed: tuple[str, ...]
    skipped_existing: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """Return whether this pass installed at least one persona."""
        return bool(self.installed)


def builtin_persona_source_root() -> Path:
    """Return the bundled SoulAgent persona resource root for wheel or source mode."""
    return BUILTIN_SKILLS_DIR / "soulagent" / "assets" / "builtin-scientist-kg"


def _try_lock(handle: Any) -> bool:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _installation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise BuiltinPersonaInstallError(f"cannot open persona installation lock {path}: {exc}") from exc
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        while not acquired and time.monotonic() < deadline:
            acquired = _try_lock(handle)
            if not acquired:
                time.sleep(0.05)
        if not acquired:
            raise BuiltinPersonaInstallError(
                f"timed out waiting for persona installation lock {path}"
            )
        yield
    finally:
        if acquired:
            _unlock(handle)
        handle.close()


def install_builtin_personas(
    paths: Any,
    *,
    source_root: Path | None = None,
) -> BuiltinPersonaInstallResult:
    """Install missing bundled KGs without modifying any existing scientist directory.

    The source snapshot is fully validated before the writable scanner root is
    touched. Each missing persona is copied to a same-volume staging directory,
    validated again, and atomically renamed into place. An existing destination
    is always preserved, including one that is incomplete or locally modified.
    """
    source = (source_root or builtin_persona_source_root()).resolve()
    try:
        scientist_ids = validate_builtin_persona_collection(source)
    except BundledPersonaValidationError as exc:
        raise BuiltinPersonaInstallError(f"bundled scientist personas are invalid: {exc}") from exc

    home = Path(paths.home).resolve()
    scanner_root = Path(getattr(paths, "scientist_kg_dir", home / "scientist-kg")).resolve()
    installed: list[str] = []
    skipped: list[str] = []
    with _installation_lock(home / ".builtin-scientist-kg.lock"):
        scanner_root.mkdir(parents=True, exist_ok=True)
        catalog = json.loads((source / "index.json").read_text(encoding="utf-8-sig"))
        manifest_hashes = {
            entry["scientist_id"]: str(entry["manifest_sha256"]).lower()
            for entry in catalog["scientists"]
        }
        for scientist_id in scientist_ids:
            destination = scanner_root / scientist_id
            if destination.exists() or destination.is_symlink():
                skipped.append(scientist_id)
                continue
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".builtin-{scientist_id}-",
                    suffix=".tmp",
                    dir=home,
                )
            )
            try:
                shutil.copytree(source / scientist_id, staging, dirs_exist_ok=True)
                validate_persona_directory(
                    staging,
                    expected_id=scientist_id,
                    expected_manifest_sha256=manifest_hashes[scientist_id],
                )
                try:
                    os.rename(staging, destination)
                except OSError as exc:
                    if destination.exists() or destination.is_symlink():
                        skipped.append(scientist_id)
                        continue
                    raise BuiltinPersonaInstallError(
                        f"cannot atomically install scientist persona {scientist_id}: {exc}"
                    ) from exc
                installed.append(scientist_id)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    return BuiltinPersonaInstallResult(tuple(installed), tuple(skipped))
