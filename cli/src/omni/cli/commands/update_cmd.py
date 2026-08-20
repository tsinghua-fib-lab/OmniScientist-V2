"""Owner-aware ``omni update`` and local lifecycle convergence.

The public command is intentionally parameterless. A published installation
delegates package replacement to its actual owner (uv tool, pipx, or the active
Python environment); a source checkout fast-forwards safely and reinstalls.
Package replacement is serialized with managed-runtime preparation, legacy
daemon cleanup, Home Service launcher refresh/restoration, and a durable
installation fingerprint.

Older advanced options remain hidden compatibility inputs for one transition
period. They are implementation seams, not the supported user workflow.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer

from omni.cli.render import console, data_table, error, info, success, warn
from omni.personas.installer import BuiltinPersonaInstallError, install_builtin_personas
from omni.runtime.daemon import list_running_daemons, untracked_serve_pids
from omni.runtime.dist_meta import DIST_NAME as DIST
from omni.runtime.uninstall import installation_method_for_prefix
from omni.skills_runtime.runtime_setup import (
    SkillRuntimeSetupError,
    setup_research_pptx_runtime,
)

app = typer.Typer(
    help="Update OmniScientist and inspect update status.",
    invoke_without_command=True,
)

# An immutable git ref is a full 40-char commit hash or a release tag (v?X.Y.Z…);
# anything else recorded as the requested revision (e.g. ``master``) is a moving
# branch channel that ``omni update`` must re-resolve to the tip each run.
_IMMUTABLE_REF = re.compile(r"^([0-9a-fA-F]{40}|v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?)$")
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_HTTP_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^#\s]+", re.IGNORECASE)


def _display_update_command(argv: list[str]) -> str:
    """Shell-quote an update command without printing URL credentials."""
    safe: list[str] = []
    for value in argv:
        redacted = _URL_USERINFO_RE.sub(r"\1***@", value)
        redacted = _HTTP_QUERY_RE.sub(r"\1?<redacted>", redacted)
        safe.append(redacted)
    return shlex.join(safe)


def _invoked_launcher_dir() -> Path | None:
    """Return the directly invoked external launcher directory, if trustworthy.

    Never fall back to ``PATH`` here. In a multi-install setup the first
    ``omni`` on ``PATH`` may belong to another uv/pipx registry; binding that
    directory as this installation's app directory could rewrite the wrong
    launcher.
    """
    raw = Path(sys.argv[0]).expanduser()
    candidate = raw if raw.name.lower() in {"omni", "omni.exe"} else None
    if candidate is None or not candidate.exists():
        return None
    try:
        absolute = candidate.absolute()
        if absolute.is_relative_to(Path(sys.prefix).resolve()):
            return None
    except OSError:
        return None
    return absolute.parent


def _manager_environment(kind: str) -> dict[str, str] | None:
    """Bind a manager command to the registry owning the running prefix."""
    prefix = Path(sys.prefix).resolve()
    env = os.environ.copy()
    # Drop host bin-dir bindings so a CI/dev shell that exported PIPX_BIN_DIR /
    # UV_TOOL_BIN_DIR cannot redirect the upgrade into another registry. We
    # re-add them below only when the invoked launcher itself is trustworthy.
    env.pop("UV_TOOL_BIN_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    launcher_dir = _invoked_launcher_dir()
    if kind == "uv":
        env["UV_TOOL_DIR"] = str(prefix.parent)
        if launcher_dir is not None:
            env["UV_TOOL_BIN_DIR"] = str(launcher_dir)
        return env
    if kind == "pipx":
        # pipx environments are <PIPX_HOME>/venvs/<package>. The metadata
        # marker was already checked by owner detection; bind the command to
        # this exact home so another default registry can never be upgraded.
        owner_home = prefix.parent.parent
        env["PIPX_HOME"] = str(owner_home)
        # pipx initializes its man directory even when this package exposes no
        # man pages. Keep that write owner-local instead of touching an
        # unrelated default registry.
        env["PIPX_MAN_DIR"] = str(owner_home / "man")
        if launcher_dir is not None:
            env["PIPX_BIN_DIR"] = str(launcher_dir)
        return env
    return None


def _run_package_command(argv: list[str], kind: str) -> subprocess.CompletedProcess:
    """Run a package-manager command against the current installation owner."""
    manager_env = _manager_environment(kind)
    if manager_env is None:
        return subprocess.run(argv, check=False)
    return subprocess.run(argv, check=False, env=manager_env)


def _stable_untracked_serve_pids(
    settings,  # noqa: ANN001 - avoids importing config during module startup.
    *,
    polls: int = 3,
    interval_s: float = 0.1,
) -> list[int]:
    """Report only same-home serve PIDs that survive a short convergence window."""
    from omni.runtime import service_state

    service_id = service_state.service_instance_id(settings.paths)

    def _scan() -> set[int]:
        runtime = service_state.read_runtime(settings.paths) or {}
        observation = service_state.observe_service(settings.paths)
        try:
            runtime_pid = int(runtime.get("pid", 0) or 0)
        except (TypeError, ValueError):
            runtime_pid = 0
        known = {
            runtime_pid,
            int(observation.pid or 0),
            int(service_state.singleton_holder_pid(settings.paths) or 0),
        }
        return set(
            untracked_serve_pids(
                list_running_daemons(settings.paths.home),
                extra_pids=known,
                service_id=service_id,
            )
        )

    persistent = _scan()
    for _ in range(1, max(1, polls)):
        if not persistent:
            break
        time.sleep(max(0.0, interval_s))
        persistent.intersection_update(_scan())
    return sorted(persistent)


def _ref_is_moving(ref: str) -> bool:
    """True when ``ref`` is a moving branch (not a pinned commit/tag)."""
    ref = ref.strip()
    return bool(ref) and _IMMUTABLE_REF.match(ref) is None


def _spec_is_moving_git(spec: str) -> bool:
    """True when a package ``spec`` is a ``git+…@<branch>`` moving channel.

    Parses the ref from ``git+<url>@<ref>#subdirectory=…`` defensively: the ref
    is the segment after the final ``@`` (a branch/tag/commit never contains a
    ``/``), which sidesteps ``git+ssh://user@host/…`` userinfo false positives.
    """
    if "git+" not in spec:
        return False
    tail = spec.split("git+", 1)[1].split("#", 1)[0]
    if "@" not in tail:
        return False
    ref = tail.rsplit("@", 1)[1]
    if not ref or "/" in ref:
        return False
    return _ref_is_moving(ref)


def _distribution() -> md.Distribution | None:
    try:
        return md.distribution(DIST)
    except md.PackageNotFoundError:
        return None


def _read_direct_url(dist: md.Distribution | None) -> dict:
    """PEP 610 ``direct_url.json`` for an install, or ``{}`` when absent/corrupt."""
    if dist is None:
        return {}
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _vcs_channel_ref(dist: md.Distribution | None) -> str:
    """The git ref a VCS install tracks (branch/tag/commit), or ``""``.

    Prefers PEP 610 ``requested_revision`` (the ref the user asked for, e.g.
    ``master``) over the pinned ``commit_id`` so a branch channel advances to the
    tip while a commit/tag pin stays reproducible.
    """
    vcs = _read_direct_url(dist).get("vcs_info") or {}
    return str(vcs.get("requested_revision") or vcs.get("commit_id") or "")


def _editable_source(dist: md.Distribution | None) -> Path | None:
    """Return the source dir for an editable install (PEP 610), else ``None``."""
    if dist is None:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (data.get("dir_info") or {}).get("editable"):
        return None
    url = data.get("url", "")
    if not url.startswith("file://"):
        return None
    return Path(unquote(urlparse(url).path))


def _local_source(dist: md.Distribution | None) -> Path | None:
    """Return the local source dir for a ``file://`` install (editable *or* not).

    Both an editable checkout and a snapshot install (``uv pip install ./cli``)
    record a ``file://`` url in PEP 610 ``direct_url.json``. Either is a *source
    checkout* we can refresh with ``git pull`` when it sits inside a repository —
    unlike :func:`_editable_source`, this does not require the editable flag.
    """
    if dist is None:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    url = str(data.get("url") or "")
    if not url.startswith("file://"):
        return None
    path = Path(unquote(urlparse(url).path))
    return path if path.exists() else None


def _git_root(source: Path) -> Path | None:
    """Find the repository root for an editable package in a subdirectory."""
    for candidate in (source, *source.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _source_checkout(dist: md.Distribution | None = None) -> tuple[Path, Path, bool] | None:
    """Resolve a git-backed source install.

    Returns ``(repo_root, source_dir, editable)`` when the running CLI is a
    ``file://`` install (editable or snapshot) that lives inside a git
    repository; ``None`` otherwise (a published package, or a loose directory
    with no ``.git``). This is the single seam both :func:`_plan` and the
    executor consult, so the decision and the action never diverge.
    """
    dist = dist if dist is not None else _distribution()
    editable_src = _editable_source(dist)
    src = editable_src or _local_source(dist)
    if src is None:
        return None
    root = _git_root(src)
    if root is None:
        return None
    return root, src, editable_src is not None


def _git_pull_argv(repo_root: Path, ref: str = "") -> list[str]:
    """The fast-forward-only pull for a source checkout.

    With ``ref`` we pull that branch explicitly from ``origin`` (``--ref master``);
    without it we fast-forward the current branch's configured upstream. ``--ff-only``
    guarantees we never create a merge commit or rewrite history — a diverged
    branch simply fails and we abort with guidance.
    """
    argv = ["git", "-C", str(repo_root), "pull", "--ff-only"]
    if ref.strip():
        argv += ["origin", ref.strip()]
    return argv


def _git_tree_is_dirty(repo_root: Path) -> bool:
    """True when tracked files have uncommitted changes (untracked files ignored).

    Untracked files never block a fast-forward, so they do not count as "dirty";
    staged/modified/deleted tracked files do, and we refuse to touch them.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if out.returncode != 0:
        return False
    return any(line and not line.startswith("??") for line in out.stdout.splitlines())


