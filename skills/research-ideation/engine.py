"""OmniScientist adapter for the portable research-ideation pipeline."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_core():
    """Load sibling core.py even when engine.py is imported by absolute path."""
    candidate = Path(__file__).with_name("core.py")
    spec = importlib.util.spec_from_file_location("research_ideation_core", candidate)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {candidate}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_core = _load_core()


class _OmniLLMPort:
    """Bridge the synchronous portable pipeline to Omni's async host LLM."""

    def __init__(self, llm: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._llm = llm
        self._loop = loop
        self.temperature = 0.7

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = list(payload.get("messages") or [])
        tools = list(payload.get("tools") or [])
        future = asyncio.run_coroutine_threadsafe(
            self._llm.chat_with_tools(
                messages,
                tools,
                tool_choice=str(payload.get("tool_choice") or "auto"),
                temperature=float(payload.get("temperature", self.temperature)),
                max_tokens=int(payload.get("max_tokens", 16384)),
            ),
            self._loop,
        )
        result = future.result()
        message: dict[str, Any] = {"content": str(getattr(result, "content", "") or "")}
        tool_calls = [
            _tool_call_fragment(call, index)
            for index, call in enumerate(getattr(result, "tool_calls", ()) or ())
        ]
        if tool_calls:
            message["tool_calls"] = tool_calls
        payload: dict[str, Any] = {"choices": [{"message": message}]}
        usage = getattr(result, "usage", None)
        if isinstance(usage, dict) and usage:
            payload["usage"] = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        return payload


def _tool_call_fragment(call: Any, index: int) -> dict[str, Any]:
    """Normalize Omni tool calls to the portable OpenAI-compatible shape."""
    fragment = getattr(call, "to_message_fragment", None)
    if callable(fragment):
        value = fragment()
        if isinstance(value, dict):
            return value
    arguments = getattr(call, "arguments", {}) or {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": str(getattr(call, "id", "") or f"call_{index}"),
        "type": "function",
        "function": {
            "name": str(getattr(call, "name", "") or ""),
            "arguments": arguments,
        },
    }


def _pipeline_error(exc: BaseException) -> dict[str, Any]:
    """Translate model failures into scheduler-safe lifecycle metadata."""
    literature_failure = isinstance(exc, _core.LiteratureSearchError)
    non_retryable = not literature_failure and _core.is_non_retryable_llm_error(exc)
    code = (
        "literature_search_failed"
        if literature_failure
        else _core.classify_llm_error(exc)
        if non_retryable
        else "pipeline_error"
    )
    message = str(exc)
    return {
        "status": "error",
        "outcome": {"code": code},
        "error": message,
        "summary": f"Research ideation pipeline failed: {message}",
        "recoverable": not non_retryable,
        "blocking": non_retryable,
        "sources": [],
        "research": {"source_ids": [], "run_id": ""},
        "run_id": "",
        "error_info": {
            "code": code,
            "message": message,
            "retryable": not non_retryable,
            "workflow_recoverable": not non_retryable,
        },
    }


class ResearchIdeationEngine:
    @staticmethod
    def validate_params(
        *, arguments: dict | None = None, input_data: dict | None = None
    ) -> dict | None:
        data = arguments or input_data or {}
        if not (data.get("input") or data.get("query") or data.get("topic")):
            return {"error": "input (research question) is required"}
        return None

    async def execute(
        self, progress_callback: Any = None, **input_data: Any
    ) -> dict[str, Any]:

        question = str(
            input_data.get("input")
            or input_data.get("query")
            or input_data.get("topic")
            or ""
        )
        default_n_ideas = _core.DEFAULT_N_IDEAS
        n_ideas = max(
            1,
            min(5, int(input_data.get("n_ideas", default_n_ideas) or default_n_ideas)),
        )
        use_tools = bool(input_data.get("use_tools", True))

        ctx = getattr(self, "ctx", None)
        loop = asyncio.get_running_loop()
        host_llm = getattr(ctx, "llm", None)
        if host_llm is None:
            return _pipeline_error(
                _core.LLMConfigurationError("Omni LLM host service is not available")
            )
        s2_api_key = _resolve_s2_key(ctx)
        llm = _OmniLLMPort(host_llm, loop)
        search = _host_search_port(ctx, loop)

        async def _notify_progress(msg: str, frac: float) -> None:
            if progress_callback is None:
                return
            value = progress_callback(msg, frac)
            if hasattr(value, "__await__"):
                await value

        def _progress(msg: str, frac: float) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_notify_progress(msg, frac))
            )

        try:
            result = await asyncio.to_thread(
                _core.run_pipeline,
                research_question=question,
                n_ideas=n_ideas,
                use_tools=use_tools,
                progress=_progress,
                llm=llm,
                s2_api_key=s2_api_key,
                search=search,
            )
        except Exception as exc:
            return _pipeline_error(exc)

        report = _build_markdown_report(result)
        result["text"] = report

        # Native completion milestone: the pipeline counts (papers surveyed,
        # ideas generated) are known only here, so the durable line carries them
        # rather than a bare "complete" stage.
        if progress_callback is not None:
            steps = result.get("steps") if isinstance(result.get("steps"), dict) else {}
            search = steps.get("search") if isinstance(steps.get("search"), dict) else {}
            outcome = result.get("outcome") if isinstance(result.get("outcome"), dict) else {}
            stats: dict[str, Any] = {}
            if search.get("paper_count"):
                stats["papers"] = int(search["paper_count"])
            concepts = search.get("core_concepts")
            if isinstance(concepts, list) and concepts:
                stats["concepts"] = len(concepts)
            if outcome.get("count"):
                stats["ideas"] = int(outcome["count"])
            try:
                emitted = progress_callback(
                    "ideation complete",
                    1.0,
                    stage_id="ideation.done",
                    milestone="Ideation complete",
                    stats=stats,
                )
            except TypeError:
                emitted = progress_callback("ideation complete", 1.0)
            if hasattr(emitted, "__await__"):
                await emitted

        # Store the report when the Omni artifact service is available.
        if ctx is not None and getattr(ctx, "artifacts", None) is not None:
            try:
                stored = await ctx.artifacts.put_bytes(
                    report.encode("utf-8"),
                    kind="report",
                    title=f"Research ideation: {question[:50]}",
                    ext="md",
                    mime="text/markdown",
                    session_id=getattr(ctx, "session_id", ""),
                    task_id=getattr(ctx, "task_id", ""),
                    subtask_id=(
                        getattr(ctx, "subtask_id", "")
                        or getattr(ctx, "task_id", "")
                    ),
                )
                result["report_uri"] = stored.uri
                result["artifacts"] = [{"uri": stored.uri, "kind": "report", "ext": "md"}]
            except Exception:
                pass

        # Add discovered papers to the library when supported.
        papers = result.get("steps", {}).get("search", {})
        if ctx and papers.get("paper_count"):
            _save_to_library(ctx, result.get("steps", {}).get("search", {}).get("papers", []))

        await _record_research_provenance(ctx, result)

        return result


