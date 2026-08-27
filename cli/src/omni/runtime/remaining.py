"""Named scientific outputs vs artifacts actually on the task.

``plan.outputs`` / ``verification_plan.required_outputs`` name what the turn
owed the user (a figure, a manuscript, slides). Settlement used to treat those
names as captions. This module is the fact lookup: given the artifacts the
record already has, which named outputs are still missing.

It does not grade quality. A PNG is a figure; a Markdown report is a draft.
Sidecar DOT/JSON files do not count as the deliverable they support.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.agent.capabilities import (
    CAPABILITY_FIGURE,
    CONTRACT_DELIVERABLES,
    TYPED_REF_OUTPUTS,
    WRITING_DELIVERABLES,
    contract_outputs,
    contract_outputs_from_capabilities,
    writing_outputs,
)
from omni.runtime.task_results import is_dot_artifact

_CONTRACT_WRITE_TOOLS = ("write_file", "edit_file")
_WRITING_FILE_DEBTS = WRITING_DELIVERABLES | {"review", "response_letter"}

# A request that names both a figure and a paper is a multi-deliverable contract
# even when the semantic planner only emitted ``outputs=["answer"]``. Either
# hint alone is too common in ordinary chat to bind a debt.
# CJK spellings as unicode escapes so the control-plane source stays English
# (same pattern as ``scheduling/temporal.py`` day-part words). Runtime values
# are the ideographs users type: figure/diagram, paper, survey, manuscript.
_FIGURE_HINTS = ("\u56fe", "figure", "diagram", "flowchart", "schematic")
# Complete decks, not a single-slide LiveFigure / editable figure.
_SLIDE_HINTS = (
    "slides",
    "slide deck",
    "pptx",
    "ppt",
    "pptx deck",
    "presentation",
    "seminar",
    "thesis defense",
    "group meeting",
    "conference talk",
    "\u7ec4\u4f1a",
    "\u7b54\u8fa9",
    "\u5e7b\u706f",
    "\u8bfe\u4ef6",
    "\u505appt",
    "\u751f\u6210ppt",
    "\u5236\u4f5cppt",
    "ppt\u6c47\u62a5",
    "\u505a\u4e00\u4efdppt",
)
_SLIDE_NEGATIVE = (
    "single-slide",
    "single slide",
    "one-slide",
    "one slide",
    "livefigure",
    "editable figure",
    "\u5355\u9875",
    "\u4e00\u5f20\u56fe",
)
_PAPER_HINTS = (
    "\u8bba\u6587",
    "\u7efc\u8ff0",
    "\u624b\u7a3f",
    "manuscript",
    "survey paper",
    "write a paper",
    "output a paper",
    " a paper",
)
# A survey-class closer binds a manuscript next to a figure. A deck that
# only names a survey / literature review is slides, not a paper, unless a
# hard paper word is also present.
_HARD_PAPER_HINTS = (
    "\u8bba\u6587",
    "manuscript",
    "survey paper",
    "write a paper",
    "output a paper",
    " a paper",
)
_SURVEY_CLOSER_HINTS = (
    "\u8c03\u7814",
    "\u7efc\u8ff0",
    "survey",
    "literature review",
    "related work",
)
# Typed retrieve-only scope: the user asked for identifiers, not produce.
_SOURCE_ID_ONLY_HINTS = (
    "only list source_id",
    "only source_id",
    "source_id only",
    "source_ids only",
    "just source_id",
    "just the source_id",
    "\u53ea\u5217\u51fa source_id",
    "\u53ea\u5217 source_id",
    "\u53ea\u5217\u51fasource_id",
)

# Cross-repo / architecture comparison: host-owns a markdown report so chat
# finding-acks cannot settle as succeeded (6a97d28f vs afb9228d).
_ANALYSIS_REPORT_STRONG = (
    "\u5bf9\u6807",
    "\u5bf9\u7167",
    "\u5206\u6790\u62a5\u544a",
    "benchmark against",
    "comparison report",
    "compare against",
    "versus the source",
)
_ANALYSIS_REPORT_SCOPE = (
    "\u4ed4\u7ec6\u5206\u6790",
    "\u67b6\u6784\u5206\u6790",
    "analyze the",
    "architecture analysis",
)
_ANALYSIS_REPORT_TARGET = (
    "\u6e90\u7801",
    "source code",
    "sourcecode",
    "\u5b9e\u73b0",
    "implementation",
)
# A code-review ask can mention "对标 Codex" as a criterion without owing a report.
# Bare "review" is not listed: it would also hide "literature review".
_ANALYSIS_REPORT_NEGATIVE = (
    "\u4ed4\u7ec6review",
    "code review",
    "\u6574\u4f53\u7684review",
    "\u6574\u4f53review",
    "\u672a\u63d0\u4ea4",
    "uncommitted",
    "review \u4eca\u5929",
    "review today's",
    "push \u5230",
    "push to master",
    "\u4e0d\u505a\u4ee3\u7801\u6539\u52a8",
    "do not change code",
    "only review",
    "\u4ec5\u505a review",
)

# Host compiles task.inspect only when the user asked for a prior task's
# status or artifact location — not when they repeat a produce request.
_PRIOR_TASK_STATUS_HINTS = (
    "\u72b6\u6001",
    "status",
    "\u6210\u529f\u4e86\u5417",
    "\u5931\u8d25\u4e86\u5417",
    "\u5931\u8d25\u539f\u56e0",
    "\u4e3a\u4ec0\u4e48\u5931\u8d25",
    "\u600e\u4e48\u6837\u4e86",
    "\u5b8c\u6210\u4e86\u5417",
    "did it succeed",
    "did it fail",
    "how did it go",
    "\u4ea7\u7269\u662f\u4ec0\u4e48",
    "\u4ea7\u51fa\u662f\u4ec0\u4e48",
    "\u4ea7\u7269\u5728\u54ea",
    "\u4ea7\u51fa\u5728\u54ea",
    "\u7ed3\u679c\u5728\u54ea\u91cc",
    "\u7ed3\u679c\u5728\u54ea",
    "artifact location",
    "look at the output",
    "what did that task",
    "previous task",
    "that task",
    "\u8fd9\u4e2a\u4efb\u52a1",
    "\u524d\u9762\u95ee\u7684",
    "\u521a\u624d\u90a3\u4e2a",
    "\u521a\u624d\u7684",
    "\u4e0a\u4e00\u4e2a\u4efb\u52a1",
)

# Asking about an earlier task's figure/paper is recall, not a new contract.
_FIGURE_PAPER_NEGATIVE = (
    "\u524d\u9762\u95ee\u7684",
    "\u8fd9\u4e2a\u4efb\u52a1",
    "\u4ea7\u51fa\u662f\u4ec0\u4e48",
    "previous task",
    "that task",
    "look at the output",
    "what did that task",
)

_FIGURE_KINDS = frozenset({"figure"})
_FIGURE_SUFFIXES = frozenset({"png", "svg", "jpg", "jpeg", "webp", "gif"})
_FIGURE_MIMES = frozenset(
    {
        "image/png",
        "image/svg+xml",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)
_MANUSCRIPT_KINDS = frozenset({"paper", "report", "review", "manuscript", "document"})
_MANUSCRIPT_SUFFIXES = frozenset({"md", "markdown", "docx", "doc", "tex", "html"})
_SLIDE_SUFFIXES = frozenset({"pptx", "ppt"})
_POSTER_SUFFIXES = frozenset({"pdf", "pptx", "png", "svg"})
# A PDF is ambiguous (paper vs figure vs poster). Kind / title decide first;
# a kind-less PDF is treated as a manuscript only when the name asks for one.
_PDF_SUFFIXES = frozenset({"pdf"})


def remaining_deliverables(
    required: list[str],
    artifacts: list[Any],
) -> list[str]:
    """Return contract outputs in ``required`` that no artifact satisfies.

    Order follows ``required``. Non-contract names (``answer``, ``sources``) are
    dropped: they are not artifact debts.
    """
    owed = contract_outputs([str(name) for name in required if str(name)])
    if not owed:
        return []
    delivered: set[str] = set()
    for artifact in artifacts:
        if _is_sidecar(artifact):
            continue
        delivered.update(_outputs_satisfied_by(artifact))
    return [name for name in owed if name not in delivered]


def remaining_typed_refs(required: list[str], *, source_ids: list[str] | None) -> list[str]:
    """ROM-backed debts (``sources``) that no ``source_id`` list satisfies."""
    owed = [name for name in required if str(name) in TYPED_REF_OUTPUTS]
    if "sources" not in owed:
        return []
    if any(str(item).strip() for item in (source_ids or [])):
        return []
    return ["sources"]


def remaining_writing(remaining: list[str]) -> list[str]:
    """Writing debts native ``synthesis.final`` can fill without re-running skills."""
    return writing_outputs(remaining)


def remaining_contract_files(remaining: list[str]) -> list[str]:
    """Named file debts settlement can prove — not ``sources`` or ``answer``."""
    return [name for name in remaining if name in CONTRACT_DELIVERABLES]


def survey_closer_eligible(plan: Any) -> bool:
    """Whether this plan is a written-survey shape (not figure+paper or slides).

    Used by the unused host-writing salvage helper. The default produce path
    is capable ReAct with ``write_file``; this predicate does not route.
    """
    from omni.agent.capabilities import (
        CAPABILITY_LITERATURE_SEARCH,
        is_survey_pair,
    )
    from omni.agent.plan_runner_utils import plan_capabilities

    text = str(getattr(plan, "user_message", "") or "")
    folded = text.casefold()
    if infer_slide_outputs(text) or infer_figure_and_paper_outputs(text):
        return False
    caps = list(plan_capabilities(plan))
    caps.extend(str(key) for key in (getattr(plan, "capability_inputs", None) or {}))
    names = [str(item) for item in (getattr(plan, "outputs", None) or []) if item]
    verification = getattr(plan, "verification_plan", None)
    names.extend(
        str(item) for item in (getattr(verification, "required_outputs", None) or []) if item
    )
    if is_survey_pair(caps, names):
        return True
    writing = remaining_writing(contract_outputs(names))
    extra = set(contract_outputs(names)) - set(WRITING_DELIVERABLES)
    if extra or not writing:
        return False
    if CAPABILITY_LITERATURE_SEARCH in caps:
        return True
    if any(hint in text or hint in folded for hint in _FIGURE_HINTS):
        return False
    return any(hint in text or hint in folded for hint in _SURVEY_CLOSER_HINTS)


def plan_owes_scientific_outputs(plan: Any) -> bool:
    """Whether settlement can still demand a figure, manuscript, slides, or review.

    Answer-only and task-inspect turns are false: memory and ``get_task`` *are*
    the work. Recalled files from another task never flip this to false — only
    names on this plan's contract do.
    """
    verification = getattr(plan, "verification_plan", None)
    required = list(getattr(verification, "required_outputs", None) or [])
    names = required or list(getattr(plan, "outputs", None) or [])
    return bool(contract_outputs([str(name) for name in names if name]))


def remaining_figure(remaining: list[str]) -> list[str]:
    """Format-neutral figure debts the host can fill (PPTX or SVG/PNG)."""
    return [name for name in remaining if name == CAPABILITY_FIGURE]


def remaining_slides(remaining: list[str]) -> list[str]:
    """Deck debts the host can fill by running the ``slides.generate`` provider.

    ``artifact.pptx`` is the single-slide editable figure, not a talk deck.
    """
    return [name for name in remaining if name == "artifact.slides"]


def skip_completed_skills_note(
    remaining: list[str],
    artifacts: list[Any],
) -> str:
    """Host note for a retry/resume: what exists, what is still owed."""
    if not remaining and not artifacts:
        return ""
    delivered = []
    for artifact in artifacts:
        if _is_sidecar(artifact):
            continue
        title = str(getattr(artifact, "title", "") or "")
        kind = str(getattr(artifact, "kind", "") or "")
        path = _path_of(artifact)
        label = title or kind or path or "artifact"
        delivered.append(label)
    lines = [
        "Already delivered artifacts on this task (do not rerun the skills that produced them): "
        + (", ".join(delivered[:8]) if delivered else "none"),
        "Still required: " + (", ".join(remaining) if remaining else "none"),
        "Only artifacts owned by this task satisfy required_outputs; files from another task do not.",
    ]
    return "\n".join(lines)


_RETRIEVE_ONLY_IGNORE = frozenset(
    {"sources", "answer", "workflow", "literature.search"}
)


def utterance_asks_only_source_ids(text: str) -> bool:
    """True when the user typed a retrieve-only identifier scope."""
    raw = str(text or "")
    folded = raw.casefold()
    return any(hint in raw or hint in folded for hint in _SOURCE_ID_ONLY_HINTS)


def incoming_plan_is_retrieve_only(plan: Any) -> bool:
    """True when the incoming plan already contracted sources, not produce artifacts.

    Named ``search_literature`` freezes the tool. Produce debts still bind
    unless the utterance asked only for source ids.
    """
    verification = getattr(plan, "verification_plan", None)
    required = [
        str(item).strip()
        for item in (getattr(verification, "required_outputs", None) or [])
        if str(item).strip()
    ]
    if "sources" not in required:
        return False
    named = {
        str(item).strip()
        for item in (*(getattr(plan, "outputs", None) or []), *required)
        if str(item).strip()
    }
    leftover = named - _RETRIEVE_ONLY_IGNORE
    return not leftover


def bind_contract_outputs(plan: Any, proposal: Any | None = None) -> Any:
    """Copy named scientific outputs onto verification so settlement can see them.

    ``answer`` stays descriptive. Figure / manuscript / slides are a contract:
    the durable record either has the artifact or the run is not succeeded.

    The semantic planner often emits ``outputs=["answer"]`` on the ReAct floor
    even when ``required_capabilities`` / ``workflow_steps`` already named a
    figure and a paper. Those names are the contract; the ReAct floor is only
    the execution mode.

    An incoming retrieve-only plan still infers figure / slides / manuscript
    when the same sentence names them. ``grant_contract_write_tools`` will
    not open write on a retrieve window (``run_skill`` blocked). Only an
    explicit source-id-only scope skips that inference.
    """
    from omni.agent.intent_plan import VerificationPlan

    names = list(plan.outputs)
    if proposal is not None:
        names.extend(str(item) for item in (getattr(proposal, "outputs", None) or []) if item)
        caps = [str(item) for item in (getattr(proposal, "required_capabilities", None) or []) if item]
        for step in getattr(proposal, "workflow_steps", None) or []:
            if isinstance(step, dict) and step.get("capability"):
                caps.append(str(step["capability"]))
        names.extend(contract_outputs_from_capabilities(caps))
    message = getattr(plan, "user_message", "") or ""
    if not utterance_asks_only_source_ids(message):
        names.extend(infer_figure_and_paper_outputs(message))
        names.extend(infer_slide_outputs(message))
        names.extend(infer_analysis_report_outputs(message))
    names.extend(_outputs_from_selected_skills(plan))
    contract = contract_outputs(names)
    if not contract:
        return plan
    plan.outputs = list(dict.fromkeys([*plan.outputs, *contract]))
    required = list(dict.fromkeys([*plan.verification_plan.required_outputs, *contract]))
    plan.verification_plan = VerificationPlan(
        required_outputs=required,
        required_events=list(plan.verification_plan.required_events),
    )
    return grant_contract_write_tools(plan)


def grant_contract_write_tools(plan: Any) -> Any:
    """Unblock ``write_file`` / ``edit_file`` when this turn owes a manuscript.

    The default capable floor blocks those as irreversible mutations. A named
    writing debt is the produce path — the model writes the file. ``bash`` and
    ``run_compute`` stay blocked. A sealed empty catalog or a retrieve-only
    contract (sources, no manuscript) is left alone.
    """
    names = [str(item) for item in (getattr(plan, "outputs", None) or []) if item]
    verification = getattr(plan, "verification_plan", None)
    names.extend(
        str(item) for item in (getattr(verification, "required_outputs", None) or []) if item
    )
    if not (set(contract_outputs(names)) & _WRITING_FILE_DEBTS):
        return plan
    policy = getattr(plan, "tool_policy", None)
    if policy is None:
        return plan
    if "run_skill" in set(policy.blocked_tools or []):
        return plan
    if policy.allowed_tools is not None and not policy.allowed_tools:
        return plan
    blocked = [
        name for name in (policy.blocked_tools or []) if name not in _CONTRACT_WRITE_TOOLS
    ]
    if list(policy.blocked_tools or []) != blocked:
        policy.blocked_tools = blocked
    if policy.allowed_tools is not None:
        policy.allowed_tools = list(
            dict.fromkeys([*policy.allowed_tools, *_CONTRACT_WRITE_TOOLS])
        )
    return plan


def infer_figure_and_paper_outputs(user_message: str) -> list[str]:
    """Bind figure + manuscript only when the request names both."""
    text = str(user_message or "")
    if not text:
        return []
    folded = text.casefold()
    if any(hint in text or hint in folded for hint in _FIGURE_PAPER_NEGATIVE):
        return []
    wants_figure = any(hint in text or hint in folded for hint in _FIGURE_HINTS)
    wants_paper = any(hint in text or hint in folded for hint in _PAPER_HINTS)
    if not (wants_figure and wants_paper):
        return []
    hard_paper = any(hint in text or hint in folded for hint in _HARD_PAPER_HINTS)
    if infer_slide_outputs(text) and not hard_paper:
        return ["artifact.figure"]
    return ["artifact.figure", "draft.manuscript"]


def utterance_asks_prior_task_status(user_message: str) -> bool:
    """Whether the user is asking about an earlier task's status or location.

    A new survey, code review, or architecture comparison is false even when
    Recent activity lists a similar title.
    """
    text = str(user_message or "")
    if not text:
        return False
    folded = text.casefold()
    return any(hint in text or hint in folded for hint in _PRIOR_TASK_STATUS_HINTS)


def utterance_asks_written_survey(user_message: str) -> bool:
    """A produce-the-survey request, not a code review that mentions 调研."""
    text = str(user_message or "")
    if not text:
        return False
    folded = text.casefold()
    if any(hint in text or hint in folded for hint in _ANALYSIS_REPORT_NEGATIVE):
        return False
    return any(hint in text or hint in folded for hint in _SURVEY_CLOSER_HINTS)


def infer_analysis_report_outputs(user_message: str) -> list[str]:
    """Bind a manuscript when the user asked for a source/architecture comparison.

    ``outputs=["answer"]`` is not enough: a finding-ack then settles as
    succeeded. A named ``draft.manuscript`` unblocks ``write_file`` and
    leaves the file unpaid until it exists. Does not fire on a lone
    "analyze this" chat, a survey, a code review, or a slide request.
    """
    text = str(user_message or "")
    if not text:
        return []
    folded = text.casefold()
    if infer_slide_outputs(text):
        return []
    if any(hint in text or hint in folded for hint in _FIGURE_PAPER_NEGATIVE):
        return []
    if any(hint in text or hint in folded for hint in _ANALYSIS_REPORT_NEGATIVE):
        return []
    if any(hint in text or hint in folded for hint in _ANALYSIS_REPORT_STRONG):
        return ["draft.manuscript"]
    scoped = any(hint in text or hint in folded for hint in _ANALYSIS_REPORT_SCOPE)
    targeted = any(hint in text or hint in folded for hint in _ANALYSIS_REPORT_TARGET)
    if scoped and targeted:
        return ["draft.manuscript"]
    return []


def infer_slide_outputs(user_message: str) -> list[str]:
    """Bind a deck debt when the request names slides, not a single-slide figure."""
    text = str(user_message or "")
    if not text:
        return []
    folded = text.casefold().replace("-", " ")
    raw = text.replace("-", " ")
    if any(hint in raw or hint in folded for hint in _SLIDE_NEGATIVE):
        return []
    if any(hint in raw or hint in folded for hint in _SLIDE_HINTS):
        return ["artifact.slides"]
    return []


def _outputs_from_selected_skills(plan: Any) -> list[str]:
    """Named contract outputs implied by the skills already selected on the plan."""
    names: list[str] = []
    for selection in getattr(plan, "selected_skills", None) or []:
        caps = [str(item) for item in (getattr(selection, "matched_capabilities", None) or []) if item]
        names.extend(contract_outputs_from_capabilities(caps))
    return names


async def remaining_retry_context(tasks: Any, artifacts: Any, task_id: str) -> str:
    """On a retry attempt, tell the model what already exists and what is still owed."""
    if not task_id:
        return ""
    try:
        current = await tasks.get_task(task_id)
    except Exception:  # noqa: BLE001
        return ""
    original_id = str(getattr(current, "retry_of_task_id", "") or "")
    if not original_id:
        return ""
    original = await tasks.get_task(original_id)
    if original is None:
        return ""
    plan = original.plan_json if isinstance(original.plan_json, dict) else {}
    verification = plan.get("verification_plan") if isinstance(plan.get("verification_plan"), dict) else {}
    required = list(verification.get("required_outputs") or plan.get("outputs") or [])
    try:
        rows = await artifacts.list_by_task(original_id)
    except Exception:  # noqa: BLE001
        rows = []
    remaining = remaining_deliverables(required, rows)
    note = skip_completed_skills_note(remaining, rows)
    if not note:
        return ""
    return (
        "[Remaining work] Continue from what already exists on this task. "
        "Files from another task do not satisfy this task's required_outputs. "
        "Do not rerun completed scientific skills when their artifacts are present.\n"
        + note
    )


# Skills whose named file is not paid by a write_file / bash leftover.
# livefigure pays the format-neutral figure slot and the stricter editable
# PPTX debt. A failed livefigure leaves both unpaid so a sibling can settle
# ``artifact.figure``; ``artifact.pptx`` stays owed only when the user named it.
CANONICAL_FILE_PRODUCERS: dict[str, tuple[str, ...]] = {
    "livefigure": ("artifact.figure", "artifact.pptx"),
    "paper-review": ("review",),
    "scientific-figure": ("artifact.figure",),
    "research-pptx": ("artifact.slides",),
    "research-poster": ("artifact.poster",),
    "review-response": ("response_letter",),
}
_FAILED_PRODUCER_STATUSES = frozenset(
    {
        "failed",
        "error",
        "needs_input",
        "blocked",
        "cancelled",
        "timed_out",
        "rejected",
    }
)
_OK_PRODUCER_STATUSES = frozenset(
    {"succeeded", "ok", "partial", "degraded", "warning", "submitted"}
)


def failed_canonical_file_debts(
    tool_trace: list[Any] | None,
    drained: list[dict[str, Any]] | None,
) -> list[str]:
    """Named files still owed after the last attempt of a canonical producer failed.

    A write_file Markdown or a harvested PPTX does not retire ``review`` /
    ``artifact.pptx`` when ``paper-review`` / ``livefigure`` itself failed.
    """
    last: dict[str, str] = {}
    for record in tool_trace or []:
        skill = _producer_skill_name(record)
        if skill not in CANONICAL_FILE_PRODUCERS:
            continue
        last[skill] = _producer_status(record)
    for item in drained or []:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill") or item.get("skill_name") or "").strip()
        if skill not in CANONICAL_FILE_PRODUCERS:
            continue
        last[skill] = str(item.get("status") or "")
    unpaid: list[str] = []
    for skill, status in last.items():
        if not _producer_attempt_failed(status):
            continue
        for name in CANONICAL_FILE_PRODUCERS[skill]:
            if name not in unpaid:
                unpaid.append(name)
    return unpaid


def _producer_skill_name(record: Any) -> str:
    arguments = getattr(record, "arguments", None)
    result = getattr(record, "result", None)
    if isinstance(result, dict):
        for key in ("skill_name", "skill"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
        nested = result.get("result")
        if isinstance(nested, dict):
            value = str(nested.get("skill_name") or nested.get("skill") or "").strip()
            if value:
                return value
    if isinstance(arguments, dict):
        return str(arguments.get("skill_name") or arguments.get("skill") or "").strip()
    return ""


def _producer_status(record: Any) -> str:
    result = getattr(record, "result", None)
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip()
        if status:
            return status
        nested = result.get("result")
        if isinstance(nested, dict):
            nested_status = str(nested.get("status") or "").strip()
            if nested_status:
                return nested_status
    return str(getattr(record, "status", "") or "")


def _producer_attempt_failed(status: str) -> bool:
    token = str(status or "").strip().lower()
    if token in _FAILED_PRODUCER_STATUSES:
        return True
    return bool(token) and token not in _OK_PRODUCER_STATUSES


def _outputs_satisfied_by(artifact: Any) -> set[str]:
    names: set[str] = set()
    kind = str(getattr(artifact, "kind", "") or "").lower()
    suffix = _suffix(artifact)
    mime = str(getattr(artifact, "mime", "") or "").lower()
    title = str(getattr(artifact, "title", "") or "").lower()
    is_figure = kind in _FIGURE_KINDS or suffix in _FIGURE_SUFFIXES or mime in _FIGURE_MIMES
    is_slides = suffix in _SLIDE_SUFFIXES or kind in {"slides", "pptx"}
    pays_figure_pptx = _pays_format_neutral_figure_pptx(
        kind=kind, suffix=suffix, title=title, path=_path_of(artifact)
    )
    if is_figure or pays_figure_pptx:
        names.add("artifact.figure")
        names.add("artifact")
    if _is_writing_artifact(artifact, kind=kind, suffix=suffix, mime=mime):
        names.update(WRITING_DELIVERABLES)
        names.add("artifact")
    if _pays_named_review(artifact, kind=kind, title=title):
        names.add("review")
        names.add("artifact")
    if is_slides:
        names.add("artifact.slides")
        names.add("artifact")
        if kind in _FIGURE_KINDS or pays_figure_pptx or (
            not kind and ("livefigure" in title or "editable" in title)
        ):
            names.add("artifact.pptx")
    if "poster" in kind or "poster" in title or (
        suffix in _POSTER_SUFFIXES and "poster" in title
    ):
        names.add("artifact.poster")
        names.add("artifact")
    if suffix in _PDF_SUFFIXES and kind in _MANUSCRIPT_KINDS and not is_figure:
        names.update(WRITING_DELIVERABLES)
        names.add("artifact")
    if "response" in kind or "response" in title:
        names.add("response_letter")
    return names & (CONTRACT_DELIVERABLES | {"artifact"})


def _is_writing_artifact(
    artifact: Any,
    *,
    kind: str,
    suffix: str,
    mime: str,
) -> bool:
    """Whether this artifact is a manuscript this task already registered.

    Codex treats the turn's written files as the deliverable. Named writing
    debts are paid by a text document on this task, not only by
    ``kind=report`` / ``.md``. Figures, decks, and data files stay out.
    """
    if kind == "review":
        return False
    if kind in _FIGURE_KINDS or suffix in _FIGURE_SUFFIXES or mime in _FIGURE_MIMES:
        return False
    if suffix in _SLIDE_SUFFIXES or kind in {"slides", "pptx", "data"}:
        return False
    if "poster" in kind:
        return False
    if kind in _MANUSCRIPT_KINDS or suffix in _MANUSCRIPT_SUFFIXES:
        return True
    if mime.startswith("text/") and mime not in {
        "text/vnd.graphviz",
        "text/csv",
    }:
        return True
    return kind == "file" and _path_is_utf8_text(artifact)


def _path_is_utf8_text(artifact: Any) -> bool:
    raw = _path_of(artifact)
    if not raw:
        return False
    path = Path(raw)
    if not path.is_file():
        return False
    try:
        probe = path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in probe:
        return False
    try:
        probe.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _pays_format_neutral_figure_pptx(
    *,
    kind: str,
    suffix: str,
    title: str,
    path: str,
) -> bool:
    """A single-slide figure PPTX pays ``artifact.figure``; a talk deck does not."""
    if suffix not in _SLIDE_SUFFIXES:
        return False
    if kind in {"slides"}:
        return False
    if kind in _FIGURE_KINDS:
        return True
    hay = f"{title} {path}".lower()
    return "livefigure" in hay


def _pays_named_review(artifact: Any, *, kind: str, title: str) -> bool:
    """``review`` is a paper-review deliverable, not every Markdown write.

    Store rows from ``write_file`` are ``kind=document``. The skill stores
    ``kind=review``. A drained ``ArtifactRef`` has no kind; only then does a
    review-named title count, so settlement can see the file before the store
    indexes it.
    """
    if kind == "review":
        return True
    if kind:
        return False
    path = _path_of(artifact)
    name = Path(path).name.lower() if path else ""
    return "review" in title or "review" in name


def _is_sidecar(artifact: Any) -> bool:
    if is_dot_artifact(artifact):
        return True
    suffix = _suffix(artifact)
    mime = str(getattr(artifact, "mime", "") or "").lower()
    return suffix in {"dot", "gv", "mmd", "json", "yaml", "yml", "log"} or mime in {
        "text/vnd.graphviz",
        "application/json",
        "application/yaml",
    }


def _suffix(artifact: Any) -> str:
    fmt = str(getattr(artifact, "format", "") or "").lower().lstrip(".")
    if fmt:
        return fmt
    path = _path_of(artifact)
    if path and "." in Path(path).name:
        return Path(path).suffix.lower().lstrip(".")
    return ""


def _path_of(artifact: Any) -> str:
    return str(
        getattr(artifact, "path", "")
        or getattr(artifact, "rel_path", "")
        or getattr(artifact, "uri", "")
        or ""
    )


__all__ = [
    "bind_contract_outputs",
    "incoming_plan_is_retrieve_only",
    "utterance_asks_only_source_ids",
    "utterance_asks_prior_task_status",
    "utterance_asks_written_survey",
    "infer_analysis_report_outputs",
    "infer_figure_and_paper_outputs",
    "infer_slide_outputs",
    "plan_owes_scientific_outputs",
    "grant_contract_write_tools",
    "CANONICAL_FILE_PRODUCERS",
    "failed_canonical_file_debts",
    "remaining_contract_files",
    "remaining_deliverables",
    "remaining_figure",
    "remaining_retry_context",
    "remaining_slides",
    "remaining_writing",
    "skip_completed_skills_note",
    "survey_closer_eligible",
]