def _git_behind_count(repo_root: Path, ref: str = "") -> int | None:
    """How many commits ``HEAD`` is behind its target, or ``None`` if undeterminable.

    Best-effort and read-only: fetches the target (so the count reflects the
    remote), then counts ``HEAD..<target>``. Returns ``None`` offline or when no
    upstream is configured, letting the caller fall through to attempting a pull
    rather than falsely reporting "up to date".
    """
    target = f"origin/{ref.strip()}" if ref.strip() else "@{u}"
    fetch = ["git", "-C", str(repo_root), "fetch", "--quiet"]
    fetch += ["origin", ref.strip()] if ref.strip() else []
    try:
        subprocess.run(fetch, capture_output=True, text=True, check=False)
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-list", "--count", f"HEAD..{target}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def _installed_source_spec(dist: md.Distribution | None) -> str:
    """Preserve a non-editable local source snapshot when one owns this install."""
    if dist is None:
        return DIST
    try:
        raw = dist.read_text("direct_url.json")
        data = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        return DIST
    url = str(data.get("url") or "")
    if url.startswith("file://"):
        path = Path(unquote(urlparse(url).path))
        if path.exists():
            return str(path)
    vcs = data.get("vcs_info") or {}
    # Prefer the *requested* revision (branch/tag) so a branch channel reinstalls
    # the moving tip; fall back to the pinned commit for reproducible installs.
    ref = str(vcs.get("requested_revision") or vcs.get("commit_id") or "")
    vcs_name = str(vcs.get("vcs") or "")
    if url and ref and vcs_name:
        direct = f"{DIST} @ {vcs_name}+{url}@{ref}"
        subdirectory = str(data.get("subdirectory") or "")
        if subdirectory:
            direct += f"#subdirectory={subdirectory}"
        return direct
    if url and data.get("archive_info") is not None:
        return f"{DIST} @ {url}"
    return DIST


