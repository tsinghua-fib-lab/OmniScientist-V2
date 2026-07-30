"""Resolve Paper Review's large retrieval indexes without bundling them in pip.

Source checkouts already carry the indexes because Git users explicitly pull
the repository. Installed wheels carry only the small index headers; the first
Paper Review run checks out the pinned data-only repository into Omni's cache,
trying GitHub before the Gitee mirror, and verifies every declared artifact
before making it active.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DATA_REPOSITORY_ENV = "OMNI_PAPER_REVIEW_DATA_REPOSITORY"
GITHUB_DATA_REPOSITORY = (
    "https://github.com/foss12138/omniscientist-paper-review-data.git"
)
GITEE_DATA_REPOSITORY = (
    "https://gitee.com/yolo1213811/omniscientist-paper-review-data.git"
)
DEFAULT_DATA_REPOSITORIES = (GITHUB_DATA_REPOSITORY, GITEE_DATA_REPOSITORY)
DATA_REVISION = "96c73c4ff84cf817a364e160f6b113eb9bfa97b1"
INDEX_NAMES = ("iclr2026-reviews", "review-arena-preferences")
REPOSITORY_PROBE_TIMEOUT_SECONDS = 30
# Two bounded mirror attempts plus review synthesis must fit the skill's 20-minute
# execution budget. Eight minutes still covers the observed full Gitee checkout.
DATA_FETCH_TIMEOUT_SECONDS = 480
GIT_PROGRESS_HEARTBEAT_SECONDS = 15.0
DataProgressCallback = Callable[[str], None]

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_GIT_PROGRESS_RE = re.compile(
    r"^(?:remote:\s*)?"
    r"(?P<phase>Counting objects|Compressing objects|Receiving objects|Resolving deltas):\s*"
    r"(?P<percent>\d{1,3})%\s*(?P<detail>.*)$",
    re.IGNORECASE,
)
_GIT_ERROR_MARKERS = (
    "fatal:",
    "error:",
    "rpc failed",
    "remote error",
    "connection reset",
    "connection timed out",
    "tls",
    "ssl",
)


class DataBundleError(RuntimeError):
    """A safe, user-facing failure while resolving the optional data bundle."""


class DataBundleCancelled(DataBundleError):
    """The owning Paper Review task cancelled an in-progress Git operation."""


@dataclass(frozen=True)
class DataBundleResolution:
    """Location and provenance of one verified retrieval-data bundle."""

    indexes_root: Path
    source: str
    downloaded: bool
    revision: str = DATA_REVISION


@dataclass
class _GitProgressRelay:
    """Throttle Git's carriage-return progress into readable CLI events."""

    callback: DataProgressCallback
    started_at: float = field(default_factory=time.monotonic)
    last_percent_by_phase: dict[str, int] = field(default_factory=dict)

    def __call__(self, raw_line: str) -> None:
        line = _clean_git_output(raw_line)
        match = _GIT_PROGRESS_RE.match(line)
        if match is None:
            return
        phase = match.group("phase")
        percent = min(100, int(match.group("percent")))
        previous = self.last_percent_by_phase.get(phase.casefold())
        if previous == percent:
            return
        if previous is not None and percent != 100 and percent < previous + 10:
            return
        self.last_percent_by_phase[phase.casefold()] = percent
        detail = match.group("detail").strip().rstrip(",")
        suffix = f" {detail}" if detail else ""
        _notify_progress(
            self.callback,
            f"{phase}: {percent}%{suffix} · elapsed {_format_elapsed(self.started_at)}",
        )


@dataclass
class _GitStreamCapture:
    """Own the reader threads used for one streamed Git subprocess."""

    stdout_parts: list[str]
    stderr_parts: list[str]
    threads: tuple[threading.Thread, threading.Thread]

    def finish(self) -> tuple[str, str]:
        for thread in self.threads:
            thread.join()
        return "".join(self.stdout_parts), "".join(self.stderr_parts)


