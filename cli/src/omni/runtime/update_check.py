"""Update-notifier: detect a newer omni release without slowing startup.

Mirrors the npm / Codex UX: a background refresh writes the latest known
version to a small cache, and the *next* launch surfaces a Codex-style menu if
an update is pending. All network work is best-effort and fully silent offline
— ``omni`` startup never blocks on it.

Entry points:

- :func:`maybe_refresh_in_background` — spawn a daemon thread to refresh the
  cache when it is stale (called once at REPL startup); its result is used on
  the *next* launch, so startup stays instant;
- :func:`pending_update_notice` — a pure, offline read of the cache telling the
  caller whether to prompt (and to which version);
- :func:`fetch_latest_version` — a synchronous, short-timeout fetch used by
  ``omni update`` for an on-demand comparison.

Version source (``update.source``): stable installs default to ``pypi``.
``auto`` (PyPI, then a raw-file fallback) and ``raw`` remain explicit
development-channel compatibility modes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from omni.runtime.dist_meta import DIST_NORMALIZED

if TYPE_CHECKING:
    from omni.config.paths import OmniPaths
    from omni.config.settings import OmniSettings

logger = logging.getLogger(__name__)

_PYPI_URL = f"https://pypi.org/pypi/{DIST_NORMALIZED}/json"
_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
_CACHE_NAME = "update-check.json"
_DISABLE_ENV = "OMNI_UPDATE_CHECK"
_DISABLED_VALUES = {"0", "false", "no", "off"}


# ── enable switch ──────────────────────────────────────────────────────────


def update_check_enabled(settings: OmniSettings) -> bool:
    """True when the startup check should run (config on and env not disabling).

    ``OMNI_UPDATE_CHECK=0`` (or false/no/off) hard-disables it for CI / air-gapped
    machines regardless of config.
    """
    if not getattr(settings.update, "check", True):
        return False
    return os.environ.get(_DISABLE_ENV, "").strip().lower() not in _DISABLED_VALUES


# ── cache I/O ──────────────────────────────────────────────────────────────


def _cache_path(paths: OmniPaths) -> Path:
    return paths.home / _CACHE_NAME


def read_cache(paths: OmniPaths) -> dict:
    """Read the update cache; ``{}`` on any error (missing / corrupt)."""
    try:
        data = json.loads(_cache_path(paths).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(paths: OmniPaths, data: dict) -> None:
    try:
        paths.home.mkdir(parents=True, exist_ok=True)
        _cache_path(paths).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug("update-check cache write failed: %s", exc)


def cache_is_fresh(cache: dict, interval_hours: float) -> bool:
    """True when ``checked_at`` is within the TTL window."""
    ts = cache.get("checked_at")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - float(ts)) < max(0.0, float(interval_hours)) * 3600.0


# ── version comparison ─────────────────────────────────────────────────────


def newer_available(current: str, latest: str | None) -> bool:
    """True when ``latest`` is a strictly newer release than ``current`` (PEP 440)."""
    if not latest:
        return False
    try:
        from packaging.version import InvalidVersion, Version
    except Exception:  # noqa: BLE001 - packaging is a dep, but never hard-fail here.
        return latest != current
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


def is_source_build_version(version: str) -> bool:
    """True when ``version`` is a local/source build rather than a published release.

    Mirrors Codex's ``is_source_build_version``: a checkout or dev build carries a
    non-release marker — a PEP 440 dev segment (``2.0.0.dev0``), a local segment
    (``2.0.0+local``), an unparseable string, or the ``0.0.0`` source sentinel —
    whose static value cannot be meaningfully compared against "latest published".
    ``omni update`` uses this to decide freshness from the *git checkout* (behind
    count) instead of the version string for such builds.
    """
    v = (version or "").strip()
    if not v:
        return True
    try:
        from packaging.version import InvalidVersion, Version
    except Exception:  # noqa: BLE001 - packaging is a dep, but never hard-fail here.
        return ".dev" in v or "+" in v or v.startswith("0.0.0")
    try:
        parsed = Version(v)
    except InvalidVersion:
        return True
    return parsed.is_devrelease or parsed.local is not None or parsed.release == (0, 0, 0)


# ── network fetch ──────────────────────────────────────────────────────────


def _fetch_pypi(client: httpx.Client) -> str | None:
    resp = client.get(_PYPI_URL, headers={"Accept": "application/json"})
    if resp.status_code == 200:
        return str((resp.json().get("info") or {}).get("version") or "") or None
    return None


def _fetch_raw(client: httpx.Client, raw_url: str) -> str | None:
    if not raw_url:
        return None
    resp = client.get(raw_url)
    if resp.status_code == 200:
        match = _VERSION_RE.search(resp.text)
        return match.group(1) if match else None
    return None


def fetch_latest_version(settings: OmniSettings, *, timeout: float = 3.0) -> str | None:
    """Fetch the latest published version, honouring ``update.source``.

    ``auto`` tries PyPI first (works once published), falling back to the raw
    ``__init__.py`` on the public repo. Fully best-effort: any error → ``None``.
    """
    source = (getattr(settings.update, "source", "auto") or "auto").lower()
    raw_url = getattr(settings.update, "raw_url", "") or ""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if source == "pypi":
                return _fetch_pypi(client)
            if source == "raw":
                return _fetch_raw(client, raw_url)
            try:
                found = _fetch_pypi(client)
            except Exception as exc:  # noqa: BLE001 - PyPI hiccup → try the raw fallback.
                logger.debug("update-check PyPI fetch failed: %s", exc)
                found = None
            return found or _fetch_raw(client, raw_url)
    except Exception as exc:  # noqa: BLE001 - offline / DNS / timeout are all non-fatal.
        logger.debug("update-check fetch failed: %s", exc)
        return None


# ── git branch channel freshness (commit-based) ────────────────────────────
#
# A ``--channel master`` install keeps the same static dev version string
# (``2.0.0.dev0``) as it advances, so the version comparison above can never see
# it as stale. For that case we compare the *commit* the branch tip resolves to
# against the installed commit, mirroring how ``omni update`` re-resolves the
# tip. All of this is dormant unless the running CLI is a moving-branch VCS
# install (PEP 610 ``requested_revision`` is a branch), so a dev/editable/PyPI
# install pays nothing.

_IMMUTABLE_REF_RE = re.compile(r"^([0-9a-fA-F]{40}|v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?)$")


def _installed_direct_url() -> dict:
    """PEP 610 ``direct_url.json`` for the running OmniScientist-V2 dist, or ``{}``."""
    try:
        import importlib.metadata as md

        from omni.runtime.dist_meta import DIST_NAME

        raw = md.distribution(DIST_NAME).read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - not installed as a dist / no metadata.
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def installed_commit_id() -> str:
    """The pinned commit of a VCS install (PEP 610 ``commit_id``), or ``""``."""
    return str((_installed_direct_url().get("vcs_info") or {}).get("commit_id") or "")


def installed_branch_channel() -> str:
    """The moving branch a VCS install tracks, or ``""``.

    Only a *branch* (not a commit/tag pin) qualifies — a reproducible pin never
    needs a "new commits" notice.
    """
    ref = str((_installed_direct_url().get("vcs_info") or {}).get("requested_revision") or "")
    if not ref or _IMMUTABLE_REF_RE.match(ref) is not None:
        return ""
    return ref


def _branch_api_url(raw_url: str, branch: str) -> str | None:
    """Derive a branch-head API URL from an explicit GitHub/Gitee raw URL."""
    github = re.match(
        r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/",
        raw_url or "",
    ) or re.match(r"^https?://github\.com/([^/]+)/([^/]+)/(?:raw|blob)/", raw_url or "")
    encoded_branch = quote(branch, safe="")
    if github:
        owner, repo = github.group(1), github.group(2)
        return f"https://api.github.com/repos/{owner}/{repo}/branches/{encoded_branch}"
    gitee = re.match(r"^https?://gitee\.com/([^/]+)/([^/]+)/raw/", raw_url or "")
    if gitee:
        owner, repo = gitee.group(1), gitee.group(2)
        return f"https://gitee.com/api/v5/repos/{owner}/{repo}/branches/{encoded_branch}"
    return None


def fetch_remote_commit(settings: OmniSettings, branch: str, *, timeout: float = 3.0) -> str | None:
    """Fetch the head commit sha of ``branch`` on the public repo (best-effort)."""
    api = _branch_api_url(getattr(settings.update, "raw_url", "") or "", branch)
    if not api:
        return None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                api,
                headers={
                    "Accept": "application/vnd.github+json, application/json",
                },
            )
            if resp.status_code != 200:
                return None
            return str(((resp.json() or {}).get("commit") or {}).get("sha") or "") or None
    except Exception as exc:  # noqa: BLE001 - offline / DNS / timeout are all non-fatal.
        logger.debug("update-check remote commit fetch failed: %s", exc)
        return None


# ── cache refresh (background) ─────────────────────────────────────────────


def record_latest(paths: OmniPaths, latest: str | None) -> None:
    """Persist an already-known latest version + timestamp (no network)."""
    cache = read_cache(paths)
    cache["checked_at"] = time.time()
    if latest:
        cache["latest"] = latest
    write_cache(paths, cache)


def refresh_cache(settings: OmniSettings) -> str | None:
    """Fetch the latest version and persist it, preserving ``skip_version``.

    For a git *branch* channel install, also refresh the branch's head commit so
    the next launch can surface "new commits on <branch>" (the version string is
    static for a channel, so this is the only freshness signal). Dormant — and
    network-free — for any non-branch install.
    """
    paths = settings.paths
    if paths is None:
        return None
    latest = fetch_latest_version(settings)
    record_latest(paths, latest)
    branch = installed_branch_channel()
    if branch:
        remote = fetch_remote_commit(settings, branch)
        if remote:
            cache = read_cache(paths)
            cache["remote_commit"] = remote
            cache["branch"] = branch
            write_cache(paths, cache)
    return latest


def maybe_refresh_in_background(settings: OmniSettings) -> threading.Thread | None:
    """Spawn a daemon thread to refresh the cache when stale (never blocks).

    Returns the thread (for tests) or ``None`` when a refresh is not needed /
    disabled. The refreshed value is consumed on the *next* launch, keeping this
    one instant.
    """
    if not update_check_enabled(settings) or settings.paths is None:
        return None
    interval = float(getattr(settings.update, "interval_hours", 24) or 24)
    if cache_is_fresh(read_cache(settings.paths), interval):
        return None

    def _worker() -> None:
        try:
            refresh_cache(settings)
        except Exception as exc:  # noqa: BLE001
            logger.debug("background update refresh failed: %s", exc)

    thread = threading.Thread(target=_worker, name="omni-update-check", daemon=True)
    thread.start()
    return thread


# ── notice + skip + reset ──────────────────────────────────────────────────


def pending_update_notice(current: str, paths: OmniPaths, settings: OmniSettings) -> str | None:
    """Return the latest version when an update is pending, else ``None``.

    Pure, offline read of the cache a prior background refresh wrote. Honours the
    enable switch and ``skip_version`` (the "don't remind me for this version"
    choice), so callers can prompt unconditionally on a non-``None`` result.
    """
    if not update_check_enabled(settings):
        return None
    cache = read_cache(paths)
    latest = cache.get("latest")
    if not isinstance(latest, str) or not latest:
        return None
    if latest == cache.get("skip_version"):
        return None
    return latest if newer_available(current, latest) else None


def pending_channel_notice(paths: OmniPaths, settings: OmniSettings) -> str | None:
    """For a git *branch* channel, return the branch when new commits are pending.

    Pure, offline read of the cache the background refresh wrote: compares the
    cached remote head commit against the installed commit. Returns the branch
    name to hint at, or ``None`` (offline / not a branch channel / same commit /
    skipped). Never raises — the startup notifier must not break launch.
    """
    if not update_check_enabled(settings):
        return None
    branch = installed_branch_channel()
    if not branch:
        return None
    cache = read_cache(paths)
    remote = cache.get("remote_commit")
    if not isinstance(remote, str) or not remote or remote == cache.get("skip_commit"):
        return None
    installed = installed_commit_id()
    return branch if installed and remote != installed else None


def mark_skip_version(paths: OmniPaths, version: str) -> None:
    """Record "don't remind me until a newer version" for ``version``."""
    cache = read_cache(paths)
    cache["skip_version"] = version
    write_cache(paths, cache)


def clear_update_state(paths: OmniPaths) -> None:
    """Drop the cache after a successful update so the next launch re-checks."""
    try:
        _cache_path(paths).unlink(missing_ok=True)
    except OSError:
        pass