def _exact_install_plan(
    spec: str,
    *,
    reinstall: bool,
    editable: bool = False,
    refresh: bool = False,
    preserve_owner: bool = False,
) -> tuple[str, list[str], str]:
    """Build an install command bound to ``sys.executable``/``sys.prefix``.

    ``refresh`` busts uv's git cache (``--refresh-package``) so a moving branch
    channel re-resolves to the current tip; pip already re-resolves a git ref on
    every install, so the flag is a uv-only no-op there.
    """
    kind = installation_method_for_prefix(Path(sys.prefix))
    uv = shutil.which("uv")
    if kind == "uv":
        # Never mutate a uv-owned tool with ``python -m pip``: doing so leaves
        # uv's receipt inconsistent, and uv tool environments need not contain
        # pip. A missing uv executable therefore remains an actionable native
        # command failure instead of silently falling back.
        uv_command = uv or "uv"
        if (spec == DIST and not editable) or preserve_owner:
            argv = [
                uv_command,
                "tool",
                "upgrade",
                DIST,
                "--compile-bytecode",
            ]
            if refresh:
                argv.extend(["--refresh-package", DIST])
            if reinstall:
                argv.extend(["--reinstall-package", DIST])
        else:
            argv = [
                uv_command,
                "tool",
                "install",
                "--force",
                "--compile-bytecode",
            ]
            if refresh:
                argv.extend(["--refresh-package", DIST])
            if editable:
                argv.append("--editable")
            argv.append(spec)
    elif kind == "pipx":
        pipx = shutil.which("pipx") or "pipx"
        if (spec == DIST and not editable) or preserve_owner:
            argv = [pipx, "upgrade", DIST]
            if reinstall:
                argv.append("--force")
        else:
            argv = [pipx, "install", "--force"]
            if editable:
                argv.append("--editable")
            argv.append(spec)
    elif uv:
        # ``--compile-bytecode``: uv skips .pyc by default, which would defer the
        # whole package's bytecode compilation to the first post-update process
        # (the research-pptx setup subprocess, then the first ``omni`` launch),
        # showing up as a mysterious multi-second pause. pip compiles by default.
        argv = [uv, "pip", "install", "--python", sys.executable, "--upgrade", "--compile-bytecode"]
        if refresh:
            argv.extend(["--refresh-package", DIST])
        if reinstall:
            argv.extend(["--reinstall-package", DIST])
        if editable:
            argv.append("--editable")
        argv.append(spec)
    else:
        argv = [sys.executable, "-m", "pip", "install", "--upgrade"]
        if reinstall:
            argv.append("--force-reinstall")
        if editable:
            argv.append("--editable")
        argv.append(spec)
    return kind, argv, f"{kind} owner; current interpreter: {sys.executable}"


