"""Trusted remote scientist-KG discovery and atomic installation.

The module is deliberately standard-library only so the portable SoulAgent
runner can use the same implementation as OmniScientist.  A remote package is
never exposed to the KG scanner until both the registry-level manifest hash and
the KG's own file hashes/structure have passed validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from kg_loader import KGValidationError, load_kg

DEFAULT_REGISTRY_URL = (
    "https://gitee.com/cvYaowenHu/scientist-kg-registry/"
    "raw/master/registry.json"
)
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 1000
_SCIENTIST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

FetchBytes = Callable[[str, int], bytes]


class RemoteRegistryError(RuntimeError):
    """The registry could not be reached or did not satisfy its contract."""


class RemoteScientistNotFound(RemoteRegistryError):
    """The registry is authoritative and has no unique matching scientist."""


class RemoteScientistAmbiguous(RemoteRegistryError):
    """A requested name maps to more than one remote scientist."""


class RemoteInstallError(RemoteRegistryError):
    """A matched remote package could not be safely installed."""


def _normal_name(value: str) -> str:
    return re.sub(r"[\s_.·•,'’\-]+", "", value.casefold())


def _safe_repo_path(value: Any, *, label: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RemoteRegistryError(f"远端注册表包含不安全的 {label}：{raw!r}")
    return path.as_posix()


def _raw_url(base: str, relative: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)
    return urljoin(base, encoded)


def _registry_base_url(registry_url: str) -> str:
    parsed = urlsplit(registry_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RemoteRegistryError("远端人格注册表必须使用 HTTPS")
    clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return clean.rsplit("/", 1)[0] + "/"


def _default_fetch_bytes(url: str, max_bytes: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json, application/octet-stream;q=0.9",
            "User-Agent": "SoulAgent/1.2 scientist-kg-installer",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise RemoteRegistryError(f"远端文件超过大小限制：{url}")
            payload = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise RemoteRegistryError(f"远端请求失败（HTTP {exc.code}）：{url}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise RemoteRegistryError(f"无法访问远端人格仓库：{url}: {exc}") from exc
    if len(payload) > max_bytes:
        raise RemoteRegistryError(f"远端文件超过大小限制：{url}")
    return payload


def _fetch(fetch_bytes: FetchBytes | None, url: str, max_bytes: int) -> bytes:
    return (fetch_bytes or _default_fetch_bytes)(url, max_bytes)


def fetch_registry(
    registry_url: str = DEFAULT_REGISTRY_URL,
    *,
    fetch_bytes: FetchBytes | None = None,
) -> dict[str, Any]:
    """Fetch and validate the public scientist registry."""
    _registry_base_url(registry_url)
    payload = _fetch(fetch_bytes, registry_url, MAX_REGISTRY_BYTES)
    try:
        registry = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteRegistryError(f"远端人格注册表不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("scientists"), list):
        raise RemoteRegistryError("远端人格注册表缺少 scientists 数组")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw_entry in registry["scientists"]:
        if not isinstance(raw_entry, dict):
            raise RemoteRegistryError("远端人格注册表包含非对象条目")
        scientist_id = str(raw_entry.get("scientist_id") or "").strip()
        if not _SCIENTIST_ID.fullmatch(scientist_id):
            raise RemoteRegistryError(f"远端 scientist_id 不安全：{scientist_id!r}")
        if scientist_id in seen:
            raise RemoteRegistryError(f"远端 scientist_id 重复：{scientist_id}")
        seen.add(scientist_id)
        scientist_name = str(raw_entry.get("scientist_name") or "").strip()
        if not scientist_name:
            raise RemoteRegistryError(f"远端科学家缺少规范名称：{scientist_id}")
        aliases = raw_entry.get("aliases") or []
        if not isinstance(aliases, list) or any(not isinstance(v, str) for v in aliases):
            raise RemoteRegistryError(f"远端 aliases 无效：{scientist_id}")
        manifest_sha256 = str(raw_entry.get("manifest_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise RemoteRegistryError(f"远端 manifest_sha256 无效：{scientist_id}")
        path = _safe_repo_path(raw_entry.get("path"), label="path")
        if PurePosixPath(path).name != scientist_id:
            raise RemoteRegistryError(
                f"远端人格目录与 scientist_id 不一致：{path} != {scientist_id}"
            )
        normalized.append(
            {
                "scientist_id": scientist_id,
                "scientist_name": scientist_name,
                "aliases": list(dict.fromkeys([scientist_name, *aliases])),
                "path": path,
                "manifest_sha256": manifest_sha256,
            }
        )
    return {
        "schema_version": str(registry.get("schema_version") or ""),
        "scientists": normalized,
    }


def resolve_registry_entry(
    registry: dict[str, Any],
    request: str,
    *,
    contains: bool = False,
) -> dict[str, Any]:
    """Resolve one registry entry by exact name or by a name present in intent text."""
    query = _normal_name(request)
    matches: list[dict[str, Any]] = []
    for entry in registry.get("scientists") or []:
        forms = {
            _normal_name(str(entry.get("scientist_id") or "")),
            _normal_name(str(entry.get("scientist_name") or "")),
            *(_normal_name(str(alias)) for alias in entry.get("aliases") or []),
        }
        forms.discard("")
        matched = any(form in query for form in forms) if contains else query in forms
        if matched:
            matches.append(entry)
    if not matches:
        raise RemoteScientistNotFound(f"远端人格仓库中没有找到：{request}")
    unique = {str(entry["scientist_id"]): entry for entry in matches}
    if len(unique) != 1:
        raise RemoteScientistAmbiguous(
            "远端科学家名称存在歧义：" + ", ".join(sorted(unique))
        )
    return next(iter(unique.values()))


def _parse_manifest(payload: bytes, entry: dict[str, Any]) -> dict[str, Any]:
    actual_hash = hashlib.sha256(payload).hexdigest()
    expected_hash = str(entry["manifest_sha256"])
    if actual_hash != expected_hash:
        raise RemoteInstallError(
            "远端 manifest 哈希不匹配："
            f"期望 {expected_hash}，实际 {actual_hash}"
        )
    try:
        manifest = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteInstallError(f"远端 manifest 不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise RemoteInstallError("远端 manifest 缺少 files 数组")
    if str(manifest.get("scientist_id") or "") != entry["scientist_id"]:
        raise RemoteInstallError("远端 manifest 的 scientist_id 与注册表不一致")
    if not 1 <= len(manifest["files"]) <= MAX_FILES:
        raise RemoteInstallError("远端 manifest 文件数量超出限制")
    return manifest


def install_registry_entry(
    kg_root: str | Path,
    entry: dict[str, Any],
    *,
    registry_url: str = DEFAULT_REGISTRY_URL,
    fetch_bytes: FetchBytes | None = None,
) -> dict[str, Any]:
    """Download, validate, and atomically expose one registry entry."""
    base_url = _registry_base_url(registry_url)
    root = Path(kg_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    scientist_id = str(entry["scientist_id"])
    if not _SCIENTIST_ID.fullmatch(scientist_id):
        raise RemoteInstallError(f"scientist_id 不安全：{scientist_id!r}")
    target = root / scientist_id
    if target.exists():
        raise RemoteInstallError(
            f"本地人格目录已存在，拒绝远端覆盖：{target}；请使用专用蒸馏器修复"
        )

    lock_path = root / f".install-{scientist_id}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RemoteInstallError(f"科学家人格正在由另一进程安装：{scientist_id}") from exc

    staging: Path | None = None
    try:
        os.close(lock_fd)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".scientist-kg-download-{scientist_id}-",
                dir=root.parent,
            )
        )
        entry_path = _safe_repo_path(entry["path"], label="path")
        manifest_relative = f"{entry_path}/manifest.json"
        manifest_payload = _fetch(
            fetch_bytes,
            _raw_url(base_url, manifest_relative),
            MAX_MANIFEST_BYTES,
        )
        manifest = _parse_manifest(manifest_payload, entry)
        (staging / "manifest.json").write_bytes(manifest_payload)

        total_bytes = len(manifest_payload)
        declared: set[str] = set()
        for raw_file in manifest["files"]:
            if not isinstance(raw_file, dict):
                raise RemoteInstallError("远端 manifest.files 包含非对象条目")
            relative = _safe_repo_path(raw_file.get("path"), label="manifest 文件路径")
            if relative in declared:
                raise RemoteInstallError(f"远端 manifest 文件路径重复：{relative}")
            declared.add(relative)
            expected = str(raw_file.get("sha256") or "").casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise RemoteInstallError(f"远端文件哈希无效：{relative}")
            payload = _fetch(
                fetch_bytes,
                _raw_url(base_url, f"{entry_path}/{relative}"),
                MAX_FILE_BYTES,
            )
            total_bytes += len(payload)
            if total_bytes > MAX_TOTAL_BYTES:
                raise RemoteInstallError("远端人格包总大小超出限制")
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise RemoteInstallError(
                    f"远端文件哈希不匹配：{relative}，期望 {expected}，实际 {actual}"
                )
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)

        try:
            kg = load_kg(staging)
        except KGValidationError as exc:
            raise RemoteInstallError(f"远端人格包结构校验失败：{exc}") from exc
        if str(kg["scientist_id"]) != scientist_id:
            raise RemoteInstallError("远端人格包 scientist_id 校验失败")
        if target.exists():
            raise RemoteInstallError(f"安装目标在下载期间已出现，拒绝覆盖：{target}")
        os.replace(staging, target)
        return {
            "downloaded": True,
            "scientist_id": scientist_id,
            "scientist_name": str(entry["scientist_name"]),
            "kg_path": str(target),
            "registry_url": registry_url,
            "manifest_sha256": str(entry["manifest_sha256"]),
        }
    except RemoteRegistryError:
        raise
    except OSError as exc:
        raise RemoteInstallError(f"远端人格安装失败：{exc}") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def resolve_and_install_remote_scientist(
    kg_root: str | Path,
    request: str,
    *,
    contains: bool = False,
    registry_url: str = DEFAULT_REGISTRY_URL,
    fetch_bytes: FetchBytes | None = None,
) -> dict[str, Any]:
    """Resolve a named remote scientist and install it into the scanner root."""
    registry = fetch_registry(registry_url, fetch_bytes=fetch_bytes)
    entry = resolve_registry_entry(registry, request, contains=contains)
    return install_registry_entry(
        kg_root,
        entry,
        registry_url=registry_url,
        fetch_bytes=fetch_bytes,
    )
