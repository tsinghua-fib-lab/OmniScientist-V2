"""Turn-boundary reader for SoulAgent scientist-persona stomata.

OmniScientist's base identity (``role.md`` under ``~/.omni`` or the built-in
default) is loaded once and stays sticky for the process — the correct analogue
of Codex's session-static ``base_instructions``. The portable, host-neutral
``soulagent`` skill writes a *temporary, reversible* scientist persona into
``<project-root>/role.md`` guarded by a ``.soulagent/`` lock + state protocol;
its contract requires the running host to read that ready stoma at a **stable
turn boundary** and inject it as an *overlay* on top of the sticky base — never
hot-swapping the base identity, and never touching ``~/.omni/role.md``.

This module is that read-only adapter. It returns a decoded persona only when
SoulAgent has an active, committed persona *for this host*; otherwise it returns
the empty overlay and the turn is byte-for-byte identical to today. It is
deliberately fail-open: any missing, locked, mismatched, or corrupt state yields
no overlay so a broken persona never blocks or degrades a conversation.

Protocol mirrored from ``skills/soulagent`` (``origin/hyw``):

- ``<root>/.soulagent/state.json`` — ``{"host", "scientist_id", ...}``; present
  only while a persona is active, deleted on ``unload``.
- ``<root>/.soulagent/lock/writing`` — a write is in progress (``ready`` cleared).
- ``<root>/.soulagent/lock/ready`` — the stoma is committed and safe to read.
- ``<root>/role.md`` — the ``omniscientist`` host stoma (SoulAgent ``HOST_STOMA``).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# SoulAgent on-disk protocol (see module docstring). ``role.md`` is the stoma
# name SoulAgent maps to ``--host omniscientist``; keep this in lockstep with the
# skill's ``HOST_STOMA``.
_STATE_DIR = ".soulagent"
_LOCK_DIR = "lock"
_WRITING = "writing"
_READY = "ready"
_HOST = "omniscientist"
_STOMA_NAME = "role.md"
_BACKUP_SUFFIX = ".soulagent.bak"

# A persona write clears ``ready`` and holds ``writing`` only for the moment it
# takes to rewrite one small file, so a short bounded wait avoids showing a
# half-written persona without ever stalling the turn.
_READY_WAIT_S = 2.0
_POLL_S = 0.05


@dataclass(frozen=True)
class PersonaOverlay:
    """A resolved SoulAgent persona for the current project, or the empty overlay.

    ``text`` is the raw decoded persona prose from the stoma; :meth:`render`
    wraps it in the titled, guard-railed block injected after the base role.
    """

    scientist_id: str
    scientist_name: str
    text: str

    @property
    def active(self) -> bool:
        return bool(self.scientist_id and self.text.strip())

    def render(self) -> str:
        """The ``[Active scientist persona]`` block, or ``""`` when inactive.

        Additive overlay only: it steers judgment and style toward the scientist
        while explicitly deferring product identity, tool policy, safety, and
        citation duties to the sticky OmniScientist base above it.
        """
        if not self.active:
            return ""
        who = self.scientist_name or self.scientist_id
        return (
            "[Active scientist persona]\n"
            f"For this project a scientist persona — {who} (id: {self.scientist_id}) — is loaded "
            "via SoulAgent. Reason and make research judgments in the spirit of the persona below: "
            "its taste and priorities, how it frames problems, chooses methods, weighs evidence, "
            "and reacts to failure.\n"
            "This shapes judgment and voice only. Your product identity, available tools, safety "
            "boundaries, and citation duties remain exactly those of OmniScientist stated above. "
            "The persona is temporary and reversible — it disappears when SoulAgent is unloaded.\n\n"
            f"{self.text.strip()}"
        )


EMPTY_OVERLAY = PersonaOverlay("", "", "")


@dataclass(frozen=True)
class PersonaIdentity:
    """Committed SoulAgent identity without reading the persona prose."""

    scientist_id: str
    scientist_name: str

    @property
    def active(self) -> bool:
        return bool(self.scientist_id)


EMPTY_IDENTITY = PersonaIdentity("", "")


def _restore_from_backup(root: Path) -> None:
    """Complete an unload by restoring the original ``role.md`` from its backup.

    When SoulAgent loads a persona, the original ``role.md`` is backed up to
    ``role.md.soulagent.bak``.  A proper unload (``core.py unload``) restores
    it and cleans up.  If for any reason only ``state.json`` was removed but
    the backup was left behind — an incomplete unload — complete it here so
    the project reverts to its pre-persona state.
    """
    backup = root / (f"{_STOMA_NAME}{_BACKUP_SUFFIX}")
    if not backup.is_file():
        return
    try:
        target = root / _STOMA_NAME
        target.write_bytes(backup.read_bytes())
        backup.unlink()
    except OSError:
        pass


def load_persona_overlay(
    working_dir: str | Path | None,
    *,
    repair_incomplete_unload: bool = True,
) -> PersonaOverlay:
    """Read the active SoulAgent persona for ``working_dir``; empty when none.

    Reads strictly inside the project root and never reads or writes ``~/.omni``.
    Returns :data:`EMPTY_OVERLAY` whenever SoulAgent is inactive, targets another
    host, is mid-write past the wait budget, or the state/stoma cannot be read.
    """
    if not working_dir:
        return EMPTY_OVERLAY
    try:
        root = Path(working_dir).resolve()
    except OSError:
        return EMPTY_OVERLAY

    state_dir = root / _STATE_DIR
    state_path = state_dir / "state.json"
    # Presence of the SoulAgent state — not a bare ``role.md`` — is what makes
    # this a persona stoma, so a user's own project ``role.md`` is left untouched.
    if not state_path.is_file():
        # If state.json is missing but the backup still exists, the unload
        # was started (state removed) but not completed (role.md not restored).
        # Complete it now so the project reverts to its pre-persona state.
        if repair_incomplete_unload and not (state_dir / _LOCK_DIR / _WRITING).exists():
            _restore_from_backup(root)
        return EMPTY_OVERLAY
    if not _await_ready(state_dir / _LOCK_DIR):
        return EMPTY_OVERLAY
    state = _read_json(state_path)
    if not state:
        return EMPTY_OVERLAY
    if state.get("host") != _HOST:
        return EMPTY_OVERLAY
    scientist_id = str(state.get("scientist_id") or "").strip()
    if not scientist_id:
        return EMPTY_OVERLAY

    text = _read_stoma(root)
    if not text.strip():
        return EMPTY_OVERLAY
    confirmed = _read_json(state_path)
    if confirmed != state or not _ready_now(state_dir / _LOCK_DIR):
        return EMPTY_OVERLAY
    expected_hash = str(state.get("persona_sha256") or "").strip()
    if expected_hash and not _matches_persona_hash(text, expected_hash):
        return EMPTY_OVERLAY
    return PersonaOverlay(scientist_id, str(state.get("scientist_name") or "").strip(), text)


def load_persona_identity(
    working_dir: str | Path | None,
    *,
    repair_incomplete_unload: bool = False,
) -> PersonaIdentity:
    """Read only the committed SoulAgent identity for status surfaces.

    Unlike :func:`load_persona_overlay`, this metadata probe never opens the
    project ``role.md`` body.  The ready/writing protocol plus a non-empty file
    stat is enough for Web and other status surfaces to report the active
    scientist without repeatedly loading sensitive persona prose.
    """
    if not working_dir:
        return EMPTY_IDENTITY
    try:
        root = Path(working_dir).resolve()
    except OSError:
        return EMPTY_IDENTITY

    state_dir = root / _STATE_DIR
    state_path = state_dir / "state.json"
    if not state_path.is_file():
        if repair_incomplete_unload and not (state_dir / _LOCK_DIR / _WRITING).exists():
            _restore_from_backup(root)
        return EMPTY_IDENTITY
    if not _await_ready(state_dir / _LOCK_DIR):
        return EMPTY_IDENTITY
    state = _read_json(state_path)
    if not state:
        return EMPTY_IDENTITY
    if state.get("host") != _HOST:
        return EMPTY_IDENTITY
    scientist_id = str(state.get("scientist_id") or "").strip()
    if not scientist_id:
        return EMPTY_IDENTITY
    try:
        stoma = root / _STOMA_NAME
        if not stoma.is_file() or stoma.stat().st_size <= 0:
            return EMPTY_IDENTITY
    except OSError:
        return EMPTY_IDENTITY
    if _read_json(state_path) != state or not _ready_now(state_dir / _LOCK_DIR):
        return EMPTY_IDENTITY
    return PersonaIdentity(
        scientist_id,
        str(state.get("scientist_name") or "").strip(),
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _await_ready(lock_dir: Path) -> bool:
    """True once the stoma is committed (``ready`` present, no active ``writing``).

    Waits out an in-flight writer up to :data:`_READY_WAIT_S`, then requires the
    committed ``ready`` marker. Fail-open: a stuck writer simply yields ``False``
    so the turn proceeds on the base identity instead of blocking.
    """
    writing = lock_dir / _WRITING
    ready = lock_dir / _READY
    deadline = time.monotonic() + _READY_WAIT_S
    while writing.exists() and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    return ready.exists() and not writing.exists()


def _ready_now(lock_dir: Path) -> bool:
    return (lock_dir / _READY).exists() and not (lock_dir / _WRITING).exists()


def _matches_persona_hash(text: str, expected_hash: str) -> bool:
    encoded = text.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() == expected_hash:
        return True
    # SoulAgent releases before the joint state/stoma commit hashed the decoded
    # text before ``stoma_writer`` appended its canonical final newline. Keep
    # those already-active personas readable while all new writes hash the
    # exact persisted payload.
    if text.endswith("\n"):
        return hashlib.sha256(text[:-1].encode("utf-8")).hexdigest() == expected_hash
    return False


def _read_stoma(root: Path) -> str:
    """Read the ``omniscientist`` host stoma (``<root>/role.md``), or ``""``.

    Only ever the canonical project stoma, so a tampered ``stoma_paths`` entry in
    state can never redirect the read outside the project root.
    """
    try:
        return (root / _STOMA_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


# Where SoulAgent's distiller writes knowledge graphs by default
# (``<project-root>/scientist-kg/<id>/``); scanned only to surface a discovery hint.
_KG_DIR = "scientist-kg"
_KG_MARKERS = ("identity.json", "manifest.json")


@dataclass(frozen=True)
class PersonaStatus:
    """Read-only snapshot for the ``/soul`` command and the startup hint.

    Combines the active overlay (if any) with whether the project ships a
    ``scientist-kg/`` and which scientist ids look loadable, so the REPL can point
    users at SoulAgent without the CLI importing or mutating the skill.
    """

    overlay: PersonaOverlay
    scientist_kg_present: bool
    available: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.overlay.active


def load_base_role(*, role: str = "", role_file: Path | None = None) -> str:
    """Sticky OmniScientist identity; complete an interrupted persona unload first.

    ``role`` may be inline text or a path. Otherwise read ``role.md`` under the
    Omni home, restoring ``role.md.soulagent.bak`` when an unload left the
    backup behind. The orchestrator loads this once per process.
    """
    from omni.data import DEFAULT_ROLE

    cfg_role = (role or "").strip()
    if cfg_role:
        path = Path(cfg_role).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return cfg_role
    if role_file is not None:
        target = Path(role_file)
        _restore_from_backup(target.parent)
        if target.is_file():
            return target.read_text(encoding="utf-8")
    return DEFAULT_ROLE


def load_turn_persona_overlay(paths: Any, *, channel: str) -> str:
    """Rendered overlay for one ReAct turn, rooted at the opened folder."""
    from omni.personas.roots import persona_overlay_root

    return load_persona_overlay(persona_overlay_root(paths, channel=channel)).render()


def persona_status(working_dir: str | Path | None) -> PersonaStatus:
    """Active persona plus discoverable ``scientist-kg/`` personas for ``working_dir``.

    Purely read-only and fail-open — used only to make the feature discoverable,
    never to change any state.
    """
    overlay = load_persona_overlay(working_dir)
    present = False
    available: tuple[str, ...] = ()
    if working_dir:
        try:
            kg = Path(working_dir).resolve() / _KG_DIR
            if kg.is_dir():
                present = True
                available = tuple(
                    sorted(
                        child.name
                        for child in kg.iterdir()
                        if child.is_dir()
                        and any((child / marker).is_file() for marker in _KG_MARKERS)
                    )
                )
        except OSError:
            pass
    return PersonaStatus(overlay, present, available)