def _plan(
    ref: str = "",
    *,
    force_reinstall: bool = False,
) -> tuple[str, list[str], str]:
    """Decide how to upgrade. Returns ``(kind, argv, human_label)``.

    ``kind`` is ``"git"`` for a source checkout (two-phase: pull then reinstall;
    ``argv`` is the representative pull command shown by ``--check``),
    ``"manual"`` when there is no automatic path (an unmanaged source build), or
    the installation method (``uv`` / ``pipx`` / ``env``) for a published install.
    """
    dist = _distribution()
    checkout = _source_checkout(dist)
    if checkout is not None:
        repo_root, src, editable = checkout
        mode = "editable" if editable else "snapshot"
        target = f"origin/{ref.strip()}" if ref.strip() else "current branch upstream"
        return (
            "git",
            _git_pull_argv(repo_root, ref),
            f"git pull --ff-only + reinstall ({mode} source: {src}; repo: {repo_root}; target: {target})",
        )
    spec = _installed_source_spec(dist)
    if spec == DIST:
        # Not a source checkout and not a resolvable local/VCS snapshot. If this
        # is a source build (dev version), the published package can't upgrade it
        # → surface manual guidance rather than run a doomed ``uv pip install``.
        from omni import __version__
        from omni.runtime.update_check import is_source_build_version

        if is_source_build_version(__version__):
            return (
                "manual",
                [],
                "unmanaged source build (no git checkout, no published release to upgrade from)",
            )
    # A git *branch* channel (e.g. installed with ``--channel master``) advances
    # to the tip on every update; a commit/tag pin stays reproducible.
    moving = _ref_is_moving(_vcs_channel_ref(dist))
    kind, argv, label = _exact_install_plan(
        spec,
        reinstall=force_reinstall or spec != DIST,
        refresh=moving,
        preserve_owner=spec != DIST,
    )
    if moving:
        label = f"git channel tip — non-reproducible; {label}"
    return kind, argv, label


def _prepare_local_web_ui(cli_src: Path) -> None:
    """Rebuild ``web/dist`` before deploying an explicit source checkout.

    A snapshot copies ``web/dist`` into ``omni/data/web``; an editable install
    serves that same directory directly. Both modes therefore need the current
    Vite output before Python installation is synchronized.
    """
    web = cli_src.parent / "web" / "package.json"
    if not web.is_file():
        return
    if shutil.which("node") is None and shutil.which("node.exe") is None:
        error(
            "Node.js is required to build the current web UI for a local checkout; "
            "an existing web/dist will not be reused because it may be stale."
        )
        raise typer.Exit(1)
    if os.name == "nt":
        script = cli_src / "scripts" / "build_web_ui.ps1"
        shell = shutil.which("pwsh") or shutil.which("powershell") or shutil.which(
            "powershell.exe"
        )
        if shell is None:
            error("PowerShell is required to build the current web UI on Windows.")
            raise typer.Exit(1)
        command = [shell, "-ExecutionPolicy", "Bypass", "-File", str(script)]
    else:
        script = cli_src / "scripts" / "build_web_ui.sh"
        command = ["bash", str(script)]
    if not script.is_file():
        error(f"Web UI build script is missing: {script}")
        raise typer.Exit(1)
    info("Building the loopback SPA from the current checkout...")
    proc = subprocess.run(command, check=False)
    if proc.returncode != 0:
        error("Failed to build the web UI. Fix web/ or install Node, then retry.")
        raise typer.Exit(proc.returncode)