def _host_search_port(ctx: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """Route this run's literature through Omni's funnel instead of one connector.

    The funnel fans out across every enabled connector — arXiv, OpenAlex,
    Crossref, PubMed, Semantic Scholar — with health checks, backoff, and a local
    corpus floor, and it treats a connector error as data rather than raising.
    Going through it is what makes a missing Semantic Scholar key cost one source
    out of several instead of the entire search.

    Returns ``None`` outside Omni, where the portable Semantic Scholar path is
    the only source a standalone copy can assume.
    """
    if ctx is None or getattr(ctx, "settings", None) is None:
        return None

    from omni.research import search_literature

    def _search(query: str, limit: int) -> list[dict]:
        future = asyncio.run_coroutine_threadsafe(
            search_literature(ctx, query=query, rows=min(int(limit or 6), 25)),
            loop,
        )
        results = future.result().get("results") or []
        # The funnel names the field ``summary``; the pipeline reasons over
        # ``abstract``. Same text, and an idea generated from an empty abstract
        # is an idea generated from the title alone.
        return [
            {**paper, "abstract": paper.get("abstract") or paper.get("summary") or ""}
            for paper in results
            if isinstance(paper, dict)
        ]

    return _search


def _resolve_s2_key(ctx: Any) -> str:
    """Return this run's scoped Semantic Scholar key, or empty public-tier access.

    A disabled Semantic Scholar connector is not a run abort: the host funnel
    still has arXiv / OpenAlex / Crossref / PubMed. Only the S2 key is resolved
    here, and only when that connector is enabled, so a kill-switch cannot
    inherit a process-wide ``S2_API_KEY``.
    """
    if ctx is None or getattr(ctx, "settings", None) is None:
        return ""

    from omni.research.engine_util import resolve_connector

    resolved = resolve_connector(ctx, "semanticscholar")
    if resolved is None:
        return ""
    return str(resolved.secrets.get("semantic_scholar_api_key", "") or "")


def _save_to_library(ctx: Any, papers: list[dict]) -> None:
    paths = getattr(ctx, "paths", None)
    if paths is None or not papers:
        return
    try:
        from omni.research import add_papers_to_library

        add_papers_to_library(paths.library, papers)
    except Exception:
        pass


async def _record_research_provenance(ctx: Any, result: dict[str, Any]) -> None:
    """Attach real Omni source/run IDs while keeping DB-free output portable."""
    if ctx is None or getattr(ctx, "db", None) is None:
        return
    try:
        from omni.research import ResearchStore, capture_env_lock

        store = ResearchStore(ctx.db)
        source_records: list[dict[str, Any]] = []
        source_ids: list[str] = []
        papers = result.get("steps", {}).get("search", {}).get("papers", [])
        for paper in papers if isinstance(papers, list) else []:
            if not isinstance(paper, dict):
                continue
            source = await store.add_source(paper, origin="research-ideation")
            source_ids.append(source.id)
            source_records.append({**paper, "source_id": source.id})

        artifacts = result.get("artifacts")
        output_uris = (
            [
                str(artifact.get("uri"))
                for artifact in artifacts
                if isinstance(artifact, dict) and artifact.get("uri")
            ]
            if isinstance(artifacts, list)
            else []
        )
        run = await store.add_run(
            title=f"Research ideation: {result.get('research_question', '')}".strip(),
            session_id=str(getattr(ctx, "session_id", "") or ""),
            subtask_id=str(
                getattr(ctx, "subtask_id", "")
                or getattr(ctx, "task_id", "")
                or ""
            ),
            cmd="research-ideation",
            env_lock=capture_env_lock(),
            inputs={
                "research_question": result.get("research_question", ""),
                "queries": result.get("steps", {}).get("search", {}).get("queries", []),
            },
            output_uris=output_uris,
            metrics={
                "paper_count": len(source_ids),
                "gap_count": len(result.get("steps", {}).get("gaps", [])),
                "idea_count": len(result.get("steps", {}).get("raw_ideas", [])),
                "outcome": result.get("outcome", {}).get("code", ""),
            },
            status=(
                "succeeded"
                if result.get("status") == "ok"
                else "degraded"
                if result.get("status") == "partial"
                else "failed"
            ),
        )
        result["sources"] = source_records
        result["research"] = {"source_ids": source_ids, "run_id": run.id}
        result["run_id"] = run.id
    except Exception:
        result.setdefault("research", {"source_ids": [], "run_id": ""})
        result.setdefault("run_id", "")


def _build_markdown_report(result: dict) -> str:
    def _join_values(value: Any, *, limit: int | None = None) -> str:
        values = value if isinstance(value, list) else [value]
        if limit is not None:
            values = values[:limit]
        return ", ".join(str(item) for item in values if item not in (None, ""))

    question = str(result.get("research_question") or "")
    lines = [f"# Research Ideation Report: {question}", ""]

    raw_steps = result.get("steps")
    steps = raw_steps if isinstance(raw_steps, dict) else {}
    raw_search = steps.get("search")
    search = raw_search if isinstance(raw_search, dict) else {}
    if search:
        lines += [
            "## Literature Review",
            f"- Search queries: {_join_values(search.get('queries', []))}",
            f"- Relevant papers: {search.get('paper_count', 0)}",
            f"- Core concepts: {_join_values(search.get('core_concepts', []), limit=10)}",
            f"- Application domains: {_join_values(search.get('domain_concepts', []), limit=10)}",
            "",
        ]
        raw_papers = search.get("papers")
        papers = raw_papers if isinstance(raw_papers, list) else []
        titles = [
            str(paper.get("title") or "").strip()
            for paper in papers
            if isinstance(paper, dict)
            and str(paper.get("title") or "").strip()
        ]
        if titles:
            lines.append("### Retrieved paper titles")
            lines.extend(
                f"{index}. {title}"
                for index, title in enumerate(titles, 1)
            )
            lines.append("")

    gaps = steps.get("gaps")
    if isinstance(gaps, list) and gaps:
        lines += ["## Research Gaps", ""]
        for g in gaps:
            if not isinstance(g, dict):
                continue
            lines += [
                f"### Gap {g.get('gap_id', '?')}",
                str(g.get("gap") or ""),
                f"- Rationale: {g.get('source', '')}",
                "",
            ]

    raw_ideas = steps.get("raw_ideas")
    thinking_blocks: list[str] = []
    for index, idea in enumerate(raw_ideas if isinstance(raw_ideas, list) else [], 1):
        if not isinstance(idea, dict):
            continue
        brainstorming = str(idea.get("brainstorming") or "").strip()
        if not brainstorming:
            continue
        title = str(idea.get("title") or f"Candidate {index}").strip()
        thinking_blocks.extend(
            [
                f"### Candidate {index}: {title}",
                brainstorming,
                "",
            ]
        )
    if thinking_blocks:
        lines.extend(["## Concept-Level Reasoning", "", *thinking_blocks])

    raw_final = result.get("final_idea")
    final = raw_final if isinstance(raw_final, dict) else {}
    if final:
        lines += [
            "## Final Idea",
            f"### {final.get('title', '')}",
            "",
            f"**Background**: {final.get('background', '')}",
            "",
            f"**Related work**: {final.get('related_work', '')}",
            "",
            f"**Gap analysis**: {final.get('gap_analysis', '')}",
            "",
            f"**Method**: {final.get('proposed_method', '')}",
            "",
        ]

    return "\n".join(lines)
