"""Task-scoped Bash trust: skip later prompts, keep every other guard."""

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
    task_id: str = "task-review",
    channel: str = "cli",
    sandbox: str = "workspace-write",
    on_event=None,  # noqa: ANN001
) -> ApprovalGate:
    return ApprovalGate(
        _settings(sandbox=sandbox),
        approver=approver,
        session_allow=store,
        working_dir=working_dir,
        workspace=workspace,
        channel=channel,
        task_id=task_id,
        on_event=on_event,
    )


@pytest.mark.asyncio
async def test_bash_choices_offer_approve_all_only_on_a_live_cli_task(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, ...]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(tuple(choice.value for choice in req.choices))
        return ApprovalDecision(False, reason="test")

    store = SessionApprovalStore()
    with_task = _gate(
        store, approver, working_dir=tmp_path / "repo", workspace=tmp_path / "ws"
    )
    without_task = ApprovalGate(
        _settings(),
        approver=approver,
        session_allow=store,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        channel="cli",
    )
    im = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        channel="wechat",
    )
    await _invoke(with_task, "printf hello")
    await _invoke(
        with_task,
        "pytest -q tests/a.py",
        prefix_rule=["pytest", "-q"],
    )
    await _invoke(without_task, "printf hello")
    await _invoke(im, "printf hello")

    assert asked == [
        ("approve", "approve_all_bash", "deny"),
        ("approve", "approve_rule", "approve_all_bash", "deny"),
        ("approve", "deny"),
        ("approve", "deny"),
    ]


@pytest.mark.asyncio
async def test_approve_all_covers_later_bash_including_workspace_destructive(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
    )
    await _invoke(gate, "pytest -q")
    await _invoke(gate, "git show HEAD")
    await _invoke(gate, "git push")
    await _invoke(gate, "rm -rf build")

    assert asked == ["pytest -q"]


@pytest.mark.asyncio
async def test_approve_all_does_not_cover_other_tools_or_other_tasks(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, str]] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append((req.tool_name, req.detail))
        return ApprovalDecision(True, scope="task_bash")

    store = SessionApprovalStore()
    first = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        task_id="task-a",
    )
    second = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        task_id="task-b",
    )
    other_dir = _gate(
        store,
        approver,
        working_dir=tmp_path / "other",
        workspace=tmp_path / "ws",
        task_id="task-a",
    )

    await _invoke(first, "pytest -q")
    await _invoke(first, "git push")
    await _invoke(second, "pytest -q")
    await _invoke(other_dir, "pytest -q")

    async def run() -> dict[str, str]:
        return {"status": "ok"}

    await first.invoke("run_compute", {"command": "python job.py"}, run)
    await first.invoke(
        "write_file",
        {"path": "/outside/notes.md", "contents": "x"},
        run,
    )

    assert asked == [
        ("bash", "pytest -q"),
        ("bash", "pytest -q"),
        ("bash", "pytest -q"),
        ("write_file", "/outside/notes.md"),
    ]


@pytest.mark.asyncio
async def test_approve_all_does_not_follow_the_owner_onto_im(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(f"{req.tool_name}:{req.detail}")
        return ApprovalDecision(True, scope="task_bash")

    store = SessionApprovalStore()
    cli = _gate(
        store, approver, working_dir=tmp_path / "repo", workspace=tmp_path / "ws"
    )
    im = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        channel="wechat",
    )
    await _invoke(cli, "pytest -q")
    await _invoke(im, "pytest -q")

    assert asked == ["bash:pytest -q", "bash:pytest -q"]
    await _invoke(im, "git push")
    assert asked == ["bash:pytest -q", "bash:pytest -q", "bash:git push"]


@pytest.mark.asyncio
async def test_im_cannot_install_a_task_bash_grant(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        channel="wechat",
    )
    await _invoke(gate, "pytest -q")
    await _invoke(gate, "git push")

    assert asked == ["pytest -q", "git push"]


@pytest.mark.asyncio
async def test_approve_all_does_not_survive_a_sandbox_change(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    store = SessionApprovalStore()
    trusted = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        sandbox="workspace-write",
    )
    widened = _gate(
        store,
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        sandbox="full",
    )
    await _invoke(trusted, "pytest -q")
    await _invoke(widened, "git push")

    assert asked == ["pytest -q", "git push"]


