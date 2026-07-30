"""Install and launch AutoSOTA without absorbing its runtime into OmniScientist.

AutoSOTA remains the owner of experiment environments, GPU scheduling, and
long-running optimisation.  This module deliberately only manages a private
Node runtime, a small launcher profile, and temporary secret materialisation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import tomli_w
import yaml

from omni.config.secure_files import write_private_toml

AUTOSOTA_REPOSITORY = "tsinghua-fib-lab/AutoSOTA"
AUTOSOTA_INSTALL_COMMAND = "omni autosota get"
_METADATA_NAME = "install.json"
_PROFILE_NAME = ".omni-autosota.toml"
_CONFIG_NAME = "config.yaml"
_SECRET_KEYS = (
    "claude_api_key",
    "research_api_key",
    "openrouter_api_key",
)
_NATIVE_NON_RUN_COMMANDS = {
    "ask",
    "continue",
    "doctor",
    "init",
    "inspect",
    "list",
    "login",
    "ls",
    "pause",
    "sessions",
    "status",
    "steer",
}


class AutosotaError(RuntimeError):
    """AutoSOTA could not be installed, configured, or started safely."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """One downloadable, official AutoSOTA npm package release."""

    version: str
    url: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AutosotaInstallResult:
    """The active private AutoSOTA installation."""

    version: str
    runtime_dir: Path
    executable: Path
    changed: bool


@dataclass(frozen=True, slots=True)
class WorkspaceConfiguration:
    """Non-secret launcher preferences for one AutoSOTA workspace."""

    workspace: Path
    repo_path: Path | None = None
    devices: str = ""
    eval_command: str = ""
    primary_metric: str = ""
    metric_direction: str = ""
    baseline: str = ""
    max_iterations: int | None = None
    max_total_hours: float | None = None
    protected_paths: tuple[str, ...] = ()
    claude_base_url: str = ""
    claude_model: str = ""
    research_base_url: str = ""
    research_model: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceConfigurationResult:
    """Locations affected by a workspace configuration update."""

    workspace: Path
    profile_path: Path
    config_path: Path
    secrets_saved: bool
    config_updated: bool


def autosota_root(paths: Any) -> Path:
    """Return AutoSOTA's owner-managed cache root under the Omni home."""
    return Path(paths.cache_dir) / "runtimes" / "autosota"


def metadata_path(paths: Any) -> Path:
    """Return the non-secret active-install metadata path."""
    return autosota_root(paths) / _METADATA_NAME


def _runtime_dir(paths: Any, version: str, digest: str) -> Path:
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", version).strip(".-") or "release"
    return autosota_root(paths) / "versions" / f"{safe_version}-{digest[:12]}"


def _executable_in(runtime_dir: Path) -> Path:
    bin_dir = runtime_dir / "node_modules" / ".bin"
    candidates = (
        ("autosota.cmd", "autosota.exe", "autosota")
        if os.name == "nt"
        else ("autosota",)
    )
    for name in candidates:
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return bin_dir / candidates[0]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutosotaError(f"AutoSOTA installation metadata is invalid: {path}") from exc
    if not isinstance(raw, dict):
        raise AutosotaError(f"AutoSOTA installation metadata is invalid: {path}")
    return raw


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def active_install(paths: Any) -> AutosotaInstallResult | None:
    """Return the currently selected private runtime, if it is complete."""
    payload = _read_json(metadata_path(paths))
    if not payload:
        return None
    try:
        version = str(payload["version"])
        runtime_dir = Path(str(payload["runtime_dir"])).resolve()
        executable = Path(str(payload["executable"])).resolve()
    except (KeyError, OSError) as exc:
        raise AutosotaError("AutoSOTA installation metadata is incomplete; run `omni autosota get --force`.") from exc
    root = autosota_root(paths).resolve()
    if root not in runtime_dir.parents or root not in executable.parents:
        raise AutosotaError("AutoSOTA installation metadata points outside the Omni runtime cache.")
    if not executable.is_file():
        return None
    return AutosotaInstallResult(version, runtime_dir, executable, False)


