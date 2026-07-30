from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


class StomaError(RuntimeError):
    pass


HOST_STOMA = {
    "workbuddy": ["soul.md"],
    "claude": ["claude.md"],
    "codex": ["agent.md"],
    "omniscientist": ["role.md"],
}
BACKUP_SUFFIX = ".soulagent.bak"


def _stoma_names(host: str) -> tuple[str, ...]:
    names = HOST_STOMA.get(host)
    if names is None:
        raise StomaError(
            f"不支持的 Host：{host}；可选值为 {', '.join(HOST_STOMA)}"
        )
    return tuple(names)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _metadata_path(project_root: Path) -> Path:
    return project_root / ".soulagent" / "originals.json"


def _load_metadata(project_root: Path) -> dict[str, Any] | None:
    path = _metadata_path(project_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StomaError(f"原始造口元数据损坏：{path}: {exc}") from exc


def _save_metadata(project_root: Path, metadata: dict[str, Any]) -> None:
    _atomic_write(
        _metadata_path(project_root),
        json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _ensure_backups(
    project_root: Path, stoma_names: tuple[str, ...]
) -> dict[str, Any]:
    metadata = _load_metadata(project_root)
    if metadata is None:
        metadata = {"version": 1, "files": {}}
    else:
        for name, info in metadata.get("files", {}).items():
            if info.get("existed") and not (
                project_root / f"{name}{BACKUP_SUFFIX}"
            ).is_file():
                raise StomaError(f"备份记录存在但备份文件缺失：{name}")

    files = metadata.setdefault("files", {})
    created_backups: list[Path] = []
    added_names: list[str] = []
    try:
        for name in stoma_names:
            if name in files:
                continue
            source = project_root / name
            backup = project_root / f"{name}{BACKUP_SUFFIX}"
            existed = source.is_file()
            if backup.exists():
                raise StomaError(f"发现无元数据的孤立备份：{backup}")
            if existed:
                shutil.copy2(source, backup)
                created_backups.append(backup)
            files[name] = {"existed": existed}
            added_names.append(name)
        if added_names:
            _save_metadata(project_root, metadata)
    except Exception:
        for backup in created_backups:
            if backup.exists():
                backup.unlink()
        for name in added_names:
            files.pop(name, None)
        raise
    return metadata


def _restore_originals(
    project_root: Path,
    metadata: dict[str, Any],
    stoma_names: tuple[str, ...],
    delete_backups: bool,
) -> None:
    for name in stoma_names:
        backup = project_root / f"{name}{BACKUP_SUFFIX}"
        info = metadata.get("files", {}).get(name)
        if info is None:
            raise StomaError(f"原始造口元数据缺少条目：{name}")
        if info.get("existed") and not backup.is_file():
            raise StomaError(f"无法恢复，备份不存在：{backup}")

    for name in stoma_names:
        target = project_root / name
        backup = project_root / f"{name}{BACKUP_SUFFIX}"
        info = metadata.get("files", {}).get(name)
        if info.get("existed"):
            _atomic_write(target, backup.read_bytes())
        elif target.exists():
            target.unlink()
    if delete_backups:
        backup_contents: dict[Path, bytes] = {}
        for name in stoma_names:
            backup = project_root / f"{name}{BACKUP_SUFFIX}"
            if backup.exists():
                backup_contents[backup] = backup.read_bytes()
                backup.unlink()
        files = metadata.get("files", {})
        removed = {name: files.pop(name) for name in stoma_names}
        try:
            metadata_path = _metadata_path(project_root)
            if files:
                _save_metadata(project_root, metadata)
            elif metadata_path.exists():
                metadata_path.unlink()
        except Exception:
            files.update(removed)
            for backup, content in backup_contents.items():
                _atomic_write(backup, content)
            raise


class _WritingLock:
    def __init__(self, project_root: Path, timeout: float = 30.0):
        self.lock_dir = project_root / ".soulagent" / "lock"
        self.writing = self.lock_dir / "writing"
        self.ready = self.lock_dir / "ready"
        self.timeout = timeout
        self.had_ready = False

    def acquire(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(
                    self.writing, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.close(descriptor)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise StomaError("等待造口 writing 锁超时")
                time.sleep(0.05)
        self.had_ready = self.ready.exists()
        if self.ready.exists():
            try:
                self.ready.unlink()
            except OSError:
                pass

    def release(self, ready: bool) -> None:
        if self.writing.exists():
            try:
                self.writing.unlink()
            except OSError:
                pass
        if ready:
            _atomic_write(self.ready, b"ready\n")
        elif self.ready.exists():
            try:
                self.ready.unlink()
            except OSError:
                pass


def write_persona(
    project_root: str | Path,
    persona_text: str,
    host: str,
    *,
    switching_scientist: bool = False,
    lock_timeout: float = 30.0,
) -> dict[str, str]:
    root = Path(project_root).resolve()
    stoma_names = _stoma_names(host)
    root.mkdir(parents=True, exist_ok=True)
    lock = _WritingLock(root, timeout=lock_timeout)
    lock.acquire()
    rollback: dict[str, bytes | None] = {
        name: (root / name).read_bytes() if (root / name).is_file() else None
        for name in stoma_names
    }
    try:
        metadata = _ensure_backups(root, stoma_names)
        if switching_scientist:
            _restore_originals(
                root, metadata, stoma_names, delete_backups=False
            )
        rollback = {
            name: (root / name).read_bytes() if (root / name).is_file() else None
            for name in stoma_names
        }
        payload = (persona_text.rstrip() + "\n").encode("utf-8")
        for name in stoma_names:
            _atomic_write(root / name, payload)
    except Exception as exc:
        for name, content in rollback.items():
            target = root / name
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                _atomic_write(target, content)
        lock.release(ready=lock.had_ready)
        if isinstance(exc, StomaError):
            raise
        raise StomaError(f"造口写入失败，已恢复上一个版本：{exc}") from exc
    lock.release(ready=True)
    return {name: str(root / name) for name in stoma_names}


def unload_persona(
    project_root: str | Path, host: str, *, lock_timeout: float = 30.0
) -> None:
    root = Path(project_root).resolve()
    stoma_names = _stoma_names(host)
    metadata = _load_metadata(root)
    if metadata is None:
        raise StomaError("没有可卸载的 SoulAgent 人格：未找到原始造口备份元数据")
    lock = _WritingLock(root, timeout=lock_timeout)
    lock.acquire()
    previous = {
        name: (root / name).read_bytes() if (root / name).is_file() else None
        for name in stoma_names
    }
    try:
        _restore_originals(
            root, metadata, stoma_names, delete_backups=True
        )
    except Exception as exc:
        for name, content in previous.items():
            target = root / name
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                _atomic_write(target, content)
        lock.release(ready=lock.had_ready)
        if isinstance(exc, StomaError):
            raise
        raise StomaError(f"卸载失败，已恢复卸载前造口：{exc}") from exc
    lock.release(ready=False)
    try:
        lock.lock_dir.rmdir()
    except OSError:
        pass
