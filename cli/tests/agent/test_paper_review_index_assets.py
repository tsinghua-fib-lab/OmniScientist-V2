"""Offline contracts for Paper Review's first-use data checkout."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "paper-review"
INDEX_NAMES = ("iclr2026-reviews", "review-arena-preferences")


def _load_module(filename: str, name: str) -> Any:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _payload(name: str) -> bytes:
    return f"verified payload for {name}\n".encode()


def _write_test_indexes(root: Path, *, include_payloads: bool = True) -> None:
    for name in INDEX_NAMES:
        content = _payload(name)
        index_root = root / name
        index_root.mkdir(parents=True, exist_ok=True)
        header = {
            "active_generation": "gen-test",
            "artifacts": {
                "vectors.faiss": {
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            },
            "status": "ready",
        }
        (index_root / "index.json").write_text(
            json.dumps(header, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if include_payloads:
            generation = index_root / "generations" / "gen-test"
            generation.mkdir(parents=True)
            (generation / "vectors.faiss").write_bytes(content)


def test_complete_source_checkout_never_fetches_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_source_test")
    bundled = tmp_path / "bundled"
    _write_test_indexes(bundled)

    def unexpected_checkout(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a complete source checkout must not use the network")

    monkeypatch.setattr(module, "_checkout_repository", unexpected_checkout)
    result = module.ensure_data_indexes(
        bundled_indexes_root=bundled,
        cache_dir=tmp_path / "cache",
    )

    assert result.indexes_root == bundled
    assert result.source == "bundled"
    assert result.downloaded is False


def test_header_only_install_fetches_once_then_reuses_verified_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_cache_test")
    bundled = tmp_path / "bundled"
    _write_test_indexes(bundled, include_payloads=False)
    calls: list[tuple[str, str]] = []

    def fake_checkout(
        repository: str,
        revision: str,
        destination: Path,
        **_kwargs: Any,
    ) -> None:
        calls.append((repository, revision))
        _write_test_indexes(destination / "indexes")

    monkeypatch.setattr(module, "_checkout_repository", fake_checkout)
    first = module.ensure_data_indexes(
        bundled_indexes_root=bundled,
        cache_dir=tmp_path / "cache",
        repository="https://example.test/paper-review-data.git",
    )
    second = module.ensure_data_indexes(
        bundled_indexes_root=bundled,
        cache_dir=tmp_path / "cache",
        repository="https://example.test/paper-review-data.git",
    )

    assert len(calls) == 1
    assert first.source == "configured_repository_download"
    assert first.downloaded is True
    assert second.source == "git_cache"
    assert second.downloaded is False
    for name in INDEX_NAMES:
        assert (second.indexes_root / name / "generations" / "gen-test").is_dir()


def test_default_download_prefers_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_github_test")
    bundled = tmp_path / "bundled"
    _write_test_indexes(bundled, include_payloads=False)
    calls: list[str] = []

    def fake_checkout(
        repository: str,
        _revision: str,
        destination: Path,
        **_kwargs: Any,
    ) -> None:
        calls.append(repository)
        _write_test_indexes(destination / "indexes")

    monkeypatch.setattr(module, "_checkout_repository", fake_checkout)
    result = module.ensure_data_indexes(
        bundled_indexes_root=bundled,
        cache_dir=tmp_path / "cache",
    )

    assert calls == [module.GITHUB_DATA_REPOSITORY]
    assert result.source == "github_download"


def test_default_download_falls_back_to_gitee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_gitee_test")
    bundled = tmp_path / "bundled"
    _write_test_indexes(bundled, include_payloads=False)
    calls: list[str] = []
    progress: list[str] = []

    def fake_checkout(
        repository: str,
        _revision: str,
        destination: Path,
        **_kwargs: Any,
    ) -> None:
        calls.append(repository)
        if repository == module.GITHUB_DATA_REPOSITORY:
            raise module.DataBundleError("GitHub unavailable")
        _kwargs["progress_callback"]("Receiving objects: 100% (22/22), done.")
        _write_test_indexes(destination / "indexes")

    monkeypatch.setattr(module, "_checkout_repository", fake_checkout)
    result = module.ensure_data_indexes(
        bundled_indexes_root=bundled,
        cache_dir=tmp_path / "cache",
        progress_callback=progress.append,
    )

    assert calls == [
        module.GITHUB_DATA_REPOSITORY,
        module.GITEE_DATA_REPOSITORY,
    ]
    assert result.source == "gitee_download"
    combined = "\n".join(progress)
    assert module.GITHUB_DATA_REPOSITORY in combined
    assert "switching to Gitee mirror" in combined
    assert module.GITEE_DATA_REPOSITORY in combined
    assert "Receiving objects: 100%" in combined
    assert "verifying file sizes and SHA-256 checksums" in combined
    assert "verified and cached" in combined


def test_corrupt_download_is_rejected_before_cache_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_corrupt_test")
    bundled = tmp_path / "bundled"
    _write_test_indexes(bundled, include_payloads=False)

    def corrupt_checkout(
        _repository: str,
        _revision: str,
        destination: Path,
        **_kwargs: Any,
    ) -> None:
        _write_test_indexes(destination / "indexes")
        artifact = (
            destination
            / "indexes"
            / "iclr2026-reviews"
            / "generations"
            / "gen-test"
            / "vectors.faiss"
        )
        artifact.write_bytes(b"x" * artifact.stat().st_size)

    monkeypatch.setattr(module, "_checkout_repository", corrupt_checkout)
    with pytest.raises(module.DataBundleError, match="SHA-256"):
        module.ensure_data_indexes(
            bundled_indexes_root=bundled,
            cache_dir=tmp_path / "cache",
        )

    target = tmp_path / "cache" / "paper-review-data" / module.DATA_REVISION
    assert not target.exists()
    assert not list((tmp_path / "cache" / "paper-review-data").glob(".download-*"))


def test_git_timeout_terminates_transport_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_timeout_test")
    events: list[str] = []

    class _TimedOutProcess:
        pid = 12345
        returncode = -9

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            if timeout is not None:
                raise subprocess.TimeoutExpired(["git", "fetch"], timeout)
            events.append("reaped")
            return "", ""

    process = _TimedOutProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        module,
        "_terminate_process_tree",
        lambda received: events.append("terminated") if received is process else None,
    )

    with pytest.raises(module.DataBundleError, match="timed out"):
        module._run_git(["git", "fetch"], timeout_s=1)

    assert events == ["terminated", "reaped"]


def test_git_user_interrupt_terminates_transport_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_interrupt_test")
    events: list[str] = []

    class _InterruptedProcess:
        pid = 12345
        returncode = -9

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            if timeout is not None:
                raise KeyboardInterrupt
            events.append("reaped")
            return "", ""

    process = _InterruptedProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        module,
        "_terminate_process_tree",
        lambda received: events.append("terminated") if received is process else None,
    )

    with pytest.raises(KeyboardInterrupt):
        module._run_git(["git", "fetch"], timeout_s=1)

    assert events == ["terminated", "reaped"]


def test_git_fetch_progress_streams_carriage_return_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_progress_test")
    messages: list[str] = []
    (tmp_path / "received.pack").write_bytes(b"x" * 2048)
    monkeypatch.setattr(module, "GIT_PROGRESS_HEARTBEAT_SECONDS", 0.01)
    script = (
        "import sys, time; "
        "sys.stderr.write('Receiving objects: 10% (1/10), 1.00 MiB | 1.00 MiB/s\\r'); "
        "sys.stderr.flush(); time.sleep(0.08); "
        "sys.stderr.write('Receiving objects: 100% (10/10), 10.00 MiB | 2.00 MiB/s\\n'); "
        "sys.stdout.write('ok\\n')"
    )

    completed = module._run_git(
        [sys.executable, "-c", script],
        timeout_s=5,
        progress_callback=module._GitProgressRelay(messages.append),
        heartbeat_callback=messages.append,
        progress_size_path=tmp_path,
    )

    assert completed.stdout == "ok\n"
    assert any("Receiving objects: 10%" in message for message in messages)
    assert any("Receiving objects: 100%" in message for message in messages)
    assert any("2.0 KiB stored locally" in message for message in messages)
    assert all("elapsed" in message for message in messages)


def test_git_failure_surfaces_redacted_actionable_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_error_test")

    class _FailedProcess:
        pid = 12345
        returncode = 128

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            assert timeout == 30
            return "", (
                "error: RPC failed; curl 56 OpenSSL SSL_read: Connection reset\n"
                "fatal: unable to access 'https://private-token@example.test/data.git/'"
            )

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FailedProcess(),
    )

    with pytest.raises(module.DataBundleError) as captured:
        module._run_git(["git", "fetch"], timeout_s=30)

    message = str(captured.value)
    assert "fatal: unable to access" in message
    assert "<credentials>@example.test" in message
    assert "private-token" not in message


def test_repository_display_keeps_public_mirror_and_redacts_credentials() -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_url_test")

    assert (
        module._safe_repository_url(module.GITEE_DATA_REPOSITORY)
        == module.GITEE_DATA_REPOSITORY
    )
    assert module._safe_repository_url(
        "https://user:secret@example.test/data.git?token=also-secret"
    ) == "https://<credentials>@example.test/data.git"
    assert module._safe_repository_url(
        "private-token@example.test:data.git"
    ) == "<credentials>@example.test:data.git"


def test_git_cancellation_stops_streamed_process() -> None:
    module = _load_module("index_assets.py", "paper_review_index_assets_cancel_test")
    cancel_event = threading.Event()
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    started_at = time.monotonic()
    try:
        with pytest.raises(module.DataBundleCancelled):
            module._run_git(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_s=5,
                cancel_event=cancel_event,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started_at < 1


@pytest.mark.asyncio
async def test_engine_hydrates_only_enabled_default_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _load_module("engine.py", "paper_review_index_asset_engine_test")
    cached = tmp_path / "resolved" / "indexes"
    calls: list[Path] = []
    progress: list[tuple[str, float]] = []
    monkeypatch.setattr(engine._index_assets, "indexes_are_complete", lambda *_args: False)

    def fake_ensure_data_indexes(**kwargs: Any) -> Any:
        calls.append(Path(kwargs["cache_dir"]))
        kwargs["progress_callback"](
            "Paper Review retrieval data · Gitee mirror · Receiving objects: 10%"
        )
        kwargs["progress_callback"](
            "Paper Review retrieval data · Gitee mirror · Receiving objects: 20%"
        )
        return engine._index_assets.DataBundleResolution(
            indexes_root=cached,
            source="git_cache",
            downloaded=False,
        )

    monkeypatch.setattr(engine._index_assets, "ensure_data_indexes", fake_ensure_data_indexes)
    review, preference = await engine._hydrate_default_memory_indexes(
        {
            "enabled": True,
            "expected": True,
            "index_source": "bundled",
            "index_path": tmp_path / "header-only-review",
        },
        {
            "enabled": True,
            "expected": True,
            "index_source": "explicit",
            "index_path": tmp_path / "explicit-preference",
        },
        ctx=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path / "cache")),
        progress_callback=lambda message, fraction: progress.append((message, fraction)),
    )

    assert calls == [tmp_path / "cache"]
    assert review["index_path"] == cached / "iclr2026-reviews"
    assert review["index_source"] == "git_cache"
    assert preference["index_path"] == tmp_path / "explicit-preference"
    assert preference["index_source"] == "explicit"
    messages = [message for message, _fraction in progress]
    ten_percent = next(i for i, message in enumerate(messages) if "objects: 10%" in message)
    twenty_percent = next(
        i for i, message in enumerate(messages) if "objects: 20%" in message
    )
    assert ten_percent < twenty_percent
    assert all(fraction in {0.22, 0.23} for _message, fraction in progress)


@pytest.mark.asyncio
async def test_engine_cancellation_stops_data_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _load_module("engine.py", "paper_review_index_asset_cancel_engine_test")
    worker_stopped = threading.Event()
    monkeypatch.setattr(engine._index_assets, "indexes_are_complete", lambda *_args: False)

    def wait_for_cancellation(**kwargs: Any) -> Any:
        cancel_event = kwargs["cancel_event"]
        while not cancel_event.wait(0.01):
            pass
        worker_stopped.set()
        raise engine._index_assets.DataBundleCancelled("cancelled")

    monkeypatch.setattr(engine._index_assets, "ensure_data_indexes", wait_for_cancellation)
    task = asyncio.create_task(
        engine._hydrate_default_memory_indexes(
            {"enabled": True, "expected": True, "index_source": "bundled"},
            {"enabled": True, "expected": True, "index_source": "bundled"},
            ctx=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path / "cache")),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(worker_stopped.wait, 1)


@pytest.mark.asyncio
async def test_engine_does_not_download_when_both_layers_are_opted_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _load_module("engine.py", "paper_review_index_asset_opt_out_test")

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("disabled retrieval layers must not fetch data")

    monkeypatch.setattr(engine._index_assets, "ensure_data_indexes", unexpected)
    review, preference = await engine._hydrate_default_memory_indexes(
        {"enabled": False, "expected": False, "index_source": "bundled"},
        {"enabled": False, "expected": False, "index_source": "bundled"},
        ctx=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path / "cache")),
    )

    assert review["enabled"] is False
    assert preference["enabled"] is False
