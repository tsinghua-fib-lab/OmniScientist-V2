"""Installation ownership contracts for installers, update, and doctor."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = ROOT / "cli" / "scripts" / "install.sh"
ALIYUN_PYPI_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
OFFICIAL_PYPI_INDEX = "https://pypi.org/simple/"


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_uv(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    uv_bin = tmp_path / "uv-bin"
    uv_log = tmp_path / "uv.log"
    template = tmp_path / "omni-template"
    _write_executable(
        template,
        "#!/bin/bash\n"
        "# from omni.cli.main import app\n"
        'printf "%s\\n" "$*" >> "$OMNI_EXACT_LOG"\n'
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 2.0.0.dev0\\n"; fi\n',
    )
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$UV_LOG"\n'
        'if [ -n "${UV_ENV_LOG:-}" ]; then '
        'printf "%s|%s|%s\\n" "${UV_TOOL_DIR:-}" "${UV_TOOL_BIN_DIR:-}" "$*" >> "$UV_ENV_LOG"; fi\n'
        'if [ "${1:-} ${2:-}" = "tool install" ]; then\n'
        '  target_bin="${UV_TOOL_BIN_DIR:-$UV_BIN}"\n'
        '  mkdir -p "$target_bin"\n'
        '  cp "$OMNI_TEMPLATE" "$target_bin/omni"\n'
        '  chmod +x "$target_bin/omni"\n'
        "fi\n"
        'if [ "${1:-} ${2:-} ${3:-}" = "tool dir --bin" ]; then\n'
        '  printf "%s\\n" "${UV_TOOL_BIN_DIR:-$UV_BIN}"\n'
        "fi\n",
    )
    return bin_dir, uv_bin, uv_log


def _installer_env(tmp_path: Path, bin_dir: Path, uv_bin: Path, uv_log: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": os.pathsep.join((str(bin_dir), "/usr/bin", "/bin")),
        "UV_BIN": str(uv_bin),
        "UV_LOG": str(uv_log),
        "OMNI_EXACT_LOG": str(tmp_path / "installed-omni.log"),
        "OMNI_TEMPLATE": str(tmp_path / "omni-template"),
    }


def test_shell_installer_defaults_to_uv_even_with_active_environments(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    venv = tmp_path / "active-venv"
    venv_python_log = tmp_path / "active-python.log"
    _write_executable(
        venv / "bin" / "python",
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{venv_python_log}"\n'
        "exit 97\n",
    )
    env["VIRTUAL_ENV"] = str(venv)
    env["CONDA_PREFIX"] = str(tmp_path / "conda" / "base")
    env["CONDA_DEFAULT_ENV"] = "base"

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--on-conflict", "cancel"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "tool install --force" in uv_calls
    assert f"--default-index {OFFICIAL_PYPI_INDEX}" in uv_calls
    assert "--reinstall-package omniscientist" in uv_calls
    assert not venv_python_log.exists()
    exact_calls = (tmp_path / "installed-omni.log").read_text(encoding="utf-8")
    assert "--version" in exact_calls
    assert "_record-install --method uv" in exact_calls
    assert "_converge-install" in exact_calls


def test_standalone_shell_installer_defaults_to_pypi(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    standalone = tmp_path / "standalone" / "install.sh"
    standalone.parent.mkdir()
    shutil.copyfile(INSTALL_SH, standalone)

    result = subprocess.run(
        ["bash", str(standalone), "--on-conflict", "cancel"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Source: published PyPI package" in output
    install_call = next(
        line
        for line in uv_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("tool install ")
    )
    assert "omniscientist[mcp,vec,channels]" in install_call
    assert "git+" not in install_call


def test_shell_installer_allows_package_index_override(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--index-url",
            "aliyun",
            "--on-conflict",
            "cancel",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert f"--default-index {ALIYUN_PYPI_INDEX}" in uv_calls
    assert OFFICIAL_PYPI_INDEX not in uv_calls


def test_shell_installer_reads_package_index_from_environment(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    custom_index = "https://packages.example.test/simple/"
    env["OMNI_PYPI_INDEX_URL"] = custom_index

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--on-conflict", "cancel"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"--default-index {custom_index}" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_bootstraps_missing_uv_and_continues(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    uv_template = tmp_path / "uv-template"
    (bin_dir / "uv").rename(uv_template)
    curl_log = tmp_path / "curl.log"
    _write_executable(
        bin_dir / "curl",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        "cat <<'INSTALL'\n"
        "#!/bin/sh\n"
        'mkdir -p "$HOME/.local/bin"\n'
        'cp "$UV_INSTALL_TEMPLATE" "$HOME/.local/bin/uv"\n'
        'chmod +x "$HOME/.local/bin/uv"\n'
        "INSTALL\n",
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["CURL_LOG"] = str(curl_log)
    env["UV_INSTALL_TEMPLATE"] = str(uv_template)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--on-conflict", "cancel"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "https://astral.sh/uv/install.sh" in curl_log.read_text(encoding="utf-8")
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "tool install --force" in uv_calls
    assert f"--default-index {OFFICIAL_PYPI_INDEX}" in uv_calls


def test_shell_installer_rejects_explicit_conda_base_without_force(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    conda_base = tmp_path / "miniconda" / "base"
    _write_executable(conda_base / "bin" / "python", "#!/bin/sh\nexit 98\n")
    env["CONDA_PREFIX"] = str(conda_base)
    env["CONDA_DEFAULT_ENV"] = "base"
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--method", "env"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Conda base" in result.stdout + result.stderr
    assert "--force-conda-base" in result.stdout + result.stderr
    assert "tool install" not in uv_log.read_text(encoding="utf-8")


def test_shell_installer_rejects_mutable_git_refs(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--remote", "--ref", "main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "not an immutable release tag" in result.stdout + result.stderr
    assert not uv_log.exists()


def test_shell_installer_requires_a_duplicate_install_decision_when_noninteractive(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    _write_executable(
        bin_dir / "omni",
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "upgrade" in output.lower()
    assert "migrate" in output.lower()
    assert "cancel" in output.lower()
    assert "--on-conflict" in output
    assert "tool install" not in uv_log.read_text(encoding="utf-8")


def test_shell_installer_automatically_reinstalls_the_same_uv_owner(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    uv_python = tmp_path / "uv" / "tools" / "omniscientist" / "bin" / "python"
    uv_python.parent.mkdir(parents=True)
    uv_python.symlink_to("/bin/bash")
    _write_executable(
        uv_bin / "omni",
        f"#!{uv_python}\n"
        "# from omni.cli.main import app\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "upgraded in place" in output
    assert "tool install --force" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_deduplicates_uv_launcher_and_manifest_owner(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    uv_python = tmp_path / "uv" / "tools" / "omniscientist" / "bin" / "python"
    uv_python.parent.mkdir(parents=True)
    uv_python.symlink_to("/bin/bash")
    internal_launcher = uv_python.parent / "omni"
    launcher_text = (
        f"#!{uv_python}\n"
        "# from omni.cli.main import app\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n'
    )
    _write_executable(internal_launcher, launcher_text)
    uv_bin.mkdir(parents=True)
    (uv_bin / "omni").symlink_to(internal_launcher)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    omni_home = Path(env["HOME"]) / ".omni"
    omni_home.mkdir(parents=True)
    env["OMNI_HOME"] = str(omni_home)
    env["MANIFEST_OMNI"] = str(internal_launcher)
    _write_executable(
        bin_dir / "python3",
        "#!/bin/sh\n"
        'printf "%s\\n" "$MANIFEST_OMNI"\n',
    )
    (omni_home / "install.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "upgraded in place" in output
    assert "  2)" not in output
    assert "tool install --force" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_waits_for_a_pending_uninstall(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    operation_dir = tmp_path / "install-state"
    operation_dir.mkdir()
    pending = operation_dir / "uninstall.pending"
    pending.write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    env["OMNI_INSTALL_STATE_DIR"] = str(operation_dir)
    env["OMNI_INSTALL_WAIT_SECONDS"] = "2"
    remover = threading.Timer(0.3, pending.unlink)
    remover.start()
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--local", "--on-conflict", "cancel"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        remover.join()
    elapsed = time.monotonic() - started

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Waiting for the previous Omni uninstall" in output
    assert elapsed >= 0.2
    assert "tool install --force" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_times_out_an_unresponsive_old_launcher(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    _write_executable(
        bin_dir / "omni",
        "#!/bin/sh\n"
        "sleep 2\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["OMNI_INSTALL_PROBE_TIMEOUT_SECONDS"] = "1"
    started = time.monotonic()

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--on-conflict", "migrate"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "timed out" in output
    assert elapsed < 1.8
    assert "tool install --force" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_ignores_a_stale_manifest_in_preserved_omni_home(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["PATH"] = os.pathsep.join((str(bin_dir), str(bin_dir), "/usr/bin", "/bin"))
    omni_home = Path(env["HOME"]) / ".omni"
    omni_home.mkdir(parents=True)
    env["OMNI_HOME"] = str(omni_home)
    (omni_home / "config.toml").write_text("[model]\nprovider = 'mock'\n", encoding="utf-8")
    removed_tool = (
        Path(env["HOME"])
        / ".local"
        / "share"
        / "uv"
        / "tools"
        / "omniscientist"
        / "bin"
    )
    env["STALE_OMNI"] = str(removed_tool / "omni")
    _write_executable(
        bin_dir / "python3",
        "#!/bin/sh\n"
        'printf "%s\\n" "$STALE_OMNI"\n',
    )
    (omni_home / "install.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installations": [
                    {
                        "method": "uv",
                        "executable": str(removed_tool / "omni"),
                        "python": str(removed_tool / "python"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local", "--on-conflict", "cancel"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (omni_home / "config.toml").is_file()
    assert "tool install --force" in uv_log.read_text(encoding="utf-8")


def test_shell_installer_migrates_a_verified_env_copy_to_uv(tmp_path: Path) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    old_python = Path("/bin/sh")
    old_omni = bin_dir / "omni"
    _write_executable(
        old_omni,
        f"#!{old_python}\n"
        "# from omni.cli.main import app\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "migrate",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = uv_log.read_text(encoding="utf-8")
    assert "tool install --force" in calls
    assert f"pip uninstall --python {old_python} omniscientist" in calls, (
        result.stdout + result.stderr + calls
    )
    exact_calls = (tmp_path / "installed-omni.log").read_text(encoding="utf-8")
    assert "_record-install --method uv" in exact_calls


def _custom_pipx_install(tmp_path: Path, launcher_dir: Path) -> tuple[Path, Path]:
    pipx_home = tmp_path / "custom-pipx"
    prefix = pipx_home / "venvs" / "omniscientist"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/bin/bash")
    (prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    launcher = launcher_dir / "omni"
    _write_executable(
        launcher,
        f"#!{python}\n"
        "# from omni.cli.main import app\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    return pipx_home, launcher


def _custom_uv_install(tmp_path: Path, launcher_dir: Path) -> tuple[Path, Path]:
    tool_dir = tmp_path / "custom-uv-tools"
    prefix = tool_dir / "omniscientist"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/bin/bash")
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    launcher = launcher_dir / "omni"
    _write_executable(
        launcher,
        f"#!{python}\n"
        "# from omni.cli.main import app\n"
        'if [ "${1:-}" = "--version" ]; then printf "OmniScientist 1.0.0\\n"; fi\n',
    )
    return tool_dir, launcher


def test_shell_installer_upgrades_the_exact_custom_uv_registry(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    custom_bin = tmp_path / "custom-uv-bin"
    tool_dir, _launcher = _custom_uv_install(tmp_path, custom_bin)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["PATH"] = os.pathsep.join(
        (str(custom_bin), str(bin_dir), "/usr/bin", "/bin")
    )
    uv_env_log = tmp_path / "uv-env.log"
    env["UV_ENV_LOG"] = str(uv_env_log)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--local"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = uv_env_log.read_text(encoding="utf-8")
    assert f"{tool_dir}|{custom_bin}|tool install --force" in calls
    assert "upgraded in place" in result.stdout + result.stderr


def test_shell_installer_never_mutates_a_pipx_venv_as_plain_env(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    _custom_pipx_install(tmp_path, bin_dir)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "upgrade",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "owned by pipx" in output
    assert "managed venv" in output
    assert not uv_log.exists() or "pip install --python" not in uv_log.read_text(
        encoding="utf-8"
    )


def test_shell_installer_migration_uninstalls_through_the_exact_pipx_owner(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    pipx_home, old_launcher = _custom_pipx_install(tmp_path, bin_dir)
    pipx_log = tmp_path / "pipx.log"
    _write_executable(
        bin_dir / "pipx",
        "#!/bin/sh\n"
        'printf "%s|%s|%s\\n" "$PIPX_HOME" "$PIPX_BIN_DIR" "$*" >> "$PIPX_LOG"\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["PIPX_LOG"] = str(pipx_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "migrate",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert pipx_log.read_text(encoding="utf-8").strip() == (
        f"{pipx_home}|{old_launcher.parent}|uninstall omniscientist"
    )


def test_shell_installer_migration_fails_when_old_owner_cleanup_fails(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    _custom_pipx_install(tmp_path, bin_dir)
    _write_executable(bin_dir / "pipx", "#!/bin/sh\nexit 7\n")
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "migrate",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Migration is incomplete" in output
    installed_log = (tmp_path / "installed-omni.log").read_text(encoding="utf-8")
    assert "_record-install" not in installed_log
    assert "_converge-install" not in installed_log


def test_shell_installer_migration_cleans_pipx_when_launcher_path_is_reused(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    pipx_home, old_launcher = _custom_pipx_install(tmp_path, uv_bin)
    pipx_log = tmp_path / "pipx.log"
    _write_executable(
        bin_dir / "pipx",
        "#!/bin/sh\n"
        'printf "%s|%s|%s\\n" "$PIPX_HOME" "$PIPX_BIN_DIR" "$*" >> "$PIPX_LOG"\n',
    )
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["PIPX_LOG"] = str(pipx_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "migrate",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    pipx_home_seen, bin_seen, command = pipx_log.read_text(
        encoding="utf-8"
    ).strip().split("|")
    assert pipx_home_seen == str(pipx_home)
    assert bin_seen != str(old_launcher.parent)
    assert "omni-pipx-cleanup." in bin_seen
    assert command == "uninstall omniscientist"
    assert old_launcher.is_file()
    assert "_record-install --method uv" in (
        tmp_path / "installed-omni.log"
    ).read_text(encoding="utf-8")


def test_shell_installer_migration_removes_old_uv_through_its_registry(
    tmp_path: Path,
) -> None:
    bin_dir, uv_bin, uv_log = _fake_uv(tmp_path)
    custom_bin = tmp_path / "old-uv-bin"
    tool_dir, _launcher = _custom_uv_install(tmp_path, custom_bin)
    env = _installer_env(tmp_path, bin_dir, uv_bin, uv_log)
    env["PATH"] = os.pathsep.join(
        (str(custom_bin), str(bin_dir), "/usr/bin", "/bin")
    )
    uv_env_log = tmp_path / "uv-env.log"
    env["UV_ENV_LOG"] = str(uv_env_log)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--local",
            "--on-conflict",
            "migrate",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = uv_env_log.read_text(encoding="utf-8")
    assert f"{tool_dir}|{custom_bin}|tool uninstall omniscientist" in calls


def test_powershell_installer_has_the_same_ownership_guards() -> None:
    text = (ROOT / "cli" / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert '[string]$Method = "uv"' in text
    assert "ForceCondaBase" in text
    assert "OnConflict" in text
    assert "InstalledOmni" in text
    assert "& $InstalledOmni @recordArgs" in text
    assert "& omni @recordArgs" not in text
    assert "full 40-character commit hash" in text
    assert '[string]$IndexUrl = ""' in text
    assert ALIYUN_PYPI_INDEX in text
    assert "else { $OfficialPypiIndexUrl }" in text
    assert '@("--default-index", $IndexUrl)' in text
    assert '@("--index-url", $IndexUrl)' in text
    assert 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' in text
    assert "Get-OrInstallUv" in text
    assert "Assert-NativeSuccess" in text
    assert 'Assert-NativeSuccess "Installing the OmniScientist uv tool"' in text
    assert 'Assert-NativeSuccess "Installing OmniScientist into $Python"' in text
    assert "Acquire-InstallLock" in text
    assert "[System.IO.FileShare]::None" in text
    assert "Release-InstallLock" in text
    assert "Wait-PreviousUninstall" in text
    assert "Invoke-OmniVersionProbe" in text
    assert "& $InstalledOmni _converge-install" in text
    assert 'Get-PipxEnvironmentValue "PIPX_HOME"' in text
    assert 'Get-PipxEnvironmentValue "PIPX_BIN_DIR"' in text
    assert "this repository installer will not mutate its managed environment" in text
    assert "$env:PIPX_HOME = $pipxHome" in text
    assert '$env:PIPX_MAN_DIR = Join-Path $pipxHome "man"' in text
    assert "& pipx uninstall omniscientist" in text
    assert "$env:UV_TOOL_DIR = $UvToolDirOverride" in text
    assert "& uv tool uninstall omniscientist" in text
    assert "omni-pipx-cleanup-" in text
    assert "Cannot safely migrate" in text
    assert "Migration is incomplete" in text


def test_release_is_tag_driven_and_pypi_publish_uses_github_oidc() -> None:
    release_script = (ROOT / "cli" / "scripts" / "release.sh").read_text(
        encoding="utf-8"
    )
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "git push origin \"$TAG\"" in release_script
    assert "uv publish" not in release_script
    assert "PYPI_TOKEN" not in release_script
    assert "--no-publish" not in release_script
    assert "uv build --no-sources" in release_script
    assert "github.com" in release_script
    assert "pyproject.toml" in release_script
    assert "git remote get-url --all origin" in release_script
    assert "git remote get-url --push --all origin" in release_script
    assert "git ls-remote --exit-code origin" in release_script
    assert "refs/heads/$CANONICAL_RELEASE_BRANCH" in release_script

    assert 'tags: ["v*"]' in workflow
    assert "Verify tag matches the immutable package version" in workflow
    assert "github.ref_name" in workflow
    assert "Tag {actual} must exactly match package version {expected}" in workflow
    assert "uv build --no-sources cli --out-dir dist" in workflow
    assert "id-token: write" in workflow
    assert (
        "pypa/gh-action-pypi-publish@"
        "ba38be9e461d3875417946c167d0b5f3d385a247" in workflow
    )
    assert "@v4" not in workflow
    assert "@v6" not in workflow
    assert "declared release authority" in workflow
    assert "matrix:" in workflow
    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert "windows-latest" in workflow


def test_release_identity_is_the_canonical_github_repository() -> None:
    canonical_slug = "tsinghua-fib-lab/OmniScientist-V2"
    canonical_url = f"https://github.com/{canonical_slug}"
    pyproject = tomllib.loads(
        (ROOT / "cli" / "pyproject.toml").read_text(encoding="utf-8")
    )
    urls = pyproject["project"]["urls"]
    release_script = (ROOT / "cli" / "scripts" / "release.sh").read_text(
        encoding="utf-8"
    )
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    package_readme = (ROOT / "cli" / "README.md").read_text(encoding="utf-8")
    security_policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert urls == {
        "Homepage": canonical_url,
        "Documentation": f"{canonical_url}/tree/master/cli/docs",
        "Repository": canonical_url,
        "Issues": f"{canonical_url}/issues",
    }
    assert f'CANONICAL_REPOSITORY_SLUG="{canonical_slug}"' in release_script
    assert f"CANONICAL_REPOSITORY_SLUG = '{canonical_slug}'" in workflow
    assert "environment: pypi" in workflow
    assert "](../" not in package_readme
    assert "](docs/" not in package_readme
    assert canonical_url in package_readme
    assert f"{canonical_url}/security/advisories/new" in security_policy


def test_release_help_works_when_invoked_from_repository_root() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "cli" / "scripts" / "release.sh"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "scripts/release.sh --dry-run" in result.stdout


def test_release_tag_gate_executes_and_rejects_a_mismatched_tag() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
    )
    step = next(
        item
        for item in workflow["jobs"]["build"]["steps"]
        if item.get("name") == "Verify tag matches the immutable package version"
    )
    version = str(
        runpy.run_path(str(ROOT / "cli" / "src" / "omni" / "__init__.py"))[
            "__version__"
        ]
    )
    env = {**os.environ, "RELEASE_TAG": f"v{version}"}

    accepted = subprocess.run(
        step["run"],
        cwd=ROOT,
        env=env,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    rejected = subprocess.run(
        step["run"],
        cwd=ROOT,
        env={**env, "RELEASE_TAG": "v9.9.9"},
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "must exactly match package version" in rejected.stderr


def test_release_authority_gate_executes_and_rejects_a_fork() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
    )
    step = next(
        item
        for item in workflow["jobs"]["build"]["steps"]
        if item.get("name") == "Verify GitHub is the declared release authority"
    )
    env = {
        **os.environ,
        **step["env"],
        "RELEASE_REPOSITORY": "tsinghua-fib-lab/OmniScientist-V2",
    }

    accepted = subprocess.run(
        step["run"],
        cwd=ROOT,
        env=env,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    rejected = subprocess.run(
        step["run"],
        cwd=ROOT,
        env={**env, "RELEASE_REPOSITORY": "untrusted/fork"},
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "Releases are only allowed from" in rejected.stderr


def test_release_script_rejects_a_noncanonical_push_url(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cli_dir = repo / "cli"
    script_dir = cli_dir / "scripts"
    source_dir = cli_dir / "src" / "omni"
    script_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "cli" / "scripts" / "release.sh", script_dir / "release.sh")
    for legal_file in ("LICENSE", "NOTICE"):
        (repo / legal_file).write_text(legal_file, encoding="utf-8")
        (cli_dir / legal_file).write_text(legal_file, encoding="utf-8")
    (source_dir / "__init__.py").write_text(
        '__version__ = "2.0.0rc1"\n',
        encoding="utf-8",
    )
    (cli_dir / "pyproject.toml").write_text(
        '[project.urls]\n'
        'Repository = "https://github.com/tsinghua-fib-lab/OmniScientist-V2"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Omni Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/tsinghua-fib-lab/OmniScientist-V2.git",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            "git@github.com:tsinghua-fib-lab/OmniScientist-V2.git",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "--add",
            "--push",
            "origin",
            "git@github.com:untrusted/fork.git",
        ],
        check=True,
    )
    bin_dir = tmp_path / "bin"
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        'echo "release reached uv unexpectedly" >&2\n'
        "exit 97\n",
    )

    result = subprocess.run(
        ["bash", str(script_dir / "release.sh"), "--yes"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": os.pathsep.join((str(bin_dir), "/usr/bin", "/bin")),
            "LC_ALL": "C",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "push URL" in result.stderr
    assert "release reached uv unexpectedly" not in result.stderr
