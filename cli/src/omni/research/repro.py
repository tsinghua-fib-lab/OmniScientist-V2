"""Reproducible artifact packaging (P1-H).

Bundles a produced artifact with *everything needed to re-derive it*: the exact
creation command/code, an environment fingerprint (Python + key package
versions), an optional git commit, declared inputs/seed/metrics, and a content
hash of the artifact itself. The bundle is a single ``.zip`` stored back into the
artifact store (``kind="bundle"``) and mirrored into the experiment-run ledger so
a report can cite ``(bundle <id>)`` and a reviewer can verify byte-for-byte.

Pure-stdlib and fully offline: ``zipfile`` + ``hashlib`` + a best-effort
``git rev-parse`` that degrades to ``""`` when git or a repo is absent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BUNDLE_SCHEMA = "omni.repro_bundle/v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_commit(cwd: Path) -> str:
    """Short HEAD commit of ``cwd``'s repo, or ``""`` (no git / not a repo)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def build_manifest(
    *,
    title: str,
    artifact_name: str,
    artifact_uri: str,
    artifact_sha256: str,
    artifact_size: int,
    artifact_mime: str,
    command: str,
    code_file: str,
    seed: int | None,
    inputs: dict[str, Any],
    metrics: dict[str, Any],
    env_lock: str,
    git_rev: str,
) -> dict[str, Any]:
    """Assemble the bundle manifest (the reproducibility contract)."""
    return {
        "schema": BUNDLE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "title": title,
        "artifact": {
            "uri": artifact_uri,
            "filename": artifact_name,
            "sha256": artifact_sha256,
            "size_bytes": artifact_size,
            "mime": artifact_mime,
        },
        "creation": {
            "command": command,
            "code_file": code_file,
            "seed": seed,
            "inputs": inputs,
        },
        "environment": {
            "env_lock": env_lock,
            "git_commit": git_rev,
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "metrics": metrics,
    }


def _readme(manifest: dict[str, Any]) -> str:
    art = manifest["artifact"]
    creation = manifest["creation"]
    env = manifest["environment"]
    steps = creation.get("command") or (
        f"python {creation['code_file']}" if creation.get("code_file") else "(no creation command supplied)"
    )
    lines = [
        f"# Reproducible artifact bundle: {manifest.get('title') or art['filename']}",
        "",
        f"- Artifact: `{art['filename']}` (sha256 `{art['sha256'][:16]}…`, {art['size_bytes']} bytes)",
        f"- Created: {manifest['created_at']}",
        f"- Platform: {env['platform']} / Python {env['python']}",
        f"- git commit：`{env['git_commit'] or '—'}`",
        "",
        "## Reproduction steps",
        "",
        "```bash",
        steps,
        "```",
        "",
        "## Environment fingerprint (env_lock)",
        "",
        "```",
        env["env_lock"],
        "```",
        "",
        "Validation: after rerunning, `shasum -a 256` should match the sha256 above.",
    ]
    if creation.get("seed") is not None:
        lines.insert(6, f"- Random seed: {creation['seed']}")
    return "\n".join(lines) + "\n"


def assemble_bundle_zip(
    dest_zip: Path,
    *,
    artifact_path: Path,
    artifact_name: str,
    manifest: dict[str, Any],
    code: str,
    command: str,
) -> None:
    """Write the bundle zip: artifact + MANIFEST.json + README.md + code/cmd."""
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(artifact_path, arcname=f"artifact/{artifact_name}")
        zf.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("README.md", _readme(manifest))
        if code:
            zf.writestr(manifest["creation"]["code_file"] or "code.txt", code)
        elif command:
            zf.writestr("command.sh", f"#!/usr/bin/env bash\nset -euo pipefail\n{command}\n")


async def build_repro_bundle(
    *,
    artifacts: Any,
    store: Any,
    paths: Any,
    artifact_uri: str,
    title: str = "",
    command: str = "",
    code: str = "",
    code_filename: str = "",
    seed: int | None = None,
    inputs: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    env_lock: str | None = None,
    session_id: str = "",
    task_id: str = "",
    subtask_id: str = "",
) -> dict[str, Any]:
    """Package ``artifact_uri`` into a reproducible ``.zip`` bundle artifact.

    Returns ``{status, bundle_uri, artifact_sha256, run_id, manifest}`` or an
    ``{status: "error"}`` payload when the source artifact can't be resolved.
    """
    src = await artifacts.resolve_path(artifact_uri) if artifacts is not None else None
    if src is None or not Path(src).exists():
        return {"status": "error", "error": f"artifact not found: {artifact_uri}"}
    src = Path(src)

    if env_lock is None:
        from omni.research.tools import capture_env_lock

        env_lock = capture_env_lock()
    inputs = inputs or {}
    metrics = metrics or {}
    code_file = code_filename or ("code.py" if code else "")

    row = await artifacts.get(artifact_uri) if artifacts is not None else None
    artifact_mime = getattr(row, "mime", "") or "application/octet-stream"
    artifact_sha = sha256_file(src)
    manifest = build_manifest(
        title=title or src.name,
        artifact_name=src.name,
        artifact_uri=artifact_uri,
        artifact_sha256=artifact_sha,
        artifact_size=src.stat().st_size,
        artifact_mime=artifact_mime,
        command=command,
        code_file=code_file,
        seed=seed,
        inputs=inputs,
        metrics=metrics,
        env_lock=env_lock,
        git_rev=git_commit(getattr(paths, "workspace_root", None) or getattr(paths, "project_dir", Path.cwd())),
    )

    tmp = Path(tempfile.mkdtemp(prefix="omni-repro-"))
    zip_path = tmp / (f"{Path(src.name).stem}-bundle.zip")
    try:
        assemble_bundle_zip(
            zip_path, artifact_path=src, artifact_name=src.name,
            manifest=manifest, code=code, command=command,
        )
        stored = await artifacts.put_file(
            zip_path, kind="bundle", title=f"repro:{title or src.name}",
            mime="application/zip", session_id=session_id, task_id=task_id,
            subtask_id=subtask_id,
            meta={
                "schema": BUNDLE_SCHEMA,
                "source_uri": artifact_uri,
                "artifact_sha256": artifact_sha,
                "git_commit": manifest["environment"]["git_commit"],
            },
            copy=False,
        )
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp, ignore_errors=True)

    run_id = ""
    if store is not None:
        try:
            run = await store.add_run(
                title=f"repro-bundle: {title or src.name}", session_id=session_id,
                cmd=command, code_uri=stored.uri, seed=seed,
                env_lock=env_lock, inputs=inputs,
                output_uris=[artifact_uri, stored.uri], metrics=metrics,
                status="recorded",
            )
            run_id = run.id
        except Exception:  # noqa: BLE001 — ledger mirror is best-effort
            run_id = ""

    return {
        "status": "ok",
        "bundle_uri": stored.uri,
        "artifact_sha256": artifact_sha,
        "run_id": run_id,
        "manifest": manifest,
    }


__all__ = [
    "BUNDLE_SCHEMA",
    "build_repro_bundle",
    "build_manifest",
    "assemble_bundle_zip",
    "git_commit",
    "sha256_file",
    "sha256_bytes",
]
