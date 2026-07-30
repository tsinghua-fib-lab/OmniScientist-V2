"""Session approval grants are useful without becoming ambient shell authority."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omni.config import load_settings
from omni.core.approval import ApprovalDecision, ApprovalGate, ApprovalRequest
from omni.core.approval_rules import SessionApprovalStore


def _settings(*, sandbox: str = "workspace-write"):
    settings = load_settings()
    settings.security.bash_sandbox = sandbox
    return settings


async def _invoke(gate: ApprovalGate, command: str, **metadata: object) -> object:
    args = {"command": command, **metadata}

    async def run() -> dict[str, object]:
        return {"status": "ok", "arguments": args}

    return await gate.invoke("bash", args, run)


def _gate(
    store: SessionApprovalStore,
    approver,
    *,
    working_dir: Path,
    workspace: Path,
    channel: str = "cli",
    sandbox: str = "workspace-write",
) -> ApprovalGate:
    return ApprovalGate(
        _settings(sandbox=sandbox),
        approver=approver,
        session_allow=store,
        working_dir=working_dir,
        workspace=workspace,
        channel=channel,
    )


@pytest.mark.asyncio
async def test_exact_session_grant_is_bound_to_the_execution_context(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    store = SessionApprovalStore()
    workspace = tmp_path / "workspace"
    first = _gate(store, approver, working_dir=tmp_path / "repo-a", workspace=workspace)
    same = _gate(store, approver, working_dir=tmp_path / "repo-a", workspace=workspace)
    other_dir = _gate(store, approver, working_dir=tmp_path / "repo-b", workspace=workspace)
    other_channel = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-a",
        workspace=workspace,
        channel="terminal-2",
    )
    other_sandbox = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-a",
        workspace=workspace,
        sandbox="full",
    )
    other_workspace = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-a",
        workspace=tmp_path / "workspace-b",
    )

    for gate in (first, same, other_dir, other_channel, other_sandbox, other_workspace):
        await _invoke(gate, "ruff check cli/src")

    assert asked == [
        "ruff check cli/src",
        "ruff check cli/src",
        "ruff check cli/src",
        "ruff check cli/src",
        "ruff check cli/src",
    ]


@pytest.mark.asyncio
async def test_exact_key_preserves_whitespace_inside_shell_arguments(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "printf '%s' 'a  b'")
    await _invoke(gate, "printf '%s' 'a b'")

    assert asked == ["printf '%s' 'a  b'", "printf '%s' 'a b'"]


@pytest.mark.asyncio
async def test_exact_key_matches_the_shell_handlers_outer_trim(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "  printf hello  ")
    await _invoke(gate, "printf hello")

    assert asked == ["printf hello"]


@pytest.mark.asyncio
async def test_pre_store_session_set_keeps_its_public_identity(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(False, reason="unexpected")

    gate = ApprovalGate(
        _settings(),
        approver=approver,
        session_allow={"bash:printf hello"},
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    result = await _invoke(gate, "printf hello")

    assert result["status"] == "ok"
    assert asked == []


@pytest.mark.asyncio
async def test_run_compute_keeps_its_pre_store_command_scope(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    gate = ApprovalGate(
        _settings(),
        approver=approver,
        session_allow=SessionApprovalStore(),
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )

    async def run() -> dict[str, str]:
        return {"status": "ok"}

    await gate.invoke(
        "run_compute", {"command": "python job.py", "timeout": 10}, run
    )
    await gate.invoke(
        "run_compute", {"command": "python job.py", "timeout": 20}, run
    )

    assert asked == ["python job.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("literal", "expanded"),
    [
        ("printf '%s' '$HOME'", 'printf \'%s\' "$HOME"'),
        ("printf '%s' '*'", "printf '%s' *"),
        (r"printf '%s' '\$HOME'", "printf '%s' $HOME"),
    ],
)
async def test_exact_grant_never_collapses_shell_source_semantics(
    tmp_path: Path, literal: str, expanded: str
) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="session")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, literal)
    await _invoke(gate, expanded)

    assert asked == [literal, expanded]


@pytest.mark.asyncio
async def test_validated_prefix_rule_covers_only_that_command_family(tmp_path: Path) -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append((req.detail, tuple(choice.value for choice in req.choices)))
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(
        gate,
        "pytest -q tests/core/test_a.py",
        prefix_rule=["pytest", "-q"],
    )
    await _invoke(gate, "pytest -q tests/core/test_b.py")
    await _invoke(gate, "ruff check cli/src")

    assert asked[0][1] == (
        "approve",
        "approve_rule",
        "deny",
    )
    assert [detail for detail, _choices in asked] == [
        "pytest -q tests/core/test_a.py",
        "ruff check cli/src",
    ]


@pytest.mark.asyncio
async def test_bash_choices_match_codex_once_rule_deny_contract(tmp_path: Path) -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append((req.detail, tuple(choice.value for choice in req.choices)))
        return ApprovalDecision(False, reason="test")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(
        gate,
        "pytest -q tests/a.py",
        prefix_rule=["pytest", "-q"],
    )
    await _invoke(gate, "printf hello")

    assert asked == [
        ("pytest -q tests/a.py", ("approve", "approve_rule", "deny")),
        ("printf hello", ("approve", "deny")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dangerous_suffix",
    ["--basetemp=/tmp/victim", "--basetemp /tmp/victim"],
)
async def test_pytest_rule_rechecks_suffix_hazards(
    tmp_path: Path, dangerous_suffix: str
) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    metadata = {"prefix_rule": ["pytest", "-q"]}
    await _invoke(gate, "pytest -q tests/a.py", **metadata)
    await _invoke(gate, f"pytest -q {dangerous_suffix}")

    assert asked == ["pytest -q tests/a.py", f"pytest -q {dangerous_suffix}"]


@pytest.mark.asyncio
async def test_granted_rule_event_records_scope_without_rule_contents(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, scope="rule", reason="test-rule")

    async def sink(kind: str, payload: dict[str, object]) -> None:
        events.append((kind, payload))

    gate = ApprovalGate(
        _settings(),
        approver=approver,
        on_event=sink,
        session_allow=SessionApprovalStore(),
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(
        gate,
        "pytest -q tests/private_name.py",
        prefix_rule=["pytest", "-q"],
    )

    payload = next(payload for kind, payload in events if kind == "approval.granted")
    assert payload["approval_scope"] == "rule-session"
    assert payload["grant_kind"] == "rule"
    assert len(str(payload["grant_fingerprint"])) == 64
    assert len(str(payload["context_fingerprint"])) == 64
    assert "rule_prefix" not in payload


@pytest.mark.asyncio
async def test_rule_grant_is_bound_to_the_execution_context(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    store = SessionApprovalStore()
    first = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-a",
        workspace=tmp_path / "workspace",
    )
    other = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-b",
        workspace=tmp_path / "workspace",
    )
    await _invoke(first, "pytest -q tests/a.py", prefix_rule=["pytest", "-q"])
    await _invoke(first, "pytest -q tests/b.py")
    await _invoke(other, "pytest -q tests/c.py", prefix_rule=["pytest", "-q"])

    assert asked == ["pytest -q tests/a.py", "pytest -q tests/c.py"]


@pytest.mark.asyncio
async def test_broad_prefix_is_rejected_but_task_rm_gets_a_host_rule(
    tmp_path: Path,
) -> None:
    choices: list[tuple[str, ...]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        choices.append(tuple(choice.value for choice in req.choices))
        return ApprovalDecision(False, reason="test")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "python -m pytest -q", prefix_rule=["python"])
    await _invoke(
        gate,
        "omni -P demo task rm deadbeef --force",
        prefix_rule=["omni", "-P", "demo", "task", "rm"],
    )

    assert choices[0] == ("approve", "deny")
    assert choices[1] == ("approve", "approve_rule", "deny")


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["rm", "delete"])
async def test_task_delete_rule_is_host_derived_and_reuses_different_ids(
    tmp_path: Path, verb: str
) -> None:
    asked: list[tuple[str, tuple[str, ...], str]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(
            (
                req.detail,
                tuple(choice.value for choice in req.choices),
                next(
                    choice.label
                    for choice in req.choices
                    if choice.value == "approve_rule"
                ),
            )
        )
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, f"omni -P demo task {verb} deadbeef --force")
    await _invoke(gate, f"omni -P demo task {verb} feedface --force")

    assert len(asked) == 1
    detail, choices, label = asked[0]
    assert detail == f"omni -P demo task {verb} deadbeef --force"
    assert choices == ("approve", "approve_rule", "deny")
    assert f"task {verb} <task-id...> --force" in label


@pytest.mark.asyncio
async def test_task_delete_rule_changes_only_ids_not_modifiers(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "omni -P demo task rm deadbeef --force")
    await _invoke(gate, "omni -P demo task rm feedface --force")
    await _invoke(gate, "omni -P demo task rm cafebabe")

    assert asked == [
        "omni -P demo task rm deadbeef --force",
        "omni -P demo task rm cafebabe",
    ]


@pytest.mark.asyncio
async def test_approve_once_does_not_authorize_the_next_task_id(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="once")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "omni -P demo task rm deadbeef --force")
    await _invoke(gate, "omni -P demo task rm feedface --force")

    assert asked == [
        "omni -P demo task rm deadbeef --force",
        "omni -P demo task rm feedface --force",
    ]


@pytest.mark.asyncio
async def test_task_delete_rule_is_bound_to_project_and_context(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    store = SessionApprovalStore()
    workspace = tmp_path / "workspace"
    first = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-a",
        workspace=workspace,
    )
    other_dir = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo-b",
        workspace=workspace,
    )
    await _invoke(first, "omni -P demo task rm deadbeef --force")
    await _invoke(first, "omni -P demo task rm feedface --force")
    await _invoke(first, "omni -P other task rm cafebabe --force")
    await _invoke(other_dir, "omni -P demo task rm 0123abcd --force")

    assert asked == [
        "omni -P demo task rm deadbeef --force",
        "omni -P other task rm cafebabe --force",
        "omni -P demo task rm 0123abcd --force",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "omni -P demo task clear --all --yes",
        "omni -P demo task prune --all --yes",
        "omni -P demo task rm --force",
        "omni -P demo task rm deadbeef --unknown",
        "uv run omni -P demo task rm deadbeef --force",
        "omni -P demo task rm deadbeef --force && echo done",
        "omni -P demo task rm $TASK_ID --force",
    ],
)
async def test_unsupported_destructive_commands_remain_once_only(
    tmp_path: Path, command: str
) -> None:
    seen: list[tuple[str, ...]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        seen.append(tuple(choice.value for choice in req.choices))
        return ApprovalDecision(False, reason="test")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, command)

    assert seen == [("approve", "deny")]


@pytest.mark.asyncio
async def test_task_delete_rule_normalizes_flags_and_redundant_stderr_merge(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "omni -Pdemo task rm deadbeef -f -y 2>&1")
    await _invoke(gate, "omni --project=demo task rm feedface --yes --force")

    assert asked == ["omni -Pdemo task rm deadbeef -f -y 2>&1"]


@pytest.mark.asyncio
async def test_multi_task_delete_preview_cannot_grant_real_delete_authority(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        choices = tuple(choice.value for choice in req.choices)
        asked.append((req.detail, choices))
        scope = "rule" if "approve_rule" in choices else "once"
        return ApprovalDecision(True, scope=scope)

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, "omni -P demo task rm deadbeef feedface --force")
    await _invoke(gate, "omni -P demo task rm cafebabe --force")

    assert asked == [
        (
            "omni -P demo task rm deadbeef feedface --force",
            ("approve", "deny"),
        ),
        (
            "omni -P demo task rm cafebabe --force",
            ("approve", "approve_rule", "deny"),
        ),
    ]


@pytest.mark.asyncio
async def test_confirmed_multi_task_delete_can_grant_the_same_real_operation(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="rule")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(
        gate,
        "omni -P demo task rm deadbeef feedface --force --yes",
    )
    await _invoke(gate, "omni -P demo task rm cafebabe --yes --force")

    assert asked == [
        "omni -P demo task rm deadbeef feedface --force --yes",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        ("git pull --ff-only", ["git", "pull"]),
        ("npm run dev -- --port 3000", ["npm", "run", "dev"]),
        (
            "omni -P demo task show deadbeef --json",
            ["omni", "-P", "demo", "task", "show"],
        ),
        ("uv run ruff check cli/src", ["uv", "run", "ruff", "check"]),
    ],
)
async def test_specific_codex_style_prefixes_are_offered(
    tmp_path: Path, command: str, prefix: list[str]
) -> None:
    seen: list[tuple[str, ...]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        seen.append(tuple(choice.value for choice in req.choices))
        return ApprovalDecision(False, reason="test")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, command, prefix_rule=prefix)

    assert "approve_rule" in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        ("npm run dev", ["npm", "run"]),
        ("npm run --silent dev", ["npm", "run", "--silent"]),
        ("uv run pytest -q", ["uv", "run"]),
        ("pytest --basetemp /tmp/victim", ["pytest", "--basetemp"]),
        ("pytest -q --basetemp=/tmp/victim", ["pytest", "-q", "--basetemp=/tmp/victim"]),
        ("pythonw script.py", ["pythonw", "script.py"]),
        ("git -c alias.x=show x HEAD", ["git", "-c"]),
        ("omni -P demo task show deadbeef", ["omni", "-P", "demo"]),
        ("sqlite3 state.db .tables", ["sqlite3", "state.db"]),
        ("FOO=bar python script.py", ["FOO=bar", "python"]),
        ("command python script.py", ["command", "python"]),
        ("GIT pull --ff-only", ["GIT", "pull"]),
        ("PyTest -q tests/a.py", ["PyTest", "-q"]),
        ("uv run PyTest -q", ["uv", "run", "PyTest", "-q"]),
        ("uv run omni task show abc", ["uv", "run", "omni"]),
        ("find . -name cache", ["find", "."]),
        ("cargo clean", ["cargo", "clean"]),
        ("git branch -D old", ["git", "branch"]),
        ("printf '%s' $HOME", ["printf", "%s"]),
        ("printf '%s' *", ["printf", "%s"]),
    ],
)
async def test_prefixes_that_leave_the_operation_open_are_not_offered(
    tmp_path: Path, command: str, prefix: list[str]
) -> None:
    seen: list[tuple[str, ...]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        seen.append(tuple(choice.value for choice in req.choices))
        return ApprovalDecision(False, reason="test")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    await _invoke(gate, command, prefix_rule=prefix)

    assert "approve_rule" not in seen[0]


@pytest.mark.asyncio
async def test_concurrent_exact_requests_queue_and_recheck_the_grant(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    asked = 0

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        nonlocal asked
        asked += 1
        entered.set()
        await release.wait()
        return ApprovalDecision(True, scope="session")

    store = SessionApprovalStore()
    gate = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    sibling_gate = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    first = asyncio.create_task(_invoke(gate, "ruff check cli/src"))
    await entered.wait()
    second = asyncio.create_task(_invoke(sibling_gate, "ruff check cli/src"))
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(first, second)
    assert asked == 1
    assert all(result["status"] == "ok" for result in results)


@pytest.mark.asyncio
async def test_different_concurrent_requests_queue_instead_of_auto_deny(tmp_path: Path) -> None:
    releases = [asyncio.Event(), asyncio.Event()]
    entered = [asyncio.Event(), asyncio.Event()]
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        index = len(asked)
        asked.append(req.detail)
        entered[index].set()
        await releases[index].wait()
        return ApprovalDecision(True, scope="once")

    store = SessionApprovalStore()
    first_gate = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    second_gate = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "workspace",
    )
    first = asyncio.create_task(_invoke(first_gate, "ruff check cli/src"))
    await entered[0].wait()
    second = asyncio.create_task(_invoke(second_gate, "pytest -q"))
    await asyncio.sleep(0)
    assert asked == ["ruff check cli/src"]

    releases[0].set()
    await entered[1].wait()
    releases[1].set()
    results = await asyncio.gather(first, second)

    assert asked == ["ruff check cli/src", "pytest -q"]
    assert all(result["status"] == "ok" for result in results)


@pytest.mark.asyncio
async def test_concurrent_task_deletes_recheck_rule_after_grant_event(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release_approver = asyncio.Event()
    granted_started = asyncio.Event()
    release_granted = asyncio.Event()
    asked = 0
    events: list[tuple[str, str]] = []

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        nonlocal asked
        asked += 1
        entered.set()
        await release_approver.wait()
        return ApprovalDecision(True, scope="rule")

    async def sink(kind: str, payload: dict[str, object]) -> None:
        if kind == "approval.granted":
            granted_started.set()
            await release_granted.wait()
        events.append((kind, str(payload.get("approval_scope") or "")))

    store = SessionApprovalStore()
    common = {
        "approver": approver,
        "on_event": sink,
        "session_allow": store,
        "working_dir": tmp_path / "repo",
        "workspace": tmp_path / "workspace",
    }
    first_gate = ApprovalGate(_settings(), **common)
    second_gate = ApprovalGate(_settings(), **common)
    first = asyncio.create_task(
        _invoke(first_gate, "omni -P demo task rm deadbeef --force")
    )
    await entered.wait()
    release_approver.set()
    await granted_started.wait()
    second = asyncio.create_task(
        _invoke(second_gate, "omni -P demo task rm feedface --force")
    )
    await asyncio.sleep(0)
    assert not any(kind == "approval.auto" for kind, _scope in events)
    release_granted.set()

    results = await asyncio.gather(first, second)
    assert asked == 1
    assert all(result["status"] == "ok" for result in results)
    granted = events.index(("approval.granted", "rule-session"))
    auto = events.index(("approval.auto", "rule-session"))
    assert granted < auto
