"""Owner-managed runtime setup for the bundled research-pptx renderer."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import omni.skills_runtime.runtime_setup as runtime_setup


def _write_renderer_manifests(skill_dir: Path) -> Path:
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "package.json").write_text(
        '{"name":"research-pptx-renderer","dependencies":{}}\n',
        encoding="utf-8",
    )
    (scripts / "package-lock.json").write_text(
        '{"name":"research-pptx-renderer","lockfileVersion":3}\n',
        encoding="utf-8",
    )
    return scripts


def test_research_pptx_setup_installs_locked_runtime_outside_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "site-packages" / "omni" / "data" / "skills" / "research-pptx"
    scripts = _write_renderer_manifests(skill_dir)
    paths = SimpleNamespace(cache_dir=tmp_path / "omni-cache")
    runtime_dir = runtime_setup.research_pptx_runtime_dir(paths)
    calls: list[tuple[list[str], Path]] = []

    def fake_run(argv, *, cwd, check):  # noqa: ANN001, ANN202
        calls.append((list(argv), Path(cwd)))
        for package in ("pptxgenjs", "sharp"):
            (Path(cwd) / "node_modules" / package).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_setup.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"node", "npm"} else None,
    )
    # A supported Node is present; the version probe is exercised separately.
    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: (22, 0, 0))

    # Fresh cache installs into the fixed directory and never touches the skill.
    assert runtime_setup.setup_research_pptx_runtime(paths, skill_dir=skill_dir) is True
    assert runtime_dir.is_relative_to(paths.cache_dir)
    assert runtime_dir.name == "research-pptx"
    assert (runtime_dir / "package-lock.json").read_bytes() == (
        scripts / "package-lock.json"
    ).read_bytes()
    assert calls == [(["/usr/bin/npm", "ci", "--omit=dev"], runtime_dir)]
    assert not (scripts / "node_modules").exists()

    # Idempotent: a ready cache is not reinstalled.
    assert runtime_setup.setup_research_pptx_runtime(paths, skill_dir=skill_dir) is False
    assert len(calls) == 1


def test_research_pptx_runtime_ready_reflects_installed_packages(tmp_path: Path) -> None:
    paths = SimpleNamespace(cache_dir=tmp_path / "omni-cache")
    assert runtime_setup.research_pptx_runtime_ready(paths) is False  # nothing installed yet

    node_modules = runtime_setup.research_pptx_runtime_dir(paths) / "node_modules"
    for package in ("pptxgenjs", "sharp"):
        (node_modules / package).mkdir(parents=True, exist_ok=True)
    assert runtime_setup.research_pptx_runtime_ready(paths) is True


def test_research_pptx_setup_requires_node_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "research-pptx"
    _write_renderer_manifests(skill_dir)
    paths = SimpleNamespace(cache_dir=tmp_path / "omni-cache")

    monkeypatch.setattr(runtime_setup.shutil, "which", lambda _name: None)

    with pytest.raises(runtime_setup.SkillRuntimeSetupError, match="Node.js"):
        runtime_setup.setup_research_pptx_runtime(paths, skill_dir=skill_dir)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v20.9.0", (20, 9, 0)),
        ("v20.9.0\n", (20, 9, 0)),
        ("v18.20.5", (18, 20, 5)),
        ("v22.1.0", (22, 1, 0)),
        ("v24.0.0-nightly20260101", (24, 0, 0)),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_node_version(raw: str, expected: tuple[int, int, int] | None) -> None:
    assert runtime_setup._parse_node_version(raw) == expected


def test_node_gate_allows_minimum_and_rejects_one_below(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exactly the minimum passes; one patch release below is rejected.
    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: (20, 9, 0))
    runtime_setup._require_supported_node("/usr/bin/node")

    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: (20, 8, 999))
    with pytest.raises(runtime_setup.SkillRuntimeSetupError, match="found 20.8.999"):
        runtime_setup._require_supported_node("/usr/bin/node")

    # An undeterminable version never blocks — npm stays the backstop.
    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: None)
    runtime_setup._require_supported_node("/usr/bin/node")


def test_research_pptx_setup_rejects_outdated_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale Node fails fast with a clear message before npm ever runs."""
    skill_dir = tmp_path / "research-pptx"
    _write_renderer_manifests(skill_dir)
    paths = SimpleNamespace(cache_dir=tmp_path / "omni-cache")
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_setup.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"node", "npm"} else None,
    )
    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: (18, 20, 5))

    with pytest.raises(runtime_setup.SkillRuntimeSetupError, match="Node.js >= 20.9"):
        runtime_setup.setup_research_pptx_runtime(paths, skill_dir=skill_dir)
    assert calls == [], "npm must not run when the Node gate rejects the toolchain"


def test_research_pptx_setup_proceeds_when_node_version_undeterminable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undeterminable Node version does not block; npm remains the backstop."""
    skill_dir = tmp_path / "research-pptx"
    _write_renderer_manifests(skill_dir)
    paths = SimpleNamespace(cache_dir=tmp_path / "omni-cache")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        cwd = Path(kwargs["cwd"])
        for package in ("pptxgenjs", "sharp"):
            (cwd / "node_modules" / package).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runtime_setup.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"node", "npm"} else None,
    )
    monkeypatch.setattr(runtime_setup, "_node_version", lambda _node: None)

    assert runtime_setup.setup_research_pptx_runtime(paths, skill_dir=skill_dir) is True
    assert calls == [["/usr/bin/npm", "ci", "--omit=dev"]]


def test_active_skill_python_runtimes_are_part_of_the_base_install() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    dependencies = {
        item.split(";", 1)[0].split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for item in tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    }

    assert {"matplotlib", "pymupdf", "pymupdf4llm", "python-pptx"} <= dependencies