def _resolve_local_checkout(dist: md.Distribution | None = None) -> Path | None:
    """Resolve the local source checkout dir (the installable ``cli`` package).

    Used by ``omni update --local/--dev/--editable`` so a developer never has to
    type the path. Resolution order:

    1. the recorded ``file://`` source of this install (editable or snapshot), so
       the canonical checkout is redeployed regardless of the current directory;
    2. otherwise the checkout under the current directory (or an ancestor up to
       the repo root), letting a user on a git-channel install switch to a local
       deploy from inside a clone.

    Returns the directory that contains ``pyproject.toml`` + ``src/omni``, or
    ``None`` when no local checkout can be found.
    """
    dist = dist if dist is not None else _distribution()

    def _is_cli_package(path: Path) -> bool:
        return (path / "pyproject.toml").exists() and (path / "src" / "omni" / "__init__.py").exists()

    recorded = _editable_source(dist) or _local_source(dist)
    if recorded is not None and _is_cli_package(recorded):
        return recorded

    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        cli_dir = base / "cli"
        if _is_cli_package(cli_dir):
            return cli_dir
        if _is_cli_package(base):
            return base
        if (base / ".git").exists():
            break
    return None


def _render_manual_update_help(current: str) -> None:
    """Guidance for an unmanaged install omni cannot upgrade automatically."""
    warn(f"OmniScientist {current} looks like an unmanaged source build.")
    info("omni update can't upgrade it automatically. To update manually:")
    info("  • from a git checkout:  git -C <repo> pull --ff-only && uv pip install -e <repo>/cli")
    info("  • or reinstall the tool: uv tool install --force OmniScientist-V2  (once published)")


def _execute_git_update(*, repo_root: Path, src: Path, editable: bool, ref: str) -> None:
    """Two-phase source update: fast-forward the checkout, then reinstall it.

    Refuses to touch a dirty tree or a diverged branch — omni never stashes,
    discards, or force-resets user work; it aborts with guidance so the user
    stays in control. Raises :class:`typer.Exit` on any failure.
    """
    if _git_tree_is_dirty(repo_root):
        error(
            f"Local uncommitted changes in {repo_root}; aborting so your work is left untouched.\n"
            "Commit or stash them, then re-run `omni update`."
        )
        raise typer.Exit(1)

    pull = _git_pull_argv(repo_root, ref)
    info(f"Fast-forwarding source checkout ({repo_root})...")
    proc = subprocess.run(pull, check=False)
    if proc.returncode != 0:
        error(
            "git pull --ff-only failed — the branch has diverged or has no upstream. "
            "No files were changed. Reconcile it yourself (e.g. `git pull` / rebase) "
            "or target another branch with `omni update --ref <branch>`."
        )
        raise typer.Exit(proc.returncode)

    try:
        _prepare_local_web_ui(src)
    except typer.Exit:
        error(
            "The source checkout was fast-forwarded, but the installed package was not "
            "changed. After fixing the Web UI toolchain/build, redeploy the checkout "
            "with its repository installer (`./cli/scripts/install.sh --local` on "
            "macOS/Linux or `cli\\scripts\\install.ps1 -Local` on Windows)."
        )
        raise

    if editable:
        reinstall = _editable_dependency_sync_plan()
        reinstall_kind = installation_method_for_prefix(Path(sys.prefix))
        note = "Synchronizing Python dependencies for the editable installation..."
    else:
        reinstall_kind, reinstall, _label = _exact_install_plan(
            str(src),
            reinstall=True,
            preserve_owner=True,
        )
        note = "Reinstalling OmniScientist from the updated source..."
    if reinstall:
        info(note)
        try:
            rproc = _run_package_command(reinstall, reinstall_kind)
        except FileNotFoundError as exc:
            if reinstall_kind in {"uv", "pipx"}:
                error(
                    f"Source checkout was fast-forwarded, but its {reinstall_kind} "
                    f"owner is unavailable ({exc}). Restore {reinstall_kind} on PATH "
                    "and run `omni update` again to finish synchronizing the installed package."
                )
            else:
                error(
                    f"Source checkout was fast-forwarded, but the reinstall command "
                    f"is unavailable ({exc}). Run `omni update` again after repairing the environment."
                )
            raise typer.Exit(1) from exc
        if rproc.returncode != 0:
            error(
                "Source pulled successfully, but the reinstall failed "
                f"(exit={rproc.returncode}). Re-run `omni update` after fixing the environment."
            )
            raise typer.Exit(rproc.returncode)


