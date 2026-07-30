"""Research builtin tools — the agent's structured research action surface.

These tools turn the agent's prose into the Research Object Model: recording
hypotheses/claims, citing sources, binding evidence to claims, searching the
local literature corpus, and logging experiment runs. They are appended to the
baseline builtin tool surface (see ``builtin_tools.build_builtin_tools``) so the
main ReAct loop, native `omni lit`, and compatible imported skills get them.

Every tool is additive and best-effort: writing a structured row also feeds a
memory entry (so existing hybrid recall benefits, with no change
to the memory design) and a NOTEBOOK line, but a failure there never breaks the
tool. Tools that need the store no-op gracefully when ``ctx.db`` is absent.
"""

from __future__ import annotations

import hashlib
from importlib import metadata as importlib_metadata
from typing import Any

from omni.core.identifiers import short_id
from omni.core.react_agent import ToolSpec
from omni.memory.notebook import append_entry
from omni.research.corpus import search_corpus
from omni.research.store import ResearchStore
from omni.skills_runtime.context import ExecContext, Tool

# Packages whose versions form a lightweight, deterministic environment lock for
# the run ledger (avoids spawning ``pip freeze`` on every log_run).
_ENV_LOCK_PKGS = (
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    "scikit-learn",
    "torch",
    "OmniScientist-V2",
)


def _store(ctx: ExecContext) -> ResearchStore | None:
    return ResearchStore(ctx.db) if getattr(ctx, "db", None) is not None else None


def _as_of(ctx: ExecContext) -> str:
    return getattr(getattr(ctx.settings, "research", None), "as_of", "") or ""


async def _feed_memory(
    ctx: ExecContext, *, summary: str, memory_type: str, importance: float = 0.5
) -> None:
    """Mirror a research object into memory so recall can find it (best-effort)."""
    try:
        from omni.memory.service import MemoryLayer, MemoryService, open_global_store

        mem = MemoryService(
            ctx.db, ctx.settings, llm=ctx.llm, global_db=open_global_store(ctx.settings)
        )
        await mem.record(
            layer=MemoryLayer.SEMANTIC, scope="session", scope_id=ctx.session_id,
            summary=summary, memory_type=memory_type, importance=importance,
        )
    except Exception:  # noqa: BLE001 — memory is advisory
        pass


def _notebook(ctx: ExecContext, title: str, body: str, *, tags: list[str] | None = None) -> None:
    try:
        append_entry(ctx.paths.notebook, title, body, tags=tags)
    except Exception:  # noqa: BLE001 — notebook is advisory
        pass


def capture_env_lock() -> str:
    """A short, deterministic environment fingerprint for run reproducibility."""
    import sys

    parts = [f"python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"]
    for pkg in _ENV_LOCK_PKGS:
        try:
            parts.append(f"{pkg}=={importlib_metadata.version(pkg)}")
        except importlib_metadata.PackageNotFoundError:
            continue
    body = "\n".join(parts)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}\n{body}"


