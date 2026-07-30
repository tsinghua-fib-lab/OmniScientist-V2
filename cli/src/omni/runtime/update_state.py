"""Durable package-to-runtime convergence identity.

Python package managers can replace OmniScientist without knowing anything
about ``OMNI_HOME`` or the supervised Home Service.  The next ``omni`` launch
therefore needs a cheap, local way to answer a different question from
"is a newer release available?": does this home still need to converge onto the
package that is already installed?

The state intentionally stores only a hash of sanitized installation metadata
and dependency versions. A PEP 610 ``direct_url.json`` may contain a private
source URL, so credentials are removed before hashing and its raw value is never
copied into ``OMNI_HOME``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from omni.runtime.uninstall import installation_method_for_prefix

if TYPE_CHECKING:
    from omni.config.paths import OmniPaths

DIST = "omniscientist"
STATE_FILE = "update-state.json"
STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class InstallationFingerprint:
    """Non-secret identity of the package currently importing ``omni``."""

    token: str
    version: str
    owner: str
    source: str
    python: str

    def to_dict(self) -> dict[str, str]:
        # The absolute interpreter path participates in ``token`` so switching
        # environments still forces convergence, but it is not copied into
        # OMNI_HOME (where it could disclose a username or internal mount path).
        data = asdict(self)
        data.pop("python", None)
        return data


def _distribution_text(dist: md.Distribution, name: str) -> str:
    try:
        return dist.read_text(name) or ""
    except Exception:  # noqa: BLE001 - corrupt metadata means a different fingerprint.
        return ""


def _source_kind(direct_url: str) -> str:
    if not direct_url:
        return "pypi"
    try:
        payload = json.loads(direct_url)
    except json.JSONDecodeError:
        return "unknown"
    if payload.get("vcs_info"):
        return "git"
    url = str(payload.get("url") or "")
    if url.startswith("file://"):
        return "editable" if (payload.get("dir_info") or {}).get("editable") else "local"
    if payload.get("archive_info") is not None:
        return "archive"
    return "direct"


def _safe_direct_identity(direct_url: str) -> dict:
    """Return update-relevant PEP 610 fields with URL credentials removed."""
    if not direct_url:
        return {}
    try:
        payload = json.loads(direct_url)
    except json.JSONDecodeError:
        return {"invalid": True}
    if not isinstance(payload, dict):
        return {"invalid": True}

    raw_url = str(payload.get("url") or "")
    parsed = urlsplit(raw_url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        hostname = f"{hostname}:{port}"
    safe_url = urlunsplit(
        (parsed.scheme, hostname, parsed.path, "", "")
    )
    return {
        "url": safe_url,
        "vcs_info": payload.get("vcs_info") or {},
        "dir_info": payload.get("dir_info") or {},
        "archive_info": payload.get("archive_info") or {},
        "subdirectory": str(payload.get("subdirectory") or ""),
    }


def _environment_versions() -> list[tuple[str, str]]:
    """Stable dependency inventory for detecting package-manager-only changes."""
    versions: list[tuple[str, str]] = []
    for dist in md.distributions():
        try:
            name = str(dist.metadata.get("Name") or "").strip().lower()
            version = str(dist.version or "").strip()
        except Exception:  # noqa: BLE001 - one corrupt dependency must not break launch.
            continue
        if name:
            versions.append((name, version))
    return sorted(set(versions))


def current_fingerprint() -> InstallationFingerprint:
    """Fingerprint the active distribution without persisting source credentials."""
    try:
        dist = md.distribution(DIST)
        version = dist.version
        direct_url = _distribution_text(dist, "direct_url.json")
        record = _distribution_text(dist, "RECORD")
        metadata = _distribution_text(dist, "METADATA")
    except md.PackageNotFoundError:
        from omni import __version__

        version = __version__
        direct_url = ""
        record = ""
        metadata = ""

    owner = installation_method_for_prefix(Path(sys.prefix))
    python = str(Path(sys.executable).resolve())
    material = json.dumps(
        {
            "version": version,
            "owner": owner,
            "python": python,
            "direct": _safe_direct_identity(direct_url),
            "record": record,
            "metadata": metadata,
            # ``uv tool upgrade`` / ``pipx upgrade`` may update a dependency
            # without changing the Omni wheel. The Home Service must still
            # restart onto that environment, so dependency versions participate
            # in the hash while never being persisted in clear text.
            "environment": _environment_versions(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return InstallationFingerprint(
        token=hashlib.sha256(material).hexdigest(),
        version=version,
        owner=owner,
        source=_source_kind(direct_url),
        python=python,
    )


def state_path(paths: OmniPaths) -> Path:
    return paths.home / STATE_FILE


def read_state(paths: OmniPaths) -> dict:
    """Read convergence state, returning ``{}`` when missing or malformed."""
    try:
        payload = json.loads(state_path(paths).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(paths: OmniPaths, payload: dict) -> None:
    """Atomically persist non-secret update state with user-only permissions."""
    paths.home.mkdir(parents=True, exist_ok=True)
    target = state_path(paths)
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def record_converged(
    paths: OmniPaths,
    fingerprint: InstallationFingerprint | None = None,
) -> InstallationFingerprint:
    """Record that this ``OMNI_HOME`` has converged onto ``fingerprint``."""
    current = fingerprint or current_fingerprint()
    write_state(
        paths,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "converged_at": time.time(),
            "fingerprint": current.to_dict(),
        },
    )
    return current


def convergence_needed(
    paths: OmniPaths,
    fingerprint: InstallationFingerprint | None = None,
) -> bool:
    """Whether local runtime/service convergence is required."""
    current = fingerprint or current_fingerprint()
    state = read_state(paths)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        return True
    saved = state.get("fingerprint")
    return not isinstance(saved, dict) or saved.get("token") != current.token