def _editable_dependency_sync_plan() -> list[str]:
    """Re-sync dependencies after git updates an editable source checkout."""
    source = _editable_source(_distribution())
    if source is None:
        return []
    _kind, argv, _label = _exact_install_plan(
        str(source),
        reinstall=True,
        editable=True,
        preserve_owner=True,
    )
    return argv


def _prepare_bundled_skill_runtimes(paths) -> None:  # noqa: ANN001
    """Prepare lock-pinned components as part of the update transaction."""
    info("Checking bundled Skill runtimes...")
    try:
        personas = install_builtin_personas(paths)
    except BuiltinPersonaInstallError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if personas.installed:
        success(
            f"Installed {len(personas.installed)} bundled scientist personas into "
            f"{paths.scientist_kg_dir}."
        )
    else:
        info("Bundled scientist personas are ready; existing directories were preserved.")
    try:
        changed = setup_research_pptx_runtime(paths)
    except SkillRuntimeSetupError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if changed:
        success("research-pptx renderer dependencies installed.")
    else:
        info("research-pptx renderer dependencies are ready.")


def _prepare_bundled_skill_runtimes_with_updated_cli(_paths) -> None:  # noqa: ANN001
    """Run setup in a fresh process so it imports the just-updated package.

    Do not short-circuit this with the old process's readiness check: a new
    release may change the lock-pinned runtime requirements while the previous
    release still considers its runtime ready.
    """
    info("Checking bundled Skill runtimes with the updated CLI...")
    command = [
        sys.executable,
        "-m",
        "omni.cli.main",
        "skills",
        "setup",
        "all",
    ]
    try:
        proc = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        error(f"Updated runtime setup command unavailable: {exc}.")
        raise typer.Exit(1) from exc
    if proc.returncode != 0:
        error(
            "OmniScientist was updated, but bundled resource setup failed "
            f"(exit={proc.returncode}). Run `omni skills setup all` to retry."
        )
        raise typer.Exit(proc.returncode)


