"""Where a write lands, and whether landing there needs the owner's consent.

Task aac5b285 shows the two halves of the old answer. The owner was asked to
confirm writing a paper into their own working directory (one prompt for the one
ordinary act of the turn), and the paper was then dropped at the repository root
as an untracked file. Codex settles both by destination: ``assess_patch_safety``
auto-approves a patch whose every path is under a writable root, and the model
edits the tree it was pointed at rather than wherever the process was launched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.core.approval import ApprovalDecision, ApprovalGate, ApprovalRequest
from omni.skills_runtime.builtin_tools.fs import build_fs_tools, write_roots_for
from omni.skills_runtime.context import ExecContext


def _tool(tools, name):  # noqa: ANN001, ANN202
    return next(t for t in tools if t.spec.name == name).handler


@pytest.fixture
def ctx(tmp_path: Path) -> ExecContext:
    settings = load_settings()
    paths = get_paths(project="writedest")
    paths.project_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return ExecContext(settings=settings, paths=paths, working_dir=tmp_path)


def _gate(ctx: ExecContext, *, policy: str = "untrusted") -> tuple[ApprovalGate, list[str]]:
    """A gate that records every question it would have put to the owner."""
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> ApprovalDecision:
        asked.append(req.detail)
        return ApprovalDecision(True, reason="owner said yes")

    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = policy
    gate = ApprovalGate(
        settings,
        approver=approver,
        writable_roots=write_roots_for(
            ctx.paths.project_dir, ctx.working_dir, settings.security.fs_write_allow
        ),
        output_roots=[ctx.paths.artifacts_dir],
        working_dir=ctx.working_dir,
        workspace=ctx.paths.project_dir,
    )
    return gate, asked


async def _authorize(gate: ApprovalGate, path: str) -> None:
    await gate.invoke("write_file", {"path": path, "contents": "x"}, _noop)


async def _noop():  # noqa: ANN202
    return "ok"


# ── consent is decided by destination, not by the tool's name ──


@pytest.mark.asyncio
async def test_writing_inside_the_workspace_does_not_interrupt_the_owner(ctx) -> None:  # noqa: ANN001
    gate, asked = _gate(ctx)

    await _authorize(gate, str(ctx.working_dir / "RAG_review.md"))

    assert asked == [], "a write into the turn's own workspace should not need confirming"


@pytest.mark.asyncio
async def test_a_write_that_escapes_the_workspace_still_asks(ctx, tmp_path: Path) -> None:  # noqa: ANN001
    gate, asked = _gate(ctx)
    outside = tmp_path.parent / "elsewhere" / "notes.md"

    await _authorize(gate, str(outside))

    assert asked == [str(outside)], "leaving the workspace is exactly what consent is for"


@pytest.mark.asyncio
async def test_a_bare_filename_is_workspace_bound_so_it_does_not_ask(ctx) -> None:  # noqa: ANN001
    """The write tools resolve a bare name into the workspace, so the gate must
    not re-resolve it against whatever directory the process happens to be in."""
    gate, asked = _gate(ctx)

    await _authorize(gate, "RAG_review.md")

    assert asked == []


@pytest.mark.asyncio
async def test_a_credential_inside_the_workspace_is_not_an_ordinary_document(ctx) -> None:  # noqa: ANN001
    """What makes a destination boring is that it is a document.

    The envelope was widened so that writing a draft into the owner's own tree
    stops costing a prompt. A private key that happens to sit in that tree is
    not a draft, and location alone must not vouch for it.
    """
    gate, asked = _gate(ctx)

    await _authorize(gate, str(ctx.working_dir / ".ssh" / "id_rsa"))

    assert asked, "a credential must never be auto-approved on location alone"


@pytest.mark.asyncio
async def test_the_always_policy_is_unchanged_by_any_of_this(ctx) -> None:  # noqa: ANN001
    """An owner who asked to see every call still sees every call."""
    gate, asked = _gate(ctx, policy="always")

    await _authorize(gate, str(ctx.working_dir / "RAG_review.md"))

    assert asked, "policy=always must keep asking regardless of destination"


@pytest.mark.asyncio
async def test_a_repository_hook_is_never_waved_through(ctx) -> None:  # noqa: ANN001
    """Writing `.git/hooks` turns file access into code execution on the owner's
    next commit, so widening the envelope must not widen to include it."""
    gate, asked = _gate(ctx)

    await _authorize(gate, str(ctx.working_dir / ".git" / "hooks" / "pre-commit"))

    assert asked, "a protected directory must never be auto-approved"


# ── and the tool refuses those paths outright, consent or not ──


@pytest.mark.asyncio
async def test_the_tool_refuses_a_protected_directory_even_when_approved(ctx) -> None:  # noqa: ANN001
    write = _tool(build_fs_tools(ctx), "write_file")

    result = await write(
        {"path": str(ctx.working_dir / ".git" / "hooks" / "pre-commit"), "contents": "#!/bin/sh"}
    )

    assert result.startswith("ERROR")
    assert "protected" in result
    assert not (ctx.working_dir / ".git" / "hooks" / "pre-commit").exists()


# ── where a bare filename lands ──


@pytest.mark.asyncio
async def test_a_named_deliverable_is_stored_in_the_workspace(ctx) -> None:  # noqa: ANN001
    write = _tool(build_fs_tools(ctx), "write_file")

    result = await write({"path": "RAG_review.md", "contents": "# survey"})

    landed = ctx.paths.artifacts_dir / "RAG_review.md"
    assert landed.read_text(encoding="utf-8") == "# survey"
    assert str(landed) in result, "the model must be told where the file actually went"


@pytest.mark.asyncio
async def test_a_bare_name_that_already_exists_rewrites_that_file(ctx) -> None:  # noqa: ANN001
    """Naming a file the user is working on means that file, not a new one."""
    existing = ctx.working_dir / "README.md"
    existing.write_text("old", encoding="utf-8")
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "README.md", "contents": "new"})

    assert existing.read_text(encoding="utf-8") == "new"
    assert not (ctx.paths.artifacts_dir / "README.md").exists()


@pytest.mark.asyncio
async def test_a_bare_name_already_in_artifacts_continues_that_file(ctx) -> None:  # noqa: ANN001
    """A name we have already generated once is a continuation of that document.

    Consulting the working directory first made the pollution self-perpetuating:
    one pre-fix run left a paper at the repository root, and from then on every
    bare-name write of that title kept finding it there, so the deliverable could
    never migrate to where deliverables live.
    """
    started = ctx.paths.artifacts_dir / "survey.md"
    started.write_text("# chapter one\n", encoding="utf-8")
    decoy = ctx.working_dir / "survey.md"
    decoy.write_text("a same-named file in the repo", encoding="utf-8")
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "survey.md", "contents": "# rewritten\n"})

    assert started.read_text(encoding="utf-8") == "# rewritten\n"
    assert decoy.read_text(encoding="utf-8") == "a same-named file in the repo"


@pytest.mark.asyncio
async def test_a_document_written_in_chunks_stays_in_one_piece(ctx) -> None:  # noqa: ANN001
    """Append is the only way to write a document longer than one response, so
    every chunk of one bare name must land on the same file."""
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "long.md", "contents": "part one\n"})
    await write({"path": "long.md", "contents": "part two\n", "append": True})

    landed = ctx.paths.artifacts_dir / "long.md"
    assert landed.read_text(encoding="utf-8") == "part one\npart two\n"


@pytest.mark.asyncio
async def test_a_same_named_repo_file_cannot_split_a_chunked_document(ctx) -> None:  # noqa: ANN001
    """The failure this order prevents: chunk one lands in artifacts, then a
    same-named repo file steals chunk two and the document exists as two halves
    in two directories."""
    write = _tool(build_fs_tools(ctx), "write_file")
    await write({"path": "long.md", "contents": "part one\n"})
    decoy = ctx.working_dir / "long.md"
    decoy.write_text("unrelated repo file\n", encoding="utf-8")

    await write({"path": "long.md", "contents": "part two\n", "append": True})

    assert (ctx.paths.artifacts_dir / "long.md").read_text(
        encoding="utf-8"
    ) == "part one\npart two\n"
    assert decoy.read_text(encoding="utf-8") == "unrelated repo file\n"


@pytest.mark.asyncio
async def test_editing_a_real_repository_file_still_works(ctx) -> None:  # noqa: ANN001
    """A bare name found only in the working directory is a genuine repo file —
    AGENTS.md, README.md — and naming it must still mean it."""
    real = ctx.working_dir / "AGENTS.md"
    real.write_text("old guidance", encoding="utf-8")
    write = _tool(build_fs_tools(ctx), "write_file")

    await write({"path": "AGENTS.md", "contents": "new guidance"})

    assert real.read_text(encoding="utf-8") == "new guidance"
    assert not (ctx.paths.artifacts_dir / "AGENTS.md").exists()


@pytest.mark.asyncio
async def test_an_explicit_path_is_left_alone(ctx) -> None:  # noqa: ANN001
    write = _tool(build_fs_tools(ctx), "write_file")
    target = ctx.working_dir / "drafts" / "survey.md"

    await write({"path": str(target), "contents": "# draft"})

    assert target.read_text(encoding="utf-8") == "# draft"


@pytest.mark.asyncio
async def test_an_empty_path_is_reported_rather_than_written_somewhere(ctx) -> None:  # noqa: ANN001
    write = _tool(build_fs_tools(ctx), "write_file")

    result = await write({"path": "  ", "contents": "x"})

    assert result.startswith("ERROR")


# ── the gate the product actually builds knows its own workspace ──


@pytest.mark.asyncio
async def test_the_real_agent_builds_a_gate_that_knows_where_it_may_write() -> None:
    """The rules above are only worth anything if production passes the roots in.

    Every assertion in this file exercises a gate the test constructed itself, so
    a wiring slip in the orchestrator would restore the prompt on every write
    while leaving them all green.
    """
    from omni.agent import OmniAgent
    from omni.config import load_settings

    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = "untrusted"
    agent = await OmniAgent.create(settings)
    try:
        gate = agent._approval_gate("t1", "cli", "s1")
        roots = gate._writable_roots
        project_dir = agent.paths.project_dir.resolve()
    finally:
        await agent.aclose()

    assert roots, "the gate was built without a workspace and will prompt for everything"
    assert project_dir in roots


@pytest.mark.asyncio
async def test_a_relative_write_is_judged_in_the_turn_directory_not_the_process_cwd(
    ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``omni serve`` is often launched from a git checkout; IM writes are not."""
    other = tmp_path / "process-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    gate, asked = _gate(ctx)

    await _authorize(gate, "artifacts/survey.md")

    assert asked == [], "a deliverable path relative to the turn workspace is in-workspace"