def build_research_tools(ctx: ExecContext) -> list[Tool]:
    """Build the research action tools bound to ``ctx`` (needs ``ctx.db``)."""
    store = _store(ctx)
    if store is None:
        return []

    # ── record_hypothesis ──
    async def record_hypothesis(args: dict) -> Any:
        statement = str(args.get("statement", "")).strip()
        if not statement:
            return {"error": "statement is required"}
        hyp = await store.add_hypothesis(
            statement, session_id=ctx.session_id,
            rationale=str(args.get("rationale", "")),
            status=str(args.get("status", "proposed")),
            confidence=float(args.get("confidence", 0.5) or 0.5),
            tags=args.get("tags") or [],
        )
        await _feed_memory(ctx, summary=f"Hypothesis: {statement}", memory_type="hypothesis", importance=0.6)
        _notebook(ctx, f"Hypothesis {hyp.id[:8]}", f"{statement}\n\nStatus: {hyp.status} (confidence {hyp.confidence:.2f})",
                  tags=["hypothesis"])
        return {"status": "ok", "hypothesis_id": hyp.id, "state": hyp.status}

    # ── record_claim ──
    async def record_claim(args: dict) -> Any:
        text = str(args.get("text", "")).strip()
        if not text:
            return {"error": "text is required"}
        claim = await store.add_claim(
            text, session_id=ctx.session_id,
            hypothesis_id=str(args.get("hypothesis_id", "")),
            polarity=str(args.get("polarity", "assert")),
            confidence=float(args.get("confidence", 0.5) or 0.5),
            made_by="agent",
        )
        await _feed_memory(ctx, summary=f"Claim: {text}", memory_type="claim", importance=0.55)
        return {"status": "ok", "claim_id": claim.id,
                "note": "Use add_evidence to bind this claim to a source; otherwise verify will flag it as unsupported."}

    # ── cite_source ──
    async def cite_source(args: dict) -> Any:
        meta = {k: args.get(k) for k in
                ("title", "arxiv_id", "doi", "url", "authors", "year", "summary", "venue", "kind")
                if args.get(k) is not None}
        if not (meta.get("title") or meta.get("arxiv_id") or meta.get("doi") or meta.get("url")):
            return {"error": "need at least one of: title, arxiv_id, doi, url"}
        src = await store.add_source(meta, origin=str(args.get("origin", "manual")), date_pin=_as_of(ctx))
        _save_to_library(ctx, meta)
        return {"status": "ok", "source_id": src.id, "title": src.title,
                "dedup_key": src.dedup_key}

    # ── add_evidence ──
    async def add_evidence(args: dict) -> Any:
        claim_id = str(args.get("claim_id", "")).strip()
        if not claim_id:
            return {"error": "claim_id is required"}
        claim = await store.get_claim(claim_id)
        if claim is None:
            return {"error": f"unknown claim '{claim_id}'; create it with record_claim first"}
        source_id = str(args.get("source_id", "")).strip()
        if source_id:
            src = await store.get_source(source_id)
            if src is None:
                return {"error": f"unknown source '{source_id}'; record it with cite_source first"}
            source_id = src.id
        ev = await store.add_evidence(
            claim.id, source_id=source_id, chunk_id=str(args.get("chunk_id", "")),
            stance=str(args.get("stance", "supports")),
            quote=str(args.get("quote", "")), locator=str(args.get("locator", "")),
            strength=float(args.get("strength", 0.6) or 0.6),
        )
        return {"status": "ok", "evidence_id": ev.id, "claim_id": claim.id, "stance": ev.stance}

    # ── search_corpus ──
    async def search_corpus_tool(args: dict) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        k = int(args.get("k", getattr(ctx.settings.research, "corpus_top_k", 6)) or 6)
        research = ctx.settings.research
        passages = await search_corpus(
            store, ctx.llm, query, k=k, as_of=_as_of(ctx),
            hybrid=bool(getattr(research, "hybrid_rerank", True)),
            rrf_k=int(getattr(research, "rrf_k", 60) or 60),
            vector_backend=str(getattr(ctx.settings.memory, "vector_backend", "auto") or "auto"),
        )
        if not passages:
            return {"status": "empty", "matches": [],
                    "note": "The local corpus is empty or has no match. Run openalex-search or search a connector and cite_source."}
        return {"status": "ok",
                "matches": [p.to_dict(i) for i, p in enumerate(passages, 1)],
                "note": "Use [S#] inline citations and call add_evidence(claim_id, source_id) for each recorded claim."}

    # ── search_literature (single resilient funnel) ──
    async def search_literature(args: dict) -> Any:
        # Delegate to the one health-aware funnel so every caller gets
        # Retry-After + backoff, burst-vs-quota classification, concurrent
        # multi-connector fan-out, circuit breaking, and the local-corpus floor
        # — no per-provider branches here (RC6: no drifting second copy).
        from omni.research.retrieval import search_literature as _funnel

        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        rows = max(1, min(int(args.get("rows", 6) or 6), 25))
        sources = [str(s).strip().lower() for s in (args.get("sources") or []) if str(s).strip()]
        return await _funnel(ctx, query=query, rows=rows, sources=sources or None)

    # ── citation_neighbors ──
    async def citation_neighbors_tool(args: dict) -> Any:
        from omni.research.citations import traverse

        src_id = str(args.get("source_id", "")).strip()
        src = await store.get_source(src_id) if src_id else None
        if src is None:
            meta = {k: args.get(k, "") for k in ("title", "doi", "arxiv_id", "url")}
            if any(meta.values()):
                src = await store.find_source(meta)
        if src is None:
            return {"status": "empty",
                    "note": "Source not found. Record it with cite_source or openalex-search, or expand the citation graph through a connector."}
        direction = str(args.get("direction", "references")).strip().lower()
        if direction not in ("references", "cited_by"):
            direction = "references"
        depth = max(1, min(int(args.get("depth", 1) or 1), 3))
        limit = max(1, min(int(args.get("limit", 50) or 50), 200))
        hood = await traverse(store, src.id, direction=direction, depth=depth, limit=limit)
        if not hood.nodes:
            return {"status": "empty", "seed_source_id": src.id, "direction": direction,
                    "note": "The local citation graph has no edges in this direction."}
        return {"status": "ok", **hood.to_dict(),
                "note": "Use search_corpus and add_evidence on neighboring source ids for a grounded review."}

    # ── package_artifact ──
    async def package_artifact(args: dict) -> Any:
        artifact_uri = str(args.get("artifact_uri", "")).strip()
        if not artifact_uri:
            return {"error": "artifact_uri is required"}
        artifacts = getattr(ctx, "artifacts", None)
        if artifacts is None:
            return {"error": "artifact store unavailable"}
        from omni.research.repro import build_repro_bundle

        result = await build_repro_bundle(
            artifacts=artifacts, store=store, paths=ctx.paths,
            artifact_uri=artifact_uri, title=str(args.get("title", "")),
            command=str(args.get("command", "")), code=str(args.get("code", "")),
            code_filename=str(args.get("code_filename", "")),
            seed=_int_or_none(args.get("seed")),
            inputs=args.get("inputs") or {}, metrics=args.get("metrics") or {},
            session_id=ctx.session_id,
            task_id=getattr(ctx, "task_id", ""),
            subtask_id=getattr(ctx, "subtask_id", ""),
        )
        if result.get("status") == "ok":
            bundle_id = str(result["bundle_uri"]).removeprefix("artifact://")
            _notebook(
                ctx, f"Reproducibility bundle {short_id(bundle_id)}",
                f"{args.get('title') or artifact_uri}\n\nsha256: {result['artifact_sha256'][:16]}…\nBundle: {result['bundle_uri']}",
                tags=["repro"],
            )
        return result

    # ── build_research_artifact ──
    async def build_research_artifact(args: dict) -> Any:
        artifacts = getattr(ctx, "artifacts", None)
        if artifacts is None:
            return {"status": "error", "error": "artifact store unavailable"}
        from omni.research.artifacts import build_evidence_table, build_research_notebook

        kind = str(args.get("kind") or "evidence_table").strip().lower()
        title = str(args.get("title") or "").strip()
        limit = max(1, min(int(args.get("limit", 200) or 200), 1000))
        if kind == "evidence_table":
            result = await build_evidence_table(
                store=store, artifacts=artifacts, session_id=ctx.session_id,
                task_id=getattr(ctx, "task_id", ""),
                title=title or "Evidence table", limit=limit,
            )
        elif kind == "research_notebook":
            result = await build_research_notebook(
                store=store, artifacts=artifacts, session_id=ctx.session_id,
                task_id=getattr(ctx, "task_id", ""),
                title=title or "Research notebook", limit=limit,
            )
        else:
            return {"status": "error", "error": f"unknown research artifact kind: {kind}"}
        _notebook(
            ctx, f"Research artifact {kind}", result.get("summary", ""), tags=["artifact", kind]
        )
        return result

    # ── figure/code/data consistency ──
    async def build_figure_bundle_tool(args: dict) -> Any:
        artifacts = getattr(ctx, "artifacts", None)
        if artifacts is None:
            return {"status": "error", "error": "artifact store unavailable"}
        from omni.research.artifacts import build_figure_bundle

        return await build_figure_bundle(
            artifacts=artifacts,
            figure_uri=str(args.get("figure_uri") or ""),
            code_uri=str(args.get("code_uri") or ""),
            data_uris=[str(uri) for uri in args.get("data_uris") or [] if str(uri)],
            run_ids=[str(run_id) for run_id in args.get("run_ids") or [] if str(run_id)],
            session_id=ctx.session_id,
            task_id=getattr(ctx, "task_id", ""),
            subtask_id=getattr(ctx, "subtask_id", ""),
            title=str(args.get("title") or "Scientific figure bundle"),
        )

    async def verify_figure_bundle_tool(args: dict) -> Any:
        artifacts = getattr(ctx, "artifacts", None)
        if artifacts is None:
            return {"status": "error", "error": "artifact store unavailable"}
        manifest_uri = str(args.get("manifest_uri") or "")
        manifest = args.get("manifest") if isinstance(args.get("manifest"), dict) else None
        if manifest is None and manifest_uri:
            import json

            path = await artifacts.resolve_path(manifest_uri)
            if path is None or not path.is_file():
                return {"status": "error", "error": f"manifest not found: {manifest_uri}"}
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return {"status": "error", "error": f"invalid figure manifest: {exc}"}
        if manifest is None:
            return {"status": "error", "error": "manifest_uri or manifest is required"}
        from omni.research.artifacts import verify_figure_bundle

        result = await verify_figure_bundle(artifacts=artifacts, manifest=manifest)
        result["status"] = "passed" if result.get("passed") else "failed"
        return result

    # ── deterministic statistical reviewer ──
    async def review_statistics(args: dict) -> Any:
        from omni.research.quality import evaluate_statistical_correctness

        result = evaluate_statistical_correctness(args)
        return {
            "status": "passed" if result.passed else "failed",
            "score": result.score,
            "checks": [check.to_dict() for check in result.checks],
            "metrics": result.metrics,
        }

    # ── attach_provenance ──
    async def attach_provenance(args: dict) -> Any:
        from omni.research.provenance import ProvenanceCapsule

        artifact_uri = str(args.get("artifact_uri", "")).strip()
        if not artifact_uri:
            return {"error": "artifact_uri is required"}

        source_ids = [str(x) for x in (args.get("source_ids") or []) if str(x).strip()]
        # inline citation: cite each metadata dict and fold its id into the capsule
        for meta in args.get("sources") or []:
            if not isinstance(meta, dict):
                continue
            if not (meta.get("title") or meta.get("arxiv_id") or meta.get("doi") or meta.get("url")):
                continue
            src = await store.add_source(meta, origin="provenance", date_pin=_as_of(ctx))
            _save_to_library(ctx, meta)
            source_ids.append(src.id)

        claim_ids = [str(x) for x in (args.get("claim_ids") or []) if str(x).strip()]
        for text in args.get("claims") or []:
            text = str(text).strip()
            if not text:
                continue
            claim = await store.add_claim(text, session_id=ctx.session_id, made_by="agent")
            claim_ids.append(claim.id)

        capsule = ProvenanceCapsule(
            artifact_uri=artifact_uri,
            title=str(args.get("title", "")),
            source_ids=_dedup_ids(source_ids),
            claim_ids=_dedup_ids(claim_ids),
            evidence_ids=[str(x) for x in (args.get("evidence_ids") or []) if str(x).strip()],
            run_ids=[str(x) for x in (args.get("run_ids") or []) if str(x).strip()],
            tool_calls=[str(x) for x in (args.get("tool_calls") or []) if str(x).strip()],
            artifact_sha256=str(args.get("artifact_sha256", "")),
            notes=str(args.get("notes", "")),
        )
        grounded, reasons = capsule.completeness()

        # attach to the artifact row (best-effort — capsule still recorded if absent)
        artifacts = getattr(ctx, "artifacts", None)
        attached = False
        if artifacts is not None and hasattr(artifacts, "set_meta"):
            try:
                attached = await artifacts.set_meta(artifact_uri, {"provenance": capsule.to_dict()})
            except Exception:  # noqa: BLE001 — meta attach is best-effort
                attached = False

        await _record_provenance_event(ctx, capsule, grounded=grounded)
        _notebook(
            ctx, f"Provenance capsule {artifact_uri[-12:]}",
            f"{capsule.title or artifact_uri}\n\nSources {len(capsule.source_ids)} · claims {len(capsule.claim_ids)}"
            f" · evidence {len(capsule.evidence_ids)}\nGrounded: {'yes' if grounded else 'no'}",
            tags=["provenance"],
        )
        return {
            "status": "ok" if grounded else "incomplete",
            "artifact_uri": artifact_uri,
            "grounded": grounded,
            "attached": attached,
            "source_ids": capsule.source_ids,
            "claim_ids": capsule.claim_ids,
            "reasons": reasons,
            "note": ("Artifact is bound to sources and claims, so its grounding can be audited by `omni verify`."
                     if grounded else "The capsule is empty; provide sources/claims or source_ids/claim_ids for traceability."),
        }

    # ── log_run ──
    async def log_run(args: dict) -> Any:
        metrics = args.get("metrics") or {}
        if isinstance(metrics, str):
            metrics = {"note": metrics}
        run = await store.add_run(
            title=str(args.get("title", "")), session_id=ctx.session_id,
            hypothesis_id=str(args.get("hypothesis_id", "")),
            cmd=str(args.get("cmd", "")), code_uri=str(args.get("code_uri", "")),
            seed=_int_or_none(args.get("seed")),
            env_lock=str(args.get("env_lock") or capture_env_lock()),
            inputs=args.get("inputs") or {},
            output_uris=args.get("output_uris") or [],
            metrics=metrics, status=str(args.get("status", "succeeded")),
        )
        nums = ", ".join(f"{k}={v}" for k, v in list(metrics.items())[:6]) if isinstance(metrics, dict) else ""
        _notebook(ctx, f"Experiment run {run.id[:8]}", f"{run.title or run.cmd}\n\nMetrics: {nums or '—'}\nSeed: {run.seed}",
                  tags=["run"])
        await _feed_memory(ctx, summary=f"Experiment run {run.id[:8]}: {run.title} {nums}",
                           memory_type="run", importance=0.6)
        return {"status": "ok", "run_id": run.id,
                "note": f"Cite this value as (run {run.id[:8]}) for traceability."}

    return [
        Tool(ToolSpec(
            "record_hypothesis",
            "Record a falsifiable hypothesis in the research object store and return hypothesis_id.",
            {"type": "object", "properties": {
                "statement": {"type": "string", "description": "Falsifiable hypothesis statement"},
                "rationale": {"type": "string"},
                "status": {"type": "string", "enum": list(_HYP_STATES)},
                "confidence": {"type": "number", "description": "0-1"},
            }, "required": ["statement"]},
        ), record_hypothesis),
        Tool(ToolSpec(
            "record_claim",
            "Record a claim that requires evidence. Returns claim_id for use with add_evidence.",
            {"type": "object", "properties": {
                "text": {"type": "string"},
                "hypothesis_id": {"type": "string"},
                "polarity": {"type": "string", "enum": ["assert", "negate", "open"]},
                "confidence": {"type": "number", "description": "0-1"},
            }, "required": ["text"]},
        ), record_claim),
        Tool(ToolSpec(
            "cite_source",
            "Record a paper, webpage, or dataset in the research object and literature stores; returns source_id.",
            {"type": "object", "properties": {
                "title": {"type": "string"}, "arxiv_id": {"type": "string"},
                "doi": {"type": "string"}, "url": {"type": "string"},
                "authors": {"type": "array", "items": {"type": "string"}},
                "year": {"type": "string"}, "summary": {"type": "string"},
                "kind": {"type": "string", "enum": ["paper", "web", "dataset", "book", "other"]},
            }},
        ), cite_source),
        Tool(ToolSpec(
            "add_evidence",
            "Bind a claim to a source with a supporting, contradicting, or mentioning provenance edge and optional excerpt/location.",
            {"type": "object", "properties": {
                "claim_id": {"type": "string"},
                "source_id": {"type": "string"},
                "chunk_id": {"type": "string"},
                "stance": {"type": "string", "enum": list(_STANCES)},
                "quote": {"type": "string"}, "locator": {"type": "string"},
                "strength": {"type": "number", "description": "0-1"},
            }, "required": ["claim_id"]},
        ), add_evidence),
        Tool(ToolSpec(
            "search_corpus",
            "Search the local literature corpus and return grounded passages labeled [S#] for inline citation and add_evidence.",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "Maximum passages to return; default 6"},
            }, "required": ["query"]},
        ), search_corpus_tool),
        Tool(ToolSpec(
            "search_literature",
            "Search enabled scholarly connectors such as arXiv, OpenAlex, Crossref, PubMed, and Semantic Scholar. "
            "Matches are indexed locally for later search_corpus grounding.",
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "Optional connector subset, e.g. [\"openalex\", \"pubmed\"]; empty means all enabled"},
                "rows": {"type": "integer", "description": "Results per connector; default 6, maximum 25"},
            }, "required": ["query"]},
        ), search_literature),
        Tool(ToolSpec(
            "citation_neighbors",
            "Traverse the local citation graph to return references or citing works for a source.",
            {"type": "object", "properties": {
                "source_id": {"type": "string", "description": "Recorded source id, or locate by title, DOI, or arXiv id"},
                "title": {"type": "string"}, "doi": {"type": "string"}, "arxiv_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["references", "cited_by"]},
                "depth": {"type": "integer", "description": "Traversal depth from 1 to 3; default 1"},
                "limit": {"type": "integer", "description": "Maximum nodes; default 50"},
            }},
        ), citation_neighbors_tool),
        Tool(ToolSpec(
            "package_artifact",
            "Package an artifact with creation command/code, environment fingerprint, git state, and sha256 into a reproducible zip bundle.",
            {"type": "object", "properties": {
                "artifact_uri": {"type": "string", "description": "Artifact URI or path to package"},
                "title": {"type": "string"},
                "command": {"type": "string", "description": "Command that generated the artifact; alternative to code"},
                "code": {"type": "string", "description": "Source code that generated the artifact; alternative to command"},
                "code_filename": {"type": "string", "description": "Filename for supplied code; default code.py"},
                "seed": {"type": "integer"},
                "inputs": {"type": "object"},
                "metrics": {"type": "object"},
            }, "required": ["artifact_uri"]},
        ), package_artifact),
        Tool(ToolSpec(
            "build_research_artifact",
            "Generate a structured artifact from the research ledger: an evidence table or notebook snapshot.",
            {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["evidence_table", "research_notebook"]},
                "title": {"type": "string"},
                "limit": {"type": "integer", "description": "Maximum objects to read; default 200"},
            }},
        ), build_research_artifact),
        Tool(ToolSpec(
            "build_figure_bundle",
            "Bind a scientific figure to generating code, input data, and experiment run in a path-independent hash manifest.",
            {"type": "object", "properties": {
                "figure_uri": {"type": "string"},
                "code_uri": {"type": "string"},
                "data_uris": {"type": "array", "items": {"type": "string"}},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"},
            }, "required": ["figure_uri", "code_uri", "data_uris", "run_ids"]},
        ), build_figure_bundle_tool),
        Tool(ToolSpec(
            "verify_figure_bundle",
            "Rehash figure, code, and data and verify consistency with the manifest and experiment run.",
            {"type": "object", "properties": {
                "manifest_uri": {"type": "string"},
                "manifest": {"type": "object"},
            }},
        ), verify_figure_bundle_tool),
        Tool(ToolSpec(
            "review_statistics",
            "Deterministically audit structured statistical assertions, including p-values, intervals, sample size, variance, standard error, degrees of freedom, and tolerances.",
            {"type": "object", "properties": {
                "assertions": {"type": "array", "items": {"type": "object"}},
            }, "required": ["assertions"]},
        ), review_statistics),
        Tool(ToolSpec(
            "attach_provenance",
            "Attach a provenance capsule containing sources, claims, evidence, experiment runs, and tool trace to an artifact for verification.",
            {"type": "object", "properties": {
                "artifact_uri": {"type": "string", "description": "Artifact URI, path, or label"},
                "title": {"type": "string"},
                "source_ids": {"type": "array", "items": {"type": "string"}},
                "sources": {"type": "array", "items": {"type": "object"},
                            "description": "Inline source metadata such as title, DOI, arXiv id, or URL; sources are recorded automatically"},
                "claim_ids": {"type": "array", "items": {"type": "string"}},
                "claims": {"type": "array", "items": {"type": "string"}, "description": "Inline claim text; claims are recorded automatically"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "tool_calls": {"type": "array", "items": {"type": "string"}, "description": "Tools used to create the artifact"},
                "notes": {"type": "string"},
            }, "required": ["artifact_uri"]},
        ), attach_provenance),
        Tool(ToolSpec(
            "log_run",
            "Record an experiment or compute run with command, seed, metrics, and artifacts; returns run_id for provenance.",
            {"type": "object", "properties": {
                "title": {"type": "string"}, "cmd": {"type": "string"},
                "seed": {"type": "integer"}, "code_uri": {"type": "string"},
                "metrics": {"type": "object"},
                "output_uris": {"type": "array", "items": {"type": "string"}},
                "hypothesis_id": {"type": "string"},
                "status": {"type": "string", "enum": ["succeeded", "failed", "recorded"]},
            }},
        ), log_run),
    ]