@app.callback(invoke_without_command=True)
def update_command(
    ctx: typer.Context,
    check: bool = typer.Option(
        False,
        "--check",
        help="Compatibility alias for a read-only update preview.",
        hidden=True,
    ),
    to: str = typer.Option(
        "",
        "--to",
        help="Compatibility target override.",
        hidden=True,
    ),
    ref: str = typer.Option(
        "",
        "--ref",
        help="Compatibility source-checkout ref override.",
        hidden=True,
    ),
    local: bool = typer.Option(
        False,
        "--local",
        "--dev",
        help="Compatibility developer deployment mode.",
        hidden=True,
    ),
    editable: bool = typer.Option(
        False,
        "--editable",
        help="Compatibility editable deployment mode.",
        hidden=True,
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Compatibility non-interactive switch.",
        hidden=True,
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Compatibility forced package reinstall.",
        hidden=True,
    ),
    restart_serve: bool = typer.Option(
        True,
        "--restart-serve/--no-restart-serve",
        help="Compatibility service restoration switch.",
        hidden=True,
    ),
) -> None:
    """Update the current installation and converge its managed runtime."""
    from omni import __version__
    from omni.runtime import update_check

    if ctx.invoked_subcommand is not None:
        return

    settings = ctx.obj.settings()
    current = __version__
    info(f"Current version: OmniScientist {current}")
    package_change = True

    if local or editable:
        if to:
            error("--local/--dev/--editable cannot be combined with --to.")
            raise typer.Exit(2)
        src = _resolve_local_checkout()
        if src is None:
            error(
                "No local source checkout is linked to this install, and the current "
                "directory is not an OmniScientist checkout.\n"
                "Install from your checkout first (scripts/install.sh --local [--editable]), "
                "or pass an explicit target with `omni update --to <path-or-spec>`."
            )
            raise typer.Exit(1)
        linked = _source_checkout()
        preserve_owner = bool(
            linked is not None
            and linked[1].resolve() == src.resolve()
            and linked[2] is editable
        )
        current_owner = installation_method_for_prefix(Path(sys.prefix))
        if current_owner in {"uv", "pipx"} and not preserve_owner:
            error(
                "Changing a manager-owned install between source checkouts or "
                "snapshot/editable modes can discard its recorded extras and package index.\n"
                "Rerun this checkout's installer instead: "
                "`./cli/scripts/install.sh --local` (add `--editable` for live edits)."
            )
            raise typer.Exit(2)
        _kind, cmd, label = _exact_install_plan(
            str(src),
            reinstall=True,
            editable=editable,
            preserve_owner=preserve_owner,
        )
        mode = "editable (live edits)" if editable else "snapshot of current tree (incl. uncommitted)"
        label = f"developer {mode} from {src}; {label}"
        if not check:
            _prepare_local_web_ui(src)
    elif to:
        target = DIST if to.strip() == f"{DIST}@latest" else to.strip()
        _kind, cmd, label = _exact_install_plan(target, reinstall=True, refresh=_spec_is_moving_git(target))
    else:
        _kind, cmd, label = _plan(ref=ref, force_reinstall=force)
        if _kind == "git":
            # Source checkout: git state — not the (static, unpublished) version
            # string — decides freshness. When the branch is current and the tree
            # is clean, there is nothing to do (unless --force forces a reinstall).
            # ``--check`` only displays the plan, so it never probes the network
            # (no ``git fetch``) or short-circuits here.
            if not check and not force:
                checkout = _source_checkout()
                if checkout is not None:
                    repo_root, _src, _editable = checkout
                    if _git_behind_count(repo_root, ref) == 0 and not _git_tree_is_dirty(repo_root):
                        package_change = False
                        cmd = []
                        label = "source checkout is current; converge managed runtime and service"
            if package_change:
                info("Source checkout detected; will fast-forward and reinstall.")
            else:
                success("Source checkout is current; checking managed runtime and service.")
        elif _kind == "manual":
            pass  # no automatic path; guidance is rendered below
        elif _spec_is_moving_git(_installed_source_spec(_distribution())):
            info("Moving git channel detected; re-resolving the branch tip.")
        elif update_check.is_source_build_version(current):
            # A dev build that is *not* a git checkout: the published version
            # can't judge it, so proceed with the (best-effort) plan directly.
            info(f"Source build ({current}); skipping the published-version comparison.")
        else:
            # Published install: fetch the latest version and short-circuit if we
            # are already current (unless --force). Offline → best-effort upgrade.
            latest = update_check.fetch_latest_version(settings)
            if latest and update_check.newer_available(current, latest):
                info(f"New version available: {current} -> {latest}")
            elif latest and not force:
                package_change = False
                cmd = []
                label = f"package is current ({current}); converge managed runtime and service"
                success(f"Python package is already up to date ({current}); checking managed runtime and service.")
            elif latest:
                info(f"Already up to date ({current}); reinstalling because --force was supplied.")
            else:
                warn("Could not fetch the latest version; attempting the update directly.")

    running_daemons = list_running_daemons(settings.paths.home) if restart_serve else []
    if running_daemons:
        info(
            f"Found {len(running_daemons)} legacy per-workspace daemon(s); they will be retired "
            "(the single home service takes over channels + schedules)."
        )

    if cmd:
        console.print(
            f"Update method: [bold]{label}[/bold]\n"
            f"  [cyan]{_display_update_command(cmd)}[/cyan]"
        )
    else:
        console.print(f"Update method: [bold]{label}[/bold]")

    if _kind == "manual":
        _render_manual_update_help(current)
        if check:
            info("--check: no changes were made")
        raise typer.Exit(0 if check else 1)

    if check:
        if _kind == "git":
            info("--check: would fast-forward the checkout then reinstall; no changes were made")
        else:
            info("--check: no changes were made")
        return

    del yes  # Explicit ``omni update`` is the user's consent; retained only for compatibility.

    from omni.runtime import service_control, service_state, update_state
    from omni.runtime.daemon import stop_legacy_daemons

    daemon_detail = ""
    orphans: list[int] = []
    try:
        # The lifecycle lock spans quiescence, package replacement and
        # restoration. No bare `omni` launch can slip an old-code service into
        # the install window, and a STARTING singleton is treated as active.
        with service_control.update_guard(
            settings, restart_serve=restart_serve
        ) as service_guard:
            info("Updating; network access may be required...")
            if package_change and _kind == "git":
                checkout = _source_checkout()
                if checkout is None:
                    error("Source checkout is no longer resolvable; aborting.")
                    raise typer.Exit(1)
                repo_root, src, editable = checkout
                _execute_git_update(
                    repo_root=repo_root, src=src, editable=editable, ref=ref
                )
            elif package_change:
                try:
                    proc = _run_package_command(cmd, _kind)
                except FileNotFoundError as exc:
                    if _kind in {"uv", "pipx"}:
                        error(
                            f"This installation is owned by {_kind}, but its "
                            f"executable is unavailable ({exc}). Restore {_kind} "
                            "on PATH, then run `omni update` again; Omni will not "
                            "modify the manager-owned environment with pip."
                        )
                    else:
                        error(f"Update command unavailable: {exc}.")
                    raise typer.Exit(1) from exc
                if proc.returncode != 0:
                    error(
                        f"Update failed (exit={proc.returncode}). "
                        "Use `omni update --check` for the command."
                    )
                    raise typer.Exit(proc.returncode)
            else:
                info("No Python package download is required.")

            _prepare_bundled_skill_runtimes_with_updated_cli(settings.paths)

            # Drop the notifier cache so the next launch re-checks against the
            # new version.
            if package_change:
                update_check.clear_update_state(settings.paths)
            else:
                # A stale cached notice must not prompt again after an explicit
                # successful convergence that found no package change.
                update_check.record_latest(settings.paths, current)

            # Legacy per-workspace daemons are the old model; retire them while
            # the home-service transaction is still serialized.
            if running_daemons:
                reaped = stop_legacy_daemons(settings.paths.home)
                daemon_detail = (
                    f"retired {len(reaped)} legacy per-workspace daemon(s); "
                    "the home service owns channels + schedules now."
                )

            # A successful update is not complete until a previously active
            # service has claimed the singleton on the newly installed code.
            # Control-plane READY is preferred; IM channels may still be connecting.
            service_detail = service_guard.restore()
            update_state.record_converged(settings.paths)

            # Take the runtime/singleton/process snapshot before releasing the
            # lifecycle lock. Otherwise a bare-omni repair can legitimately
            # start between the known-PID snapshot and ps scan and be reported
            # as the very "stray" this transaction just converged.
            if restart_serve:
                orphans = _stable_untracked_serve_pids(settings)
    except typer.Exit as exc:
        if restore_error := getattr(exc, "_omni_service_restore_error", ""):
            error(str(restore_error))
        raise
    except service_state.LifecycleLockTimeout as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    except RuntimeError as exc:
        detail = str(exc)
        if restore_error := getattr(exc, "_omni_service_restore_error", ""):
            detail = f"{detail}\n{restore_error}"
        error(detail)
        raise typer.Exit(1) from exc

    _render_update_summary(
        restart_serve=restart_serve,
        daemon_detail=daemon_detail,
        daemon_count=len(running_daemons),
        orphans=orphans,
        editable=editable,
        service_detail=service_detail,
    )