@pytest.mark.asyncio
async def test_an_im_write_inside_the_workspace_does_not_need_a_human(ctx) -> None:  # noqa: ANN001
    """Generating a file is a basic agent capability on chat channels too.

    Codex ``assess_patch_safety`` auto-approves an in-project patch when nobody
    can be asked. The IM confirmation string is for shell, not for write_file.
    """
    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = "untrusted"
    ran: list[str] = []

    async def run() -> str:
        ran.append("yes")
        return "ok"

    gate = ApprovalGate(
        settings,
        channel="wechat",
        approver=None,
        writable_roots=write_roots_for(
            ctx.paths.project_dir, ctx.working_dir, settings.security.fs_write_allow
        ),
        working_dir=ctx.working_dir,
        workspace=ctx.paths.project_dir,
    )
    result = await gate.invoke(
        "write_file",
        {"path": str(ctx.working_dir / "paper.md"), "contents": "# survey\n"},
        run,
    )

    assert ran == ["yes"], result
    assert result == "ok"


@pytest.mark.asyncio
async def test_an_im_write_that_leaves_the_project_is_rejected_not_sent_to_the_cli(
    ctx, tmp_path: Path
) -> None:
    """The model must not be told to rerun from the CLI — that became a chat essay."""
    settings = load_settings()
    settings.security.require_approval = True
    settings.security.approval_policy = "untrusted"
    outside = tmp_path.parent / "owner-home" / "notes.md"

    async def should_not_run() -> str:
        raise AssertionError("an escaped write must not reach the tool")

    gate = ApprovalGate(
        settings,
        channel="wechat",
        approver=None,
        writable_roots=write_roots_for(
            ctx.paths.project_dir, ctx.working_dir, settings.security.fs_write_allow
        ),
        working_dir=ctx.working_dir,
        workspace=ctx.paths.project_dir,
    )
    result = await gate.invoke(
        "write_file", {"path": str(outside), "contents": "x"}, should_not_run
    )

    text = str(result)
    assert "outside of the project" in text
    assert "CLI" not in text
    assert "local confirmation" not in text