_HYP_STATES = ("proposed", "testing", "supported", "refuted", "inconclusive")
_STANCES = ("supports", "contradicts", "mentions")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


def _dedup_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        i = str(i).strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


_LIT_FIELDS = (
    "title", "authors", "year", "doi", "arxiv_id", "url", "venue", "origin",
    "category", "nct_id", "status", "phases", "conditions", "interventions",
    "primary_outcomes", "has_results",
)


def _paper_key(paper: dict[str, Any]) -> str:
    """Stable identity for cross-connector de-dup: DOI ▸ arXiv id ▸ title."""
    for key in ("doi", "arxiv_id"):
        val = str(paper.get(key) or "").strip().lower()
        if val:
            return f"{key}:{val}"
    return "title:" + " ".join(str(paper.get("title") or "").lower().split())


def _dedup_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-dup aggregated connector hits and project to the compact return shape."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in papers:
        key = _paper_key(p)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({k: p.get(k) for k in _LIT_FIELDS if p.get(k)})
    return out


async def _run_connector(name: str, query: str, rows: int, reg: Any) -> list[dict[str, Any]]:
    """Dispatch one connector search with its scoped secrets (never cross-read)."""
    from omni.research import connectors as _c

    resolved = reg.resolve(name)
    secrets = dict(resolved.secrets) if resolved is not None else {}
    email = str(secrets.get("contact_email", "") or "")
    if name == "openalex":
        return await _c.openalex_search(query, rows=rows, email=email)
    if name == "crossref":
        return await _c.crossref_search(query, rows=rows, email=email)
    if name == "pubmed":
        return await _c.pubmed_search(query, rows=rows, email=email)
    if name == "semanticscholar":
        return await _c.semanticscholar_search(
            query, rows=rows, api_key=str(secrets.get("semantic_scholar_api_key", "") or "")
        )
    if name == "biorxiv":
        return await _c.biorxiv_search(query, rows=rows)
    if name == "clinicaltrials":
        return await _c.clinicaltrials_search(query, rows=rows)
    if name == "arxiv":
        from omni.research.arxiv import search as arxiv_search

        found = await arxiv_search(query, max_results=rows)
        for r in found:
            r.setdefault("origin", "arxiv")
        return found
    return []