@pytest.mark.asyncio
async def test_revoke_restores_the_prompt(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    store = SessionApprovalStore()
    gate = _gate(
        store, approver, working_dir=tmp_path / "repo", workspace=tmp_path / "ws"
    )
    await _invoke(gate, "pytest -q")
    store.revoke_task_bash("task-review")
    await _invoke(gate, "git push")

    assert asked == ["pytest -q", "git push"]


@pytest.mark.asyncio
async def test_audit_failure_approves_the_current_command_only(tmp_path: Path) -> None:
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    async def sink(kind: str, _payload: dict) -> None:
        if kind == "approval.granted":
            raise RuntimeError("audit down")

    gate = _gate(
        SessionApprovalStore(),
        approver,
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
        on_event=sink,
    )
    first = await _invoke(gate, "git push")
    await _invoke(gate, "rm -rf build")

    assert first["status"] == "ok"
    assert asked == ["git push", "rm -rf build"]


@pytest.mark.asyncio
async def test_production_audit_failure_does_not_publish_task_bash(
    tmp_path: Path,
) -> None:
    from omni.agent.interaction_lifecycle import build_approval_gate

    class _Tasks:
        async def append_event(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("db down")

        async def get_task(self, _task_id: str) -> None:
            return None

    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, scope="task_bash")

    gate = build_approval_gate(
        settings=_settings(),
        tasks=_Tasks(),
        approver=approver,
        session_allow={},
        task_id="task-review",
        channel="cli",
        session_id="s1",
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
    )
    first = await _invoke(gate, "pytest -q")
    await _invoke(gate, "git push")

    assert first["status"] == "ok"
    assert asked == ["pytest -q", "git push"]


@pytest.mark.asyncio
async def test_approve_all_persists_workspace_tools_on_the_task(
    tmp_path: Path,
) -> None:
    from omni.agent.interaction_lifecycle import build_approval_gate
    from omni.core.approval import SENSITIVE_TOOLS

    class _Task:
        status = "running"
        approved_tools: list[str] = []

    class _Tasks:
        def __init__(self) -> None:
            self.task = _Task()
            self.granted: list[list[str]] = []

        async def append_event(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get_task(self, _task_id: str) -> _Task:
            return self.task

        async def grant_tools(
            self, _task_id: str, tools: list[str], *, reason: str
        ) -> list[str]:
            self.granted.append(list(tools))
            self.task.approved_tools = sorted(
                {*(self.task.approved_tools or []), *tools}
            )
            assert reason == "task-workspace"
            return list(self.task.approved_tools)

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, scope="task_bash")

    tasks = _Tasks()
    gate = build_approval_gate(
        settings=_settings(),
        tasks=tasks,
        approver=approver,
        session_allow={},
        task_id="task-review",
        channel="cli",
        session_id="s1",
        working_dir=tmp_path / "repo",
        workspace=tmp_path / "ws",
    )
    await _invoke(gate, "pytest -q")
    await _invoke(gate, "python job.py")

    assert tasks.granted
    assert set(tasks.granted[0]) == set(SENSITIVE_TOOLS)
    assert set(tasks.task.approved_tools) == set(SENSITIVE_TOOLS)


@pytest.mark.asyncio
async def test_queued_bash_on_the_same_task_reuses_the_grant(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    asked = 0

    async def approver(_req: ApprovalRequest) -> ApprovalDecision:
        nonlocal asked
        asked += 1
        entered.set()
        await release.wait()
        return ApprovalDecision(True, scope="task_bash")

    store = SessionApprovalStore()
    first = _gate(
        store, approver, working_dir=tmp_path / "repo", workspace=tmp_path / "ws"
    )
    sibling = _gate(
        store, approver, working_dir=tmp_path / "repo", workspace=tmp_path / "ws"
    )
    one = asyncio.create_task(_invoke(first, "pytest -q"))
    await entered.wait()
    two = asyncio.create_task(_invoke(sibling, "git push"))
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(one, two)
    assert asked == 1
    assert all(result["status"] == "ok" for result in results)