def require_active_install(paths: Any) -> AutosotaInstallResult:
    """Return a verified installation or a focused recovery instruction."""
    installation = active_install(paths)
    if installation is None:
        raise AutosotaError(
            "AutoSOTA is not installed. Run `omni autosota get` to install an official release."
        )
    return installation


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "OmniScientist"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API URL only.
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise AutosotaError(f"Could not query the official AutoSOTA release: {exc}") from exc
    if not isinstance(payload, dict):
        raise AutosotaError("The official AutoSOTA release response was malformed.")
    return payload


def _latest_release_tag_from_page() -> str:
    """Resolve the latest tag without consuming GitHub's REST API quota.

    GitHub's ``/releases/latest`` endpoint redirects to a stable release page.
    It is intentionally used only as a fallback when the richer Releases API
    is unavailable (for example, behind a shared rate-limited egress IP).
    """
    url = f"https://github.com/{AUTOSOTA_REPOSITORY}/releases/latest"
    request = Request(url, headers={"User-Agent": "OmniScientist"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub URL only.
            final_url = response.geturl()
    except OSError as exc:
        raise AutosotaError(f"Could not resolve the latest AutoSOTA release page: {exc}") from exc
    match = re.search(r"/releases/tag/([^/?#]+)$", final_url)
    if not match:
        raise AutosotaError("The latest AutoSOTA release page did not resolve to a release tag.")
    return match.group(1)


def _conventional_release_asset(tag_name: str) -> ReleaseAsset:
    """Return AutoSOTA's documented npm-archive URL for a release tag."""
    normalized_tag = tag_name.strip()
    if not normalized_tag:
        raise AutosotaError("The official AutoSOTA release tag was empty.")
    version = normalized_tag[1:] if normalized_tag.startswith("v") else normalized_tag
    if not version:
        raise AutosotaError("The official AutoSOTA release tag was invalid.")
    return ReleaseAsset(
        normalized_tag,
        "https://github.com/"
        f"{AUTOSOTA_REPOSITORY}/releases/download/{quote(normalized_tag, safe='')}/"
        f"autosota-{quote(version, safe='')}.tgz",
    )


def official_release_asset(version: str = "latest") -> ReleaseAsset:
    """Resolve a .tgz asset from the official GitHub Releases API.

    When the API is temporarily rate limited, fall back to the project's
    conventional archive name and GitHub's redirecting latest-release page.
    The package is still downloaded exclusively from the official repository.
    """
    normalized = version.strip()
    if not normalized or normalized.lower() == "latest":
        endpoint = f"https://api.github.com/repos/{AUTOSOTA_REPOSITORY}/releases/latest"
    else:
        tag = normalized if normalized.startswith("v") else f"v{normalized}"
        endpoint = f"https://api.github.com/repos/{AUTOSOTA_REPOSITORY}/releases/tags/{tag}"
    try:
        release = _fetch_json(endpoint)
    except AutosotaError as api_error:
        try:
            tag_name = _latest_release_tag_from_page() if endpoint.endswith("/latest") else tag
            return _conventional_release_asset(tag_name)
        except AutosotaError as fallback_error:
            raise AutosotaError(
                "Could not resolve the official AutoSOTA release through either GitHub endpoint: "
                f"API: {api_error}; fallback: {fallback_error}"
            ) from fallback_error
    tag_name = str(release.get("tag_name") or "").strip()
    assets = release.get("assets")
    if not tag_name or not isinstance(assets, list):
        raise AutosotaError("The official AutoSOTA release is missing its tag or assets.")
    packages = [
        asset for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name") or "").lower().endswith(".tgz")
        and str(asset.get("browser_download_url") or "").startswith("https://")
    ]
    if len(packages) != 1:
        names = ", ".join(str(asset.get("name") or "unknown") for asset in assets if isinstance(asset, dict))
        raise AutosotaError(
            "Could not uniquely identify the AutoSOTA .tgz release asset. "
            f"Release assets: {names or 'none'}."
        )
    asset = packages[0]
    digest = str(asset.get("digest") or "")
    sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
    return ReleaseAsset(tag_name, str(asset["browser_download_url"]), sha256)


def _download(url: str, destination: Path) -> str:
    request = Request(url, headers={"User-Agent": "OmniScientist"})
    digest = hashlib.sha256()
    try:
        with urlopen(request, timeout=90) as response, destination.open("wb") as output:  # noqa: S310 - release URL validated above.
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise AutosotaError(f"Could not download the official AutoSOTA package: {exc}") from exc
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise AutosotaError(f"Could not read AutoSOTA package: {path}") from exc
    return digest.hexdigest()


def _check_requirements(
    *,
    which: Callable[[str], str | None],
    run: Callable[..., Any],
) -> tuple[str, str]:
    node = which("node") or which("node.exe")
    npm = which("npm") or which("npm.cmd")
    missing = [name for name, value in (("Node.js >= 18", node), ("npm", npm), ("git", which("git")), ("bash", which("bash"))) if not value]
    if missing:
        raise AutosotaError("AutoSOTA requires " + ", ".join(missing) + ".")
    try:
        completed = run([str(node), "--version"], check=False, capture_output=True, text=True)
    except OSError as exc:
        raise AutosotaError(f"Could not start Node.js: {exc}") from exc
    version_text = f"{getattr(completed, 'stdout', '')}\n{getattr(completed, 'stderr', '')}"
    match = re.search(r"v?(\d+)(?:\.\d+){0,2}", version_text)
    if int(getattr(completed, "returncode", 1)) != 0 or not match or int(match.group(1)) < 18:
        raise AutosotaError("AutoSOTA requires Node.js >= 18. Install a newer Node.js, then retry.")
    return str(node), str(npm)


def install_release(
    paths: Any,
    *,
    version: str = "latest",
    package: Path | None = None,
    force: bool = False,
    release_resolver: Callable[[str], ReleaseAsset] = official_release_asset,
    downloader: Callable[[str, Path], str] = _download,
    run: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> AutosotaInstallResult:
    """Install an official release into an isolated, versioned npm prefix.

    A local ``package`` is intentionally supported for air-gapped deployments
    and tests; it is never used implicitly.  Normal use resolves only the
    project-owned GitHub Release asset.
    """
    _, npm = _check_requirements(which=which, run=run)
    if package is not None:
        package = package.expanduser().resolve()
        if not package.is_file() or package.suffix.lower() != ".tgz":
            raise AutosotaError(f"Local AutoSOTA package must be an existing .tgz file: {package}")
        source_digest = _file_sha256(package)
        asset = ReleaseAsset(f"local-{source_digest[:12]}", package.as_uri(), source_digest)
    else:
        asset = release_resolver(version)
        source_digest = ""

    existing = active_install(paths)
    if (
        not force
        and existing is not None
        and existing.version == asset.version
        and _read_json(metadata_path(paths)).get("source_url") == asset.url
    ):
        return existing

    root = autosota_root(paths)
    staging_parent = root / "staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=staging_parent))
    try:
        artifact = staging / "autosota.tgz"
        if package is not None:
            shutil.copyfile(package, artifact)
        else:
            source_digest = downloader(asset.url, artifact)
        if asset.sha256 and source_digest.lower() != asset.sha256.lower():
            raise AutosotaError("The AutoSOTA package checksum did not match the official release metadata.")

        completed = run(
            [npm, "--prefix", str(staging), "install", "--omit=dev", "--no-save", str(artifact)],
            check=False,
            capture_output=True,
            text=True,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            raise AutosotaError(
                "AutoSOTA npm installation failed. Retry with `omni autosota get --force`; "
                "the existing installation was left unchanged."
            )
        executable = _executable_in(staging)
        if not executable.is_file():
            raise AutosotaError("The AutoSOTA package installed without its `autosota` executable.")
        verified = run([str(executable), "--version"], check=False, capture_output=True, text=True)
        if int(getattr(verified, "returncode", 1)) != 0:
            raise AutosotaError("The installed AutoSOTA executable did not pass `--version` verification.")

        runtime_dir = _runtime_dir(paths, asset.version, source_digest)
        if runtime_dir.exists():
            runtime_dir = runtime_dir.with_name(f"{runtime_dir.name}-{uuid.uuid4().hex[:8]}")
        runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(runtime_dir)
        executable = _executable_in(runtime_dir)
        _write_json(
            metadata_path(paths),
            {
                "version": asset.version,
                "source_url": asset.url,
                "sha256": source_digest,
                "runtime_dir": str(runtime_dir),
                "executable": str(executable),
                "installed_at": datetime.now(UTC).isoformat(),
            },
        )
        return AutosotaInstallResult(asset.version, runtime_dir, executable, True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _workspace_identity(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AutosotaError(f"AutoSOTA configuration file is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise AutosotaError(f"AutoSOTA configuration file is invalid: {path}")
    return payload


def _write_public_toml(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise AutosotaError(f"Refusing to write AutoSOTA profile through a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as output:
        tomli_w.dump(payload, output)
    temporary.replace(path)


def _read_workspace_secrets(paths: Any, workspace: Path) -> dict[str, str]:
    payload = _read_toml(Path(paths.secrets_file))
    autosota = payload.get("autosota")
    workspaces = autosota.get("workspaces") if isinstance(autosota, dict) else None
    stored = workspaces.get(_workspace_identity(workspace)) if isinstance(workspaces, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {key: str(value) for key, value in stored.items() if key in _SECRET_KEYS and str(value)}


def _save_workspace_secrets(paths: Any, workspace: Path, secrets: dict[str, str]) -> bool:
    clean = {key: value for key, value in secrets.items() if key in _SECRET_KEYS and value}
    if not clean:
        return False
    target = Path(paths.secrets_file)
    payload = _read_toml(target)
    autosota = payload.setdefault("autosota", {})
    if not isinstance(autosota, dict):
        raise AutosotaError("Cannot store AutoSOTA secrets because [autosota] is not a TOML table.")
    workspaces = autosota.setdefault("workspaces", {})
    if not isinstance(workspaces, dict):
        raise AutosotaError("Cannot store AutoSOTA secrets because [autosota.workspaces] is not a TOML table.")
    entry = workspaces.setdefault(_workspace_identity(workspace), {})
    if not isinstance(entry, dict):
        raise AutosotaError("Cannot store AutoSOTA secrets because its workspace entry is not a TOML table.")
    entry.update(clean)
    write_private_toml(target, payload)
    return True


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise AutosotaError(f"Refusing to read AutoSOTA config through a symlink: {path}")
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AutosotaError(f"AutoSOTA YAML configuration is invalid: {path}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise AutosotaError(f"AutoSOTA YAML configuration must be a mapping: {path}")
    return payload


def _write_yaml(path: Path, payload: dict[str, Any], *, mode: int | None = None) -> None:
    if path.is_symlink():
        raise AutosotaError(f"Refusing to write AutoSOTA config through a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if mode is not None and os.name != "nt":
        os.chmod(temporary, mode)
    temporary.replace(path)
    if mode is not None and os.name != "nt":
        os.chmod(path, mode)


def workspace_profile_path(workspace: Path) -> Path:
    """Return the public, workspace-local Omni launcher profile."""
    return workspace.resolve() / _PROFILE_NAME


def load_workspace_profile(workspace: Path) -> dict[str, Any]:
    """Load public launcher preferences without exposing secrets."""
    return _read_toml(workspace_profile_path(workspace))


def configure_workspace(
    paths: Any,
    configuration: WorkspaceConfiguration,
    *,
    secrets: dict[str, str] | None = None,
    force: bool = False,
) -> WorkspaceConfigurationResult:
    """Save public AutoSOTA settings and keep provider keys in Omni secrets.

    Existing AutoSOTA ``config.yaml`` files are not reserialised unless
    ``force`` is explicit.  This keeps comments and native-only settings owned
    by AutoSOTA intact.
    """
    workspace = configuration.workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if configuration.repo_path is not None and not configuration.repo_path.expanduser().resolve().is_dir():
        raise AutosotaError(f"AutoSOTA target repository is not a directory: {configuration.repo_path}")
    if configuration.metric_direction and configuration.metric_direction not in {"maximize", "minimize"}:
        raise AutosotaError("Metric direction must be `maximize` or `minimize`.")

    profile_path = workspace_profile_path(workspace)
    profile = load_workspace_profile(workspace)
    launcher = profile.setdefault("launcher", {})
    if not isinstance(launcher, dict):
        raise AutosotaError(f"AutoSOTA launcher profile is invalid: {profile_path}")
    profile_updates: dict[str, Any] = {
        "repo_path": str(configuration.repo_path.expanduser().resolve()) if configuration.repo_path else "",
        "devices": configuration.devices,
        "eval_command": configuration.eval_command,
        "primary_metric": configuration.primary_metric,
        "metric_direction": configuration.metric_direction,
        "baseline": configuration.baseline,
        "max_iterations": configuration.max_iterations,
        "max_total_hours": configuration.max_total_hours,
        "protected_paths": list(configuration.protected_paths),
    }
    launcher.update({key: value for key, value in profile_updates.items() if value not in (None, "", [])})
    _write_public_toml(profile_path, profile)

    config_path = workspace / _CONFIG_NAME
    public_model_values = {
        "claude_base_url": configuration.claude_base_url,
        "claude_model": configuration.claude_model,
        "research_base_url": configuration.research_base_url,
        "research_model": configuration.research_model,
    }
    config_updated = False
    if not config_path.exists() or force:
        yaml_payload = _read_yaml_mapping(config_path) if config_path.exists() else {}
        yaml_payload.update({key: value for key, value in public_model_values.items() if value})
        _write_yaml(config_path, yaml_payload)
        config_updated = True

    secrets_saved = _save_workspace_secrets(paths, workspace, secrets or {})
    return WorkspaceConfigurationResult(workspace, profile_path, config_path, secrets_saved, config_updated)


def _safe_paper_name(paper: str) -> str:
    normalized = paper.strip()
    if not normalized or normalized in {".", ".."} or Path(normalized).name != normalized:
        raise AutosotaError("AutoSOTA paper name must be one safe path component.")
    return normalized


def _baseline_value(raw: Any) -> float | str:
    value = str(raw).strip()
    try:
        return float(value)
    except ValueError:
        return value


def _eval_command_file(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    for token in tokens:
        if token.endswith((".py", ".sh")):
            return token
    return ""


def native_paper_config_path(workspace: Path, paper: str) -> Path:
    """Return AutoSOTA's project-level configuration path for one paper."""
    return workspace.expanduser().resolve() / ".autosota" / "papers" / _safe_paper_name(paper) / _CONFIG_NAME


def prepare_native_paper_config(workspace: Path, paper: str) -> Path:
    """Materialize launcher policy into AutoSOTA's non-secret paper config.

    Native AutoSOTA onboarding writes the provider key into its generated
    project configuration before immediately starting optimization.  Omni's
    production-safe path instead derives the fields already supplied through
    ``omni autosota config``, writes no credentials, and lets callers launch
    with ``--skip-onboard``.
    """
    workspace = workspace.expanduser().resolve()
    profile = launcher_defaults(workspace)
    required = {
        "repo_path": profile.get("repo_path"),
        "eval_command": profile.get("eval_command"),
        "primary_metric": profile.get("primary_metric"),
        "metric_direction": profile.get("metric_direction"),
        "baseline": profile.get("baseline"),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise AutosotaError(
            "AutoSOTA launcher profile is incomplete for safe preparation; missing: "
            + ", ".join(missing)
            + ". Run `omni autosota config` with these fields first."
        )
    repo_path = Path(str(required["repo_path"])).expanduser().resolve()
    if not repo_path.is_dir():
        raise AutosotaError(f"AutoSOTA target repository is not a directory: {repo_path}")

    config_path = native_paper_config_path(workspace, paper)
    payload = _read_yaml_mapping(config_path)
    primary_metric = str(required["primary_metric"])
    metric_direction = str(required["metric_direction"])
    if metric_direction not in {"maximize", "minimize"}:
        raise AutosotaError("Metric direction must be `maximize` or `minimize`.")
    eval_command = str(required["eval_command"])
    payload.update(
        {
            "paper_title": str(payload.get("paper_title") or _safe_paper_name(paper)),
            "paper_repo_url": str(payload.get("paper_repo_url") or ""),
            "repo_path": str(repo_path),
            "venv_path": str(payload.get("venv_path") or ""),
            "env_vars": str(payload.get("env_vars") or ""),
            "eval_command": eval_command,
            "eval_command_file": _eval_command_file(eval_command),
            "eval_timeout_minutes": int(payload.get("eval_timeout_minutes") or 30),
            "gpu_devices": str(profile.get("devices") or ""),
            "baseline_metrics": {
                **(payload.get("baseline_metrics") if isinstance(payload.get("baseline_metrics"), dict) else {}),
                primary_metric: _baseline_value(required["baseline"]),
            },
            "primary_metric": primary_metric,
            "metric_direction": "higher" if metric_direction == "maximize" else "lower",
            "max_iterations": int(profile.get("max_iterations") or payload.get("max_iterations") or 1),
            "max_debug_attempts": int(payload.get("max_debug_attempts") or 3),
            "max_debug_minutes": int(payload.get("max_debug_minutes") or 15),
            "protected_paths": [str(path) for path in profile.get("protected_paths", []) if str(path)],
            "openrouter_api_key": "",
            "research_api_key": "",
            "claude_api_key": "",
        }
    )
    _write_yaml(config_path, payload)
    return config_path


def _paper_name_from_native_args(args: list[str]) -> str | None:
    if not args or args[0].startswith("-") or args[0] in _NATIVE_NON_RUN_COMMANDS:
        return None
    return _safe_paper_name(args[0])


def _scrub_native_paper_secrets(workspace: Path) -> None:
    papers_root = workspace / ".autosota" / "papers"
    if not papers_root.is_dir():
        return
    for config_path in papers_root.glob("*/config.yaml"):
        payload = _read_yaml_mapping(config_path)
        changed = False
        for key in _SECRET_KEYS:
            if payload.get(key):
                payload[key] = ""
                changed = True
        if changed:
            _write_yaml(config_path, payload)


@contextmanager
def materialized_workspace_secrets(
    paths: Any,
    workspace: Path,
    *,
    secret_keys: tuple[str, ...] = _SECRET_KEYS,
) -> Iterator[None]:
    """Temporarily add stored keys to AutoSOTA's workspace config.yaml.

    The original byte sequence and permissions are restored even when the
    native process exits unsuccessfully or is interrupted.  The wrapper never
    prints the generated file or any key value.
    """
    workspace = workspace.expanduser().resolve()
    config_path = workspace / _CONFIG_NAME
    secrets = {
        key: value
        for key, value in _read_workspace_secrets(paths, workspace).items()
        if key in secret_keys
    }
    if not secrets or not config_path.exists():
        yield
        return
    if config_path.is_symlink():
        raise AutosotaError(f"Refusing to materialize secrets through a symlink: {config_path}")
    try:
        original = config_path.read_bytes()
        original_mode = stat.S_IMODE(config_path.stat().st_mode)
    except OSError as exc:
        raise AutosotaError(f"Could not prepare AutoSOTA workspace configuration: {exc}") from exc
    payload = _read_yaml_mapping(config_path)
    payload.update(secrets)
    _write_yaml(config_path, payload, mode=0o600)
    try:
        yield
    finally:
        temporary = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.restore")
        try:
            temporary.write_bytes(original)
            if os.name != "nt":
                os.chmod(temporary, original_mode)
            temporary.replace(config_path)
            if os.name != "nt":
                os.chmod(config_path, original_mode)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass


def run_native(
    paths: Any,
    *,
    workspace: Path,
    args: list[str],
    materialize_secrets: bool = True,
    run: Callable[..., Any] = subprocess.run,
) -> int:
    """Run the external CLI in the foreground and return its exit status."""
    installation = require_active_install(paths)
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise AutosotaError(f"AutoSOTA workspace is not a directory: {workspace}")
    if "--api-key" in args:
        raise AutosotaError(
            "Refusing `--api-key` passthrough because native AutoSOTA exposes it in process arguments. "
            "Store provider keys with `omni autosota config` instead."
        )
    paper = _paper_name_from_native_args(args)
    dry_run = "--dry-run" in args
    if paper is not None:
        native_config = native_paper_config_path(workspace, paper)
        stored_secrets = _read_workspace_secrets(paths, workspace)
        launcher = launcher_defaults(workspace)
        if native_config.exists():
            prepare_native_paper_config(workspace, paper)
        elif not dry_run and (stored_secrets or launcher.get("protected_paths")):
            raise AutosotaError(
                "Safe AutoSOTA onboarding is required before this model-backed run. "
                f"Run `omni autosota prepare {paper} --workspace {workspace}`, then retry with `--skip-onboard`."
            )
    secret_keys = () if dry_run else _SECRET_KEYS
    manager = (
        materialized_workspace_secrets(paths, workspace, secret_keys=secret_keys)
        if materialize_secrets
        else _null_context()
    )
    environment = os.environ.copy()
    bundled_bin_path = installation.runtime_dir / "node_modules" / ".bin"
    bundled_bin = str(bundled_bin_path if bundled_bin_path.is_dir() else installation.executable.parent)
    inherited_path = environment.get("PATH", "")
    environment["PATH"] = bundled_bin if not inherited_path else bundled_bin + os.pathsep + inherited_path
    try:
        with manager:
            try:
                completed = run(
                    [str(installation.executable), *args],
                    cwd=workspace,
                    check=False,
                    env=environment,
                )
            except OSError as exc:
                raise AutosotaError(f"Could not start AutoSOTA: {exc}") from exc
    finally:
        _scrub_native_paper_secrets(workspace)
    return int(getattr(completed, "returncode", 1))


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def launcher_defaults(workspace: Path) -> dict[str, Any]:
    """Return the public defaults understood by the thin `omni autosota run` wrapper."""
    profile = load_workspace_profile(workspace)
    launcher = profile.get("launcher")
    return dict(launcher) if isinstance(launcher, dict) else {}


__all__ = [
    "AUTOSOTA_INSTALL_COMMAND",
    "AUTOSOTA_REPOSITORY",
    "AutosotaError",
    "AutosotaInstallResult",
    "ReleaseAsset",
    "WorkspaceConfiguration",
    "WorkspaceConfigurationResult",
    "active_install",
    "autosota_root",
    "configure_workspace",
    "install_release",
    "launcher_defaults",
    "materialized_workspace_secrets",
    "metadata_path",
    "native_paper_config_path",
    "official_release_asset",
    "prepare_native_paper_config",
    "require_active_install",
    "run_native",
    "workspace_profile_path",
]
