from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from omni.runtime import update_state
from omni.runtime.uninstall import installation_method_for_prefix


def _fingerprint(token: str) -> update_state.InstallationFingerprint:
    return update_state.InstallationFingerprint(
        token=token,
        version="2.0.0",
        owner="uv",
        source="pypi",
        python="/tool/python",
    )


def test_missing_state_requires_convergence(settings):
    assert update_state.convergence_needed(
        settings.paths, _fingerprint("installed")
    )


def test_recorded_fingerprint_is_current_until_the_package_changes(settings):
    update_state.record_converged(settings.paths, _fingerprint("old"))

    assert not update_state.convergence_needed(settings.paths, _fingerprint("old"))
    assert update_state.convergence_needed(settings.paths, _fingerprint("new"))


def test_state_schema_change_requires_convergence(settings, monkeypatch):
    update_state.record_converged(settings.paths, _fingerprint("same-package"))
    monkeypatch.setattr(
        update_state,
        "STATE_SCHEMA_VERSION",
        update_state.STATE_SCHEMA_VERSION + 1,
    )

    assert update_state.convergence_needed(
        settings.paths, _fingerprint("same-package")
    )


def test_legacy_state_with_interpreter_path_is_rewritten_without_it(settings):
    fingerprint = _fingerprint("same-package")
    update_state.write_state(
        settings.paths,
        {
            "schema_version": 1,
            "fingerprint": fingerprint.to_dict() | {
                "python": "/Users/private-name/tool/python",
            },
        },
    )

    assert update_state.convergence_needed(settings.paths, fingerprint)
    update_state.record_converged(settings.paths, fingerprint)
    text = update_state.state_path(settings.paths).read_text(encoding="utf-8")
    assert "/Users/private-name" not in text
    assert f'"schema_version": {update_state.STATE_SCHEMA_VERSION}' in text


def test_corrupt_state_fails_closed(settings):
    path = update_state.state_path(settings.paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    assert update_state.convergence_needed(settings.paths, _fingerprint("installed"))


def test_state_does_not_store_package_metadata_or_source_credentials(settings):
    fingerprint = _fingerprint("safe-token")
    update_state.record_converged(settings.paths, fingerprint)

    text = update_state.state_path(settings.paths).read_text(encoding="utf-8")
    assert "safe-token" in text
    assert fingerprint.python not in text
    assert "direct_url" not in text
    assert "RECORD" not in text


def test_fingerprint_changes_when_package_manager_updates_a_dependency(monkeypatch):
    class _Dist:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

        def read_text(self, _name: str) -> str:
            return ""

    omni = _Dist("omniscientist", "2.0.0")
    dependency = _Dist("dependency", "1.0.0")
    monkeypatch.setattr(update_state.md, "distribution", lambda _name: omni)
    monkeypatch.setattr(
        update_state.md,
        "distributions",
        lambda: [omni, dependency],
    )
    before = update_state.current_fingerprint()

    dependency.version = "1.1.0"
    after = update_state.current_fingerprint()

    assert before.token != after.token


def test_fingerprint_does_not_depend_on_direct_url_credentials(monkeypatch):
    class _Dist:
        metadata = {"Name": "omniscientist"}
        version = "2.0.0"

        def __init__(self) -> None:
            self.token = "first-secret"

        def read_text(self, name: str) -> str:
            if name != "direct_url.json":
                return ""
            return (
                '{"url":"https://user:'
                + self.token
                + '@github.example/org/repo.git?access_token='
                + self.token
                + '","vcs_info":{"vcs":"git","commit_id":"abc"}}'
            )

    omni = _Dist()
    monkeypatch.setattr(update_state.md, "distribution", lambda _name: omni)
    monkeypatch.setattr(update_state.md, "distributions", lambda: [omni])
    before = update_state.current_fingerprint()

    omni.token = "second-secret"
    after = update_state.current_fingerprint()

    assert before.token == after.token


def test_owner_detection_supports_custom_uv_and_pipx_roots(tmp_path):
    uv_prefix = tmp_path / "custom-tools" / "omniscientist"
    uv_prefix.mkdir(parents=True)
    (uv_prefix / "uv-receipt.toml").write_text("", encoding="utf-8")
    pipx_prefix = tmp_path / "custom-apps" / "omniscientist"
    pipx_prefix.mkdir(parents=True)
    (pipx_prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")

    assert installation_method_for_prefix(uv_prefix) == "uv"
    assert installation_method_for_prefix(pipx_prefix) == "pipx"


class _Guard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self):
        self.events.append("guard-enter")
        return self

    def restore(self) -> str:
        self.events.append("service-restored")
        return "restarted"

    def __exit__(self, *_args) -> None:
        self.events.append("guard-exit")


def test_bare_launch_converges_external_package_upgrade_before_repl(
    settings, monkeypatch
):
    from omni.cli import main as cli_main
    from omni.cli.commands import update_cmd
    from omni.runtime import daemon as daemon_runtime
    from omni.runtime import service_control

    events: list[str] = []
    fingerprint = _fingerprint("externally-upgraded")
    monkeypatch.setattr(update_state, "current_fingerprint", lambda: fingerprint)
    monkeypatch.setattr(update_state, "convergence_needed", lambda *_a: True)
    monkeypatch.setattr(
        service_control,
        "update_guard",
        lambda *_a, **_k: _Guard(events),
    )
    monkeypatch.setattr(
        update_cmd,
        "_prepare_bundled_skill_runtimes",
        lambda _paths: events.append("runtime-ready"),
    )
    monkeypatch.setattr(
        daemon_runtime,
        "stop_legacy_daemons",
        lambda _home: events.append("legacy-clean") or [],
    )
    monkeypatch.setattr(
        update_state,
        "record_converged",
        lambda _paths, saved: events.append(f"recorded:{saved.token}") or saved,
    )

    cli_main._maybe_converge_installation(
        SimpleNamespace(settings=lambda: settings)
    )

    assert events == [
        "guard-enter",
        "runtime-ready",
        "legacy-clean",
        "service-restored",
        "recorded:externally-upgraded",
        "guard-exit",
    ]


def test_bare_launch_does_nothing_when_installation_is_already_converged(
    settings, monkeypatch
):
    from omni.cli import main as cli_main
    from omni.runtime import service_control

    monkeypatch.setattr(update_state, "convergence_needed", lambda *_a: False)
    monkeypatch.setattr(
        service_control,
        "update_guard",
        lambda *_a, **_k: pytest.fail("converged launch must not take update guard"),
    )

    cli_main._maybe_converge_installation(
        SimpleNamespace(settings=lambda: settings)
    )


def test_failed_bare_launch_convergence_is_retried_next_time(settings, monkeypatch):
    from omni.cli import main as cli_main
    from omni.cli.commands import update_cmd
    from omni.runtime import service_control

    fingerprint = _fingerprint("new-but-not-converged")
    monkeypatch.setattr(update_state, "current_fingerprint", lambda: fingerprint)
    monkeypatch.setattr(update_state, "convergence_needed", lambda *_a: True)
    monkeypatch.setattr(
        service_control,
        "update_guard",
        lambda *_a, **_k: _Guard([]),
    )
    monkeypatch.setattr(
        update_cmd,
        "_prepare_bundled_skill_runtimes",
        lambda _paths: (_ for _ in ()).throw(typer.Exit(1)),
    )
    monkeypatch.setattr(
        update_state,
        "record_converged",
        lambda *_a, **_k: pytest.fail("failed convergence must not be recorded"),
    )

    with pytest.raises(typer.Exit):
        cli_main._maybe_converge_installation(
            SimpleNamespace(settings=lambda: settings)
        )