@app.command("status")
def update_status_command(ctx: typer.Context) -> None:
    """Show installed, available, and locally converged update state."""
    from omni.runtime import update_check, update_state

    settings = ctx.obj.settings()
    fingerprint = update_state.current_fingerprint()
    state = update_state.read_state(settings.paths)
    converged = state.get("fingerprint")
    converged_version = (
        str(converged.get("version") or "—") if isinstance(converged, dict) else "—"
    )
    kind, _cmd, label = _plan()
    latest = "—"
    if kind not in {"git", "manual"} and fingerprint.source not in {"local", "editable"}:
        latest = update_check.fetch_latest_version(settings) or "unavailable"
    data_table(
        "OmniScientist update status",
        ["field", "value"],
        [
            ["Installed", fingerprint.version],
            ["Available", latest],
            ["Owner", fingerprint.owner],
            ["Source", fingerprint.source],
            ["Converged", converged_version],
            [
                "Needs convergence",
                str(update_state.convergence_needed(settings.paths, fingerprint)),
            ],
            ["Plan", label],
        ],
    )


def _render_update_summary(
    *,
    restart_serve: bool,
    daemon_detail: str,
    daemon_count: int,
    orphans: list[int],
    editable: bool = False,
    service_detail: str = "",
) -> None:
    """Spell out exactly what took effect after updating Omni itself."""
    success("Update completed.")
    if editable:
        info("• Editable install: pure-Python edits are live on the next launch (no reinstall needed).")
    info("• New omni commands use the new version immediately.")
    if service_detail:
        if "still becoming ready" in service_detail:
            warn(f"• Home service: {service_detail}")
        else:
            info(f"• Home service: {service_detail}")
    if daemon_count:
        info(f"• Legacy daemons: {daemon_detail}")
    if not restart_serve:
        warn("• Background serve restart was skipped; run `omni serve restart` if needed.")
    # Any already-open REPL/terminal keeps old code in-memory until relaunched.
    info("• Restart open omni sessions or terminals to load the new version.")
    info("• Restart `omni web` to load the UI that shipped with this version.")
    if orphans:
        pids = ", ".join(str(p) for p in orphans)
        warn(
            f"Found {len(orphans)} stray omni serve process(es) (pid={pids}) beyond the single home "
            "service. Run `omni serve restart` to converge on one, or `omni serve stop --all` to clear them."
        )