async def _record_provenance_event(ctx: ExecContext, capsule: Any, *, grounded: bool) -> None:
    """Record a durable ``provenance.capsule`` run event (best-effort).

    This is what the honesty audit and the eval harness inspect — evidence that an
    artifact was shipped *with* its grounding rather than naked.
    """
    db = getattr(ctx, "db", None)
    run_id = getattr(ctx, "task_id", "") or ""
    if db is None or not run_id:
        return
    try:
        from omni.runtime.task_recorder import TaskRecorder

        payload = capsule.to_dict()
        payload["complete"] = grounded
        # surface ids at top level so TaskRecorder merges them onto the run too
        payload["source_ids"] = capsule.source_ids
        payload["claim_ids"] = capsule.claim_ids
        recorder = TaskRecorder(db, project=getattr(ctx, "project", "default") or "default")
        await recorder.append_event(
            run_id,
            event_type="provenance.capsule",
            status="succeeded" if grounded else "degraded",
            name="provenance",
            output_json=payload,
            summary=f"provenance capsule for {capsule.artifact_uri} grounded={grounded}",
        )
    except Exception:  # noqa: BLE001 — provenance event is best-effort, never fatal.
        pass


def _save_to_library(ctx: ExecContext, meta: dict[str, Any]) -> None:
    """Keep ``library.jsonl`` in sync so ``omni cite export`` still works."""
    paths = getattr(ctx, "paths", None)
    if paths is None:
        return
    try:
        from omni.memory.library import add_papers

        add_papers(paths.library, [meta])
    except Exception:  # noqa: BLE001
        pass


__all__ = ["build_research_tools", "capture_env_lock"]
