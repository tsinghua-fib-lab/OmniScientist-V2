"""What a turn with nobody watching is still allowed to produce.

Re-running task aac5b285 through `omni exec` reached the last step and stopped
there: `write_file · unknown tool 'write_file'`. Sensitive tools were dropped
from the catalog whenever no interactive approver was wired, on the reasoning
that with no human present nothing could authorise the call.

Path-based approval retired that premise for file writes. A write states its
destination, one inside the workspace is auto-approved with no human, and the
protected directories are refused by the tool outright — so the gate would in
fact have cleared this call. Withholding the tool denied a capability that was
available. `omni exec` now also uses workspace-auto for sandboxed `bash` /
`run_compute` (Codex `exec` / Never). Daemon and IM turns still hide the
shell, because a command names nothing to assess and nobody is present to
confirm it.

An IM turn was held back from that reasoning for one more release, on the
grounds that the write tools refuse a chat channel in their own body anyway.
Task 964f17aa is what that cost: asked over WeChat for a survey, the model
called `write_file`, was told no such tool exists, and delivered a 14,000
character paper as chat text — eight message bubbles, no file to attach, and
enough of them that the figures queued behind it failed to send. The refusal now
turns on the destination like every other write, so both layers appear here: the
catalog offers the tool, and the tool still refuses to touch the owner's files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.agent.intent_plan import ToolPolicy
from omni.agent.orchestrator import OmniAgent
from omni.config import load_settings
from omni.core.approval import SENSITIVE_TOOLS, reconcile_sensitive_visibility

_BLOCKED = ["bash", "write_file", "edit_file", "run_compute", "navigate"]


def _visible(blocked: list[str], remaining: list[str]) -> set[str]:
    """The sensitive tools this reconciliation hands to the model."""
    return {t for t in blocked if t in SENSITIVE_TOOLS} - set(remaining)


# ── the rule itself ──


def test_a_write_is_offered_when_the_gate_can_settle_it_alone() -> None:
    remaining = reconcile_sensitive_visibility(
        _BLOCKED, gate_can_clear=False, path_assessed_can_clear=True
    )

    assert _visible(_BLOCKED, remaining) == {"write_file", "edit_file"}


def test_a_shell_is_not_offered_because_it_names_nothing_to_assess() -> None:
    remaining = reconcile_sensitive_visibility(
        _BLOCKED, gate_can_clear=False, path_assessed_can_clear=True
    )

    assert "bash" in remaining
    assert "run_compute" in remaining


def test_a_capability_decision_is_not_a_security_decision() -> None:
    """Non-sensitive entries were blocked by planning and stay blocked."""
    remaining = reconcile_sensitive_visibility(
        _BLOCKED, gate_can_clear=False, path_assessed_can_clear=True
    )

    assert "navigate" in remaining


def test_without_a_path_assessment_nothing_sensitive_is_offered() -> None:
    remaining = reconcile_sensitive_visibility(_BLOCKED, gate_can_clear=False)

    assert remaining == _BLOCKED


def test_an_approver_still_clears_the_whole_set() -> None:
    remaining = reconcile_sensitive_visibility(_BLOCKED, gate_can_clear=True)

    assert _visible(_BLOCKED, remaining) == set(SENSITIVE_TOOLS) & set(_BLOCKED)


def test_a_tool_the_owner_pre_approved_is_still_honoured_alongside(tmp_path) -> None:  # noqa: ANN001
    remaining = reconcile_sensitive_visibility(
        _BLOCKED,
        gate_can_clear=False,
        approved={"bash"},
        path_assessed_can_clear=True,
    )

    assert _visible(_BLOCKED, remaining) == {"bash", "write_file", "edit_file"}


# ── the facts the agent supplies for that rule ──


async def _policy_for(
    channel: str, *, approval_policy: str, workspace_auto: bool = False
) -> ToolPolicy:
    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = approval_policy
    agent = await OmniAgent.create(settings)
    agent.approver = None  # the non-interactive case: nobody to ask
    if workspace_auto:
        agent._workspace_auto_tasks.add("t1")
    try:
        return agent._react_tool_policy(
            ToolPolicy(blocked_tools=list(_BLOCKED)), task_id="t1", channel=channel
        )
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_a_local_exec_turn_can_write_the_paper_it_was_asked_for() -> None:
    policy = await _policy_for("cli", approval_policy="untrusted")

    assert _visible(_BLOCKED, policy.blocked_tools) == {"write_file", "edit_file"}


@pytest.mark.asyncio
async def test_a_local_cli_turn_without_workspace_auto_still_gets_no_shell() -> None:
    """Daemon / REPL-without-TTY: a command names nothing to assess."""
    policy = await _policy_for("cli", approval_policy="untrusted")

    assert "bash" in policy.blocked_tools
    assert "run_compute" in policy.blocked_tools


@pytest.mark.asyncio
async def test_a_workspace_auto_exec_turn_is_offered_the_shell() -> None:
    """``omni exec`` is Codex Never + workspace-write: the catalog must offer bash."""
    policy = await _policy_for(
        "cli", approval_policy="untrusted", workspace_auto=True
    )

    assert _visible(_BLOCKED, policy.blocked_tools) == set(SENSITIVE_TOOLS) & set(
        _BLOCKED
    )


@pytest.mark.asyncio
async def test_an_im_turn_is_offered_the_write_it_needs_to_produce_a_document() -> None:
    """A chat request that must produce a file has to be able to ask for one.

    Not a relaxation of where an IM turn may write — that is decided per
    destination below — but of whether it may write at all. While the answer was
    no, the only place a generated paper could go was the reply itself.
    """
    policy = await _policy_for("wechat", approval_policy="untrusted")

    assert _visible(_BLOCKED, policy.blocked_tools) == {"write_file", "edit_file"}


@pytest.mark.asyncio
async def test_an_im_turn_still_gets_no_shell() -> None:
    """A command names nothing to assess, so a chat channel cannot run one."""
    policy = await _policy_for("wechat", approval_policy="untrusted")

    assert "bash" in policy.blocked_tools
    assert "run_compute" in policy.blocked_tools


@pytest.mark.asyncio
async def test_an_owner_who_asked_to_see_everything_is_offered_nothing() -> None:
    """Under `always` even an in-workspace write prompts, and with no approver
    that prompt cannot be answered."""
    policy = await _policy_for("cli", approval_policy="always")

    assert _visible(_BLOCKED, policy.blocked_tools) == set()


# ── visibility is not authorisation ──


@pytest.mark.asyncio
async def test_being_offered_the_tool_does_not_authorise_leaving_the_workspace(
    tmp_path,  # noqa: ANN001
) -> None:
    """The catalog got wider; the boundary did not move."""
    from omni.core.approval import ApprovalGate
    from omni.skills_runtime.builtin_tools.fs import write_roots_for

    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = "untrusted"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = ApprovalGate(
        settings,
        approver=None,  # non-interactive daemon / IM: nobody to ask
        writable_roots=write_roots_for(workspace, workspace, []),
    )

    outside = tmp_path / "elsewhere.md"
    result = await gate.invoke(
        "write_file", {"path": str(outside), "contents": "x"}, _should_not_run
    )

    assert not outside.exists()
    assert "denied" in str(result).lower() or "approval" in str(result).lower()


async def _should_not_run():  # noqa: ANN202
    raise AssertionError("an out-of-workspace write must not reach the tool")


# ── where an IM turn may write ──


async def _im_write(tmp_path, path: str) -> tuple[str, Path]:  # noqa: ANN001
    """Run ``write_file`` on a WeChat-originated turn; return its reply and target."""
    from omni.config.paths import get_paths
    from omni.skills_runtime.builtin_tools.fs import build_fs_tools, resolve_write_target
    from omni.skills_runtime.context import ExecContext
    from omni.storage.artifacts import ArtifactStore
    from omni.storage.db import get_database
    from omni.storage.models import TaskORM

    settings = load_settings()
    paths = get_paths(project="imwriteboundary")
    paths.ensure_dirs()
    db = get_database(paths.project_db)
    await db.init()
    task_id = "task-im-write-boundary"
    async with db.session() as session:
        if await session.get(TaskORM, task_id) is None:
            session.add(
                TaskORM(
                    id=task_id,
                    session_id="s1",
                    project=paths.project_name,
                    title="IM write boundary",
                )
            )
            await session.commit()
    ctx = ExecContext(
        settings=settings,
        paths=paths,
        working_dir=tmp_path,
        artifacts=ArtifactStore(paths, db),
        task_id=task_id,
        session_id="s1",
        db=db,
        channel="wechat",
    )
    try:
        handler = next(
            tool.handler
            for tool in build_fs_tools(ctx)
            if tool.spec.name == "write_file"
        )
        return str(await handler({"path": path, "contents": "# survey\n"})), (
            await resolve_write_target(ctx, path)
        )
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_a_chat_request_can_store_the_deliverable_it_asked_for(tmp_path) -> None:  # noqa: ANN001
    """A bare filename is a deliverable, and generated output is omni's own area.

    This is the write that had nowhere to go: the whole survey went into the
    conversation because naming it as a file was refused.
    """
    reply, target = await _im_write(tmp_path, "rag_survey.md")

    assert reply.startswith("OK:"), reply
    assert target.is_file()


@pytest.mark.asyncio
async def test_a_chat_request_may_write_inside_its_workspace(tmp_path) -> None:  # noqa: ANN001
    """Codex workspace-write: an IM turn may edit the tree it already operates in."""
    target = tmp_path / "notes.md"

    reply, landed = await _im_write(tmp_path, str(target))

    assert reply.startswith("OK:"), reply
    assert landed.read_text(encoding="utf-8") == "# survey\n"


@pytest.mark.asyncio
async def test_a_chat_request_still_may_not_write_the_owners_files(tmp_path) -> None:  # noqa: ANN001
    """The workspace got writable; the envelope around it did not move.

    Codex still rejects a patch that leaves the project. A chat channel has
    nobody to ask, so an escape is refused rather than confirmed.
    """
    outside = tmp_path.parent / "owner-home" / "README.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("owned by the user\n", encoding="utf-8")

    reply, _target = await _im_write(tmp_path, str(outside))

    assert reply.startswith("ERROR:"), reply
    assert outside.read_text(encoding="utf-8") == "owned by the user\n"
