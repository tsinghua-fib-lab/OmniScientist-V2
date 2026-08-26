"""Compute I/O paths must be mkdir-able on every host, including Windows."""

from __future__ import annotations

from types import SimpleNamespace

from omni.skills_runtime.builtin_tools import shell
from omni.skills_runtime.exec_io import (
    compute_dir_key,
    durable_output_dir,
    harvestable_output,
)


def test_harvestable_output_skips_venv_license_and_unknown_suffixes(
    tmp_path,
) -> None:
    root = tmp_path / "outbox"
    (root / ".venv" / "lib" / "site-packages" / "pkg").mkdir(parents=True)
    license_file = root / "LICENSE"
    license_file.write_text("MIT\n")
    wheel = root / ".venv" / "lib" / "site-packages" / "pkg" / "mod.py"
    wheel.write_text("x = 1\n")
    notice = root / "NOTICE.txt"
    notice.write_text("notice\n")
    csv = root / "results.csv"
    csv.write_text("a,1\n")
    svg = root / "plot.svg"
    svg.write_text("<svg/>\n")
    pptx = root / "slide.pptx"
    pptx.write_bytes(b"PK")

    assert harvestable_output(license_file, root) is False
    assert harvestable_output(wheel, root) is False
    assert harvestable_output(notice, root) is False
    assert harvestable_output(csv, root) is True
    assert harvestable_output(svg, root) is True
    assert harvestable_output(pptx, root) is True


def test_compute_dir_key_replaces_windows_forbidden_characters() -> None:
    assert compute_dir_key("run-root::sub-a87ca759") == "run-root--sub-a87ca759"
    assert compute_dir_key("run::sub-1::sub-2") == "run--sub-1--sub-2"
    assert compute_dir_key('note<>:"/\\|?*') == "note---------"
    assert compute_dir_key("   ") == "ad-hoc"
    assert compute_dir_key("") == "ad-hoc"


def test_durable_output_dir_accepts_a_subagent_task_id(tmp_path) -> None:
    ctx = SimpleNamespace(
        paths=SimpleNamespace(artifacts_dir=tmp_path / "artifacts"),
        task_id="run-root::sub-a87ca759",
        session_id="",
    )
    dest = durable_output_dir(ctx)
    assert dest.is_dir()
    assert dest.name == "run-root--sub-a87ca759"
    assert dest.parent.name == "compute"


def test_windows_posix_shells_skip_the_wsl_launcher(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "bin" / "bash.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"")
    wsl = tmp_path / "System32" / "bash.exe"
    wsl.parent.mkdir(parents=True)
    wsl.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "x86"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shell.shutil, "which", lambda _name: str(wsl))

    found = shell._windows_posix_shells()

    assert git in found
    assert wsl not in found


def test_posix_shell_executable_uses_git_bash_on_windows(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "bin" / "bash.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"")
    monkeypatch.setattr(shell.os, "name", "nt")
    monkeypatch.setattr(shell, "_windows_posix_shells", lambda: [git])

    assert shell.posix_shell_executable() == str(git)