def indexes_are_complete(
    indexes_root: str | Path,
    *,
    expected_indexes_root: str | Path | None = None,
    verify_hashes: bool = False,
) -> bool:
    """Return whether both immutable indexes match their shipped headers."""

    root = Path(indexes_root)
    expected = Path(expected_indexes_root) if expected_indexes_root else root
    try:
        _validate_indexes(
            root,
            expected_indexes_root=expected,
            verify_hashes=verify_hashes,
        )
    except (DataBundleError, OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def ensure_data_indexes(
    *,
    bundled_indexes_root: str | Path,
    cache_dir: str | Path,
    repository: str | None = None,
    progress_callback: DataProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> DataBundleResolution:
    """Return bundled/cache indexes, cloning and verifying them when necessary."""

    bundled = Path(bundled_indexes_root)
    if indexes_are_complete(bundled):
        return DataBundleResolution(
            indexes_root=bundled,
            source="bundled",
            downloaded=False,
        )

    cache_root = Path(cache_dir).expanduser() / "paper-review-data"
    target = cache_root / DATA_REVISION
    cached_indexes = target / "indexes"
    if indexes_are_complete(
        cached_indexes,
        expected_indexes_root=bundled,
    ):
        _notify_progress(
            progress_callback,
            f"Paper Review retrieval data · using verified cache: {target}",
        )
        return DataBundleResolution(
            indexes_root=cached_indexes,
            source="git_cache",
            downloaded=False,
        )

    cache_root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _remove_owned_cache_target(target, cache_root)

    download_root = Path(tempfile.mkdtemp(prefix=".download-", dir=cache_root))
    repositories = _candidate_repositories(repository)
    last_error: Exception | None = None
    _notify_progress(
        progress_callback,
        f"Paper Review retrieval data · verified cache destination: {target}",
    )
    try:
        for position, selected_repository in enumerate(repositories):
            checkout = download_root / f"source-{position}"
            checkout.mkdir()
            source_name = _repository_name(selected_repository)
            display_repository = _safe_repository_url(selected_repository)
            source_started_at = time.monotonic()
            _notify_progress(
                progress_callback,
                (
                    f"Paper Review retrieval data · source {position + 1}/"
                    f"{len(repositories)} · {source_name}: {display_repository}"
                ),
            )

            def source_progress(message: str, *, _source_name: str = source_name) -> None:
                _notify_progress(
                    progress_callback,
                    f"Paper Review retrieval data · {_source_name} · {message}",
                )

            try:
                _checkout_repository(
                    selected_repository,
                    DATA_REVISION,
                    checkout,
                    progress_callback=source_progress,
                    cancel_event=cancel_event,
                )
                _notify_progress(
                    progress_callback,
                    (
                        f"Paper Review retrieval data · {source_name} download complete; "
                        "verifying file sizes and SHA-256 checksums"
                    ),
                )
                _validate_indexes(
                    checkout / "indexes",
                    expected_indexes_root=bundled,
                    verify_hashes=True,
                )
            except DataBundleCancelled:
                raise
            except (DataBundleError, OSError, ValueError) as exc:
                last_error = exc
                failure = (
                    f"Paper Review retrieval data · {source_name} failed after "
                    f"{_format_elapsed(source_started_at)}: {exc}"
                )
                if position + 1 < len(repositories):
                    next_repository = repositories[position + 1]
                    failure += (
                        f"; switching to {_repository_name(next_repository)}: "
                        f"{_safe_repository_url(next_repository)}"
                    )
                _notify_progress(progress_callback, failure)
                continue

            git_metadata = checkout / ".git"
            if git_metadata.is_dir():
                shutil.rmtree(git_metadata)

            try:
                checkout.rename(target)
            except FileExistsError:
                # Another Paper Review process may have completed the same download.
                if not indexes_are_complete(
                    target / "indexes",
                    expected_indexes_root=bundled,
                ):
                    raise DataBundleError(
                        "another process created an incomplete Paper Review data cache"
                    ) from None
                return DataBundleResolution(
                    indexes_root=target / "indexes",
                    source="git_cache",
                    downloaded=False,
                )

            _notify_progress(
                progress_callback,
                (
                    f"Paper Review retrieval data · {source_name} verified and cached: "
                    f"{target}"
                ),
            )
            return DataBundleResolution(
                indexes_root=target / "indexes",
                source=_download_source(selected_repository),
                downloaded=True,
            )
    finally:
        shutil.rmtree(download_root, ignore_errors=True)

    attempted = "GitHub, then Gitee" if len(repositories) > 1 else "the configured repository"
    detail = f" Last error: {last_error}" if last_error else ""
    raise DataBundleError(
        f"Paper Review data could not be fetched and verified from {attempted}.{detail}"
    )


def _candidate_repositories(repository: str | None) -> tuple[str, ...]:
    """Return an explicit override or the ordered public mirror list."""

    configured = (repository or os.environ.get(DATA_REPOSITORY_ENV, "")).strip()
    return (configured,) if configured else DEFAULT_DATA_REPOSITORIES


def _download_source(repository: str) -> str:
    if repository == GITHUB_DATA_REPOSITORY:
        return "github_download"
    if repository == GITEE_DATA_REPOSITORY:
        return "gitee_download"
    return "configured_repository_download"


def _repository_name(repository: str) -> str:
    if repository == GITHUB_DATA_REPOSITORY:
        return "GitHub primary"
    if repository == GITEE_DATA_REPOSITORY:
        return "Gitee mirror"
    return "configured repository"


def _checkout_repository(
    repository: str,
    revision: str,
    destination: Path,
    *,
    progress_callback: DataProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Fetch exactly one advertised data commit without invoking a shell."""

    git = shutil.which("git")
    if git is None:
        raise DataBundleError(
            "Git is not installed; install Git or supply explicit Paper Review indexes"
        )

    advertised = _run_git(
        [git, "ls-remote", "--heads", repository],
        timeout_s=REPOSITORY_PROBE_TIMEOUT_SECONDS,
        cancel_event=cancel_event,
    )
    advertised_revisions = {
        line.split(maxsplit=1)[0].casefold()
        for line in advertised.stdout.splitlines()
        if line.strip()
    }
    if revision.casefold() not in advertised_revisions:
        raise DataBundleError("the data repository does not advertise the pinned revision")

    _notify_progress(
        progress_callback,
        f"pinned revision {revision[:12]} is available; starting shallow Git fetch",
    )

    _run_git(
        [git, "init", "--quiet", str(destination)],
        timeout_s=60,
        cancel_event=cancel_event,
    )
    _run_git(
        [git, "-C", str(destination), "remote", "add", "origin", repository],
        timeout_s=60,
        cancel_event=cancel_event,
    )
    _run_git(
        [
            git,
            "-C",
            str(destination),
            "-c",
            "advice.detachedHead=false",
            "fetch",
            "--progress",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            revision,
        ],
        timeout_s=DATA_FETCH_TIMEOUT_SECONDS,
        progress_callback=(
            _GitProgressRelay(progress_callback)
            if progress_callback is not None
            else None
        ),
        heartbeat_callback=progress_callback,
        progress_size_path=destination / ".git" / "objects",
        cancel_event=cancel_event,
    )
    _run_git(
        [git, "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        timeout_s=120,
        cancel_event=cancel_event,
    )
    completed = _run_git(
        [git, "-C", str(destination), "rev-parse", "HEAD"],
        timeout_s=60,
        cancel_event=cancel_event,
    )
    if completed.stdout.strip().casefold() != revision.casefold():
        raise DataBundleError("the data repository returned an unexpected revision")


def _run_git(
    arguments: list[str],
    *,
    timeout_s: int,
    progress_callback: DataProgressCallback | None = None,
    heartbeat_callback: DataProgressCallback | None = None,
    progress_size_path: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except OSError as exc:
        raise DataBundleError(
            "Git could not fetch the Paper Review data; check repository access and "
            "rerun Paper Review"
        ) from exc

    stream_capture: _GitStreamCapture | None = None
    try:
        if progress_callback is None and cancel_event is None:
            stdout, stderr = process.communicate(timeout=timeout_s)
        else:
            stream_capture = _start_git_stream_capture(
                process,
                progress_callback or (lambda _message: None),
            )
            _wait_for_git_process(
                process,
                timeout_s=timeout_s,
                heartbeat_callback=heartbeat_callback,
                progress_size_path=progress_size_path,
                cancel_event=cancel_event,
            )
            stdout, stderr = stream_capture.finish()
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        if stream_capture is None:
            process.communicate()
        else:
            process.wait()
            stream_capture.finish()
        raise DataBundleError(
            f"the Git data download timed out after {timeout_s}s; check the network "
            "and rerun Paper Review"
        ) from exc
    except BaseException:
        _terminate_process_tree(process)
        if stream_capture is None:
            process.communicate()
        else:
            process.wait()
            stream_capture.finish()
        raise
    if process.returncode:
        error = subprocess.CalledProcessError(
            process.returncode,
            arguments,
            output=stdout,
            stderr=stderr,
        )
        detail = _git_error_detail(stderr)
        detail_suffix = f" Git reported: {detail}" if detail else ""
        raise DataBundleError(
            "Git could not fetch the Paper Review data; check repository access and "
            f"rerun Paper Review.{detail_suffix}"
        ) from error
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _wait_for_git_process(
    process: subprocess.Popen[str],
    *,
    timeout_s: int,
    heartbeat_callback: DataProgressCallback | None,
    progress_size_path: Path | None,
    cancel_event: threading.Event | None,
) -> None:
    """Wait with periodic byte/elapsed heartbeats when Git itself is silent."""

    started_at = time.monotonic()
    deadline = started_at + timeout_s
    next_heartbeat = started_at + GIT_PROGRESS_HEARTBEAT_SECONDS
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DataBundleCancelled("the Paper Review data download was cancelled")
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_s)
        wait_seconds = min(0.5, remaining, max(0.01, next_heartbeat - now))
        try:
            process.wait(timeout=wait_seconds)
            return
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if heartbeat_callback is None or now < next_heartbeat:
                continue
            size = _directory_size(progress_size_path)
            size_text = (
                f" · {_format_bytes(size)} stored locally as Git objects"
                if size is not None
                else ""
            )
            _notify_progress(
                heartbeat_callback,
                f"download still running{size_text} · elapsed {_format_elapsed(started_at)}",
            )
            next_heartbeat = now + GIT_PROGRESS_HEARTBEAT_SECONDS


def _start_git_stream_capture(
    process: subprocess.Popen[str],
    progress_callback: DataProgressCallback,
) -> _GitStreamCapture:
    """Drain both pipes while relaying Git's carriage-return progress frames."""

    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Git progress capture requires stdout and stderr pipes")
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_thread = threading.Thread(
        target=_drain_text_stream,
        args=(process.stdout, stdout_parts),
        daemon=True,
        name="paper-review-git-stdout",
    )
    stderr_thread = threading.Thread(
        target=_drain_git_progress_stream,
        args=(process.stderr, stderr_parts, progress_callback),
        daemon=True,
        name="paper-review-git-stderr",
    )
    stdout_thread.start()
    stderr_thread.start()
    return _GitStreamCapture(
        stdout_parts=stdout_parts,
        stderr_parts=stderr_parts,
        threads=(stdout_thread, stderr_thread),
    )


def _drain_text_stream(stream: Any, output: list[str]) -> None:
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        output.append(chunk)


def _drain_git_progress_stream(
    stream: Any,
    output: list[str],
    progress_callback: DataProgressCallback,
) -> None:
    frame: list[str] = []
    while True:
        character = stream.read(1)
        if not character:
            if frame:
                _notify_progress(progress_callback, "".join(frame))
            return
        output.append(character)
        if character in {"\r", "\n"}:
            if frame:
                _notify_progress(progress_callback, "".join(frame))
                frame.clear()
            continue
        frame.append(character)


def _notify_progress(
    callback: DataProgressCallback | None,
    message: str,
) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:  # noqa: BLE001 - presentation must never break the download
        return


def _clean_git_output(value: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", value).replace("\x00", "").strip()
    return re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _git_error_detail(stderr: str) -> str:
    frames = [
        _clean_git_output(frame)
        for frame in re.split(r"[\r\n]+", stderr)
        if _clean_git_output(frame)
    ]
    actionable = [
        frame
        for frame in frames
        if any(marker in frame.casefold() for marker in _GIT_ERROR_MARKERS)
    ]
    if not actionable:
        return ""
    return _redact_url_credentials(actionable[-1])[:500]


def _safe_repository_url(repository: str) -> str:
    try:
        parsed = urlsplit(repository)
        if not parsed.scheme or not parsed.netloc:
            if re.match(r"^[^/@:\s]+@[^/:\s]+:", repository):
                return re.sub(r"^[^@\s]+@", "<credentials>@", repository)
            return repository
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return "configured repository URL"
    if parsed.username is not None or parsed.password is not None:
        host = f"<credentials>@{host}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _redact_url_credentials(value: str) -> str:
    return re.sub(
        r"(https?://)[^\s/@]+@",
        r"\1<credentials>@",
        value,
        flags=re.IGNORECASE,
    )


def _format_elapsed(started_at: float) -> str:
    total_seconds = max(0, int(time.monotonic() - started_at))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _directory_size(path: Path | None) -> int | None:
    if path is None:
        return None
    total = 0
    try:
        for candidate in path.rglob("*"):
            if candidate.is_file():
                total += candidate.stat().st_size
    except OSError:
        return None
    return total


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop a timed-out or interrupted Git process and all of its helpers."""

    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _validate_indexes(
    indexes_root: Path,
    *,
    expected_indexes_root: Path,
    verify_hashes: bool,
) -> None:
    for name in INDEX_NAMES:
        _validate_index(
            indexes_root / name,
            expected_header_path=expected_indexes_root / name / "index.json",
            verify_hashes=verify_hashes,
        )


def _validate_index(
    index_root: Path,
    *,
    expected_header_path: Path,
    verify_hashes: bool,
) -> None:
    expected = _read_object(expected_header_path, "shipped index header")
    actual = _read_object(index_root / "index.json", "downloaded index header")
    if actual != expected:
        raise DataBundleError("a downloaded index header does not match this Omni version")

    generation_name = _safe_component(actual.get("active_generation"), "generation")
    artifacts = actual.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise DataBundleError("an index header has no artifact manifest")

    generation = index_root / "generations" / generation_name
    _reject_symlink(index_root)
    _reject_symlink(index_root / "generations")
    _reject_symlink(generation)
    for raw_name, raw_metadata in artifacts.items():
        artifact_name = _safe_component(raw_name, "artifact")
        if not isinstance(raw_metadata, dict):
            raise DataBundleError("an index artifact manifest is malformed")
        try:
            expected_bytes = int(raw_metadata["bytes"])
            expected_sha256 = str(raw_metadata["sha256"]).casefold()
        except (KeyError, TypeError, ValueError) as exc:
            raise DataBundleError("an index artifact manifest is malformed") from exc
        if expected_bytes < 0 or len(expected_sha256) != 64:
            raise DataBundleError("an index artifact manifest is malformed")

        artifact = generation / artifact_name
        _reject_symlink(artifact)
        if not artifact.is_file() or artifact.stat().st_size != expected_bytes:
            raise DataBundleError(f"the {artifact_name} index artifact is incomplete")
        if verify_hashes and _sha256(artifact) != expected_sha256:
            raise DataBundleError(f"the {artifact_name} index artifact failed SHA-256 verification")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DataBundleError(f"the {label} is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DataBundleError(f"the {label} is malformed")
    return value


def _safe_component(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or Path(text).name != text:
        raise DataBundleError(f"the index {label} name is unsafe")
    return text


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise DataBundleError("the Paper Review data bundle contains a symbolic link")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_owned_cache_target(target: Path, cache_root: Path) -> None:
    if target.parent != cache_root or target.name != DATA_REVISION:
        raise DataBundleError("refusing to replace an unexpected cache path")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
