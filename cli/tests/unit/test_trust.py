"""Per-directory workspace trust: store, gate, settings gating, key protection."""

from __future__ import annotations

from pathlib import Path

from omni.cli.state import AppState, resolve_workspace_trust
from omni.config import trust as trustmod
from omni.config.paths import user_home
from omni.config.settings import load_settings


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


# ── store: keying, inheritance, allowlist, revoke ──────────────────────────
def test_trust_is_keyed_on_vcs_root_and_inherits_down(tmp_path):
    home = user_home()
    repo = _repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    assert not trustmod.is_trusted(sub, home=home)
    key = trustmod.set_trusted(sub, home=home)
    # keyed on the git root, not the exact launch dir
    assert key == repo.resolve()
    # inherits downward: the root and any child are now trusted
    assert trustmod.is_trusted(repo, home=home)
    assert trustmod.is_trusted(sub, home=home)


def test_trust_does_not_inherit_upward(tmp_path):
    home = user_home()
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    # no VCS markers → keyed on the exact dir
    trustmod.set_trusted(child, home=home)
    assert trustmod.is_trusted(child, home=home)
    assert not trustmod.is_trusted(parent, home=home)


def test_allowlist_trusts_subtree_without_persisting(tmp_path):
    home = user_home()
    repo = _repo(tmp_path)
    assert trustmod.is_trusted(repo / "x", home=home, allow=[str(tmp_path)])
    assert trustmod.list_trusted(home) == []  # allowlist is not persisted


def test_revoke_removes_trust(tmp_path):
    home = user_home()
    repo = _repo(tmp_path)
    trustmod.set_trusted(repo, home=home)
    assert trustmod.revoke(str(repo), home=home)
    assert not trustmod.is_trusted(repo, home=home)
    assert not trustmod.revoke(str(repo), home=home)  # already gone


def test_trust_store_lives_in_home_not_repo(tmp_path):
    home = user_home()
    repo = _repo(tmp_path)
    trustmod.set_trusted(repo, home=home)
    assert (home / "trust.json").is_file()
    assert not (repo / "trust.json").exists()
    assert not (repo / ".omni").exists()  # B: no in-place adoption on trust


# ── settings gating ────────────────────────────────────────────────────────
def test_untrusted_skips_project_config_layer(tmp_path):
    # A named project's project.toml must not apply when the launch is untrusted.
    from omni.config.paths import get_paths

    paths = get_paths(project="proj")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    paths.project_config.write_text("[display]\nui_mode = 'tui'\n", encoding="utf-8")

    trusted = load_settings(project="proj", trusted=True)
    untrusted = load_settings(project="proj", trusted=False)
    assert trusted.display.ui_mode == "tui"       # applied when trusted
    assert untrusted.display.ui_mode != "tui"      # skipped when untrusted


def test_mirror_switch_requires_trust(tmp_path):
    repo = _repo(tmp_path)
    assert load_settings(cwd=repo, trusted=True).artifacts.mirror_outputs is True
    assert load_settings(cwd=repo, trusted=False).artifacts.mirror_outputs is False
    assert load_settings(cwd=repo, trusted=None).artifacts.mirror_outputs is False


def test_project_config_cannot_self_trust_or_redirect_output(tmp_path):
    from omni.config.paths import get_paths

    paths = get_paths(project="proj")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    paths.project_config.write_text(
        "[trust]\nallow = ['/etc']\n[artifacts]\noutput_dir = '/tmp/evil'\n",
        encoding="utf-8",
    )
    s = load_settings(project="proj", trusted=True)
    assert s.trust.allow == []            # trust.* stripped from project layer
    assert s.artifacts.output_dir == "."   # artifacts.output_dir stripped


# ── gate decision (resolve_workspace_trust) ────────────────────────────────
def test_gate_named_project_is_trusted(tmp_path):
    state = AppState(project="proj")
    assert resolve_workspace_trust(state, interactive=False) is True


def test_gate_noninteractive_untrusted_is_restricted(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    state = AppState()
    assert resolve_workspace_trust(state, interactive=False) is False


def test_gate_trust_flag_grants_and_persists(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    state = AppState(trust_flag=True)
    assert resolve_workspace_trust(state, interactive=False) is True
    assert trustmod.is_trusted(repo, home=user_home())


def test_gate_no_trust_flag_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    state = AppState(trust_flag=False)
    assert resolve_workspace_trust(state, interactive=True) is False


def test_gate_in_place_omni_is_trusted(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".omni").mkdir()
    monkeypatch.chdir(repo)
    state = AppState()
    assert resolve_workspace_trust(state, interactive=False) is True


def test_gate_interactive_prompt_accept(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("omni.cli.render.confirm", lambda *a, **k: True)
    state = AppState()
    assert resolve_workspace_trust(state, interactive=True) is True
    assert trustmod.is_trusted(repo, home=user_home())


def test_gate_interactive_prompt_decline(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("omni.cli.render.confirm", lambda *a, **k: False)
    state = AppState()
    assert resolve_workspace_trust(state, interactive=True) is False
    assert not trustmod.is_trusted(repo, home=user_home())


def test_gate_disabled_is_always_trusted(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    home = user_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[trust]\nenabled = false\n", encoding="utf-8")
    state = AppState()
    assert resolve_workspace_trust(state, interactive=False) is True
