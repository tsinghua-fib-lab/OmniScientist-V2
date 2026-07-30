"""scientific-figure engine — reproducible, provenance-aware figures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ScientificFigureEngine:
    @staticmethod
    def validate_params(*, arguments: dict | None = None, input_data: dict | None = None) -> dict | None:
        data = arguments or input_data or {}
        prompt = str(data.get("input") or data.get("query") or data.get("prompt") or "")
        if not prompt:
            return {"error": "input is required"}
        if str(data.get("revision_mode") or "").strip() and not _source_dot(data):
            return {
                "error": "source_artifact_required",
                "summary": "A figure revision requires source_artifact_path or source_artifact_dot.",
                "recoverable": True,
                "blocking": True,
            }
        return None

    async def execute(self, progress_callback: Any = None, **input_data: Any) -> dict[str, Any]:
        prompt = str(input_data.get("input") or input_data.get("query") or input_data.get("prompt") or "")
        source_dot = _source_dot(input_data)
        source_title = str(input_data.get("source_artifact_title") or (_dot_title(source_dot) if source_dot else ""))
        title = str(input_data.get("title") or source_title or "Scientific Figure")
        ctx = getattr(self, "ctx", None)

        await _progress(progress_callback, "plan figure", 0.1)
        revision: dict[str, Any] = {}
        requested_figure_kind = _figure_kind(input_data)
        figure_kind, kind_gate = _resolve_creation_kind(
            input_data,
            prompt=prompt,
            title=title,
            source_text=source_dot,
        )
        effective_inputs = _effective_inputs(
            input_data,
            prompt=prompt,
            title=title,
            requested_figure_kind=requested_figure_kind,
            figure_kind=figure_kind,
            revision=bool(source_dot),
        )
        if source_dot:
            dot = _major_revision_dot(source_dot, prompt=prompt, title=title, kind=figure_kind)
            revision = {
                "mode": "major",
                "source_task_id": str(input_data.get("source_task_id") or ""),
                "source_artifact_path": str(input_data.get("source_artifact_path") or ""),
            }
            quality = _quality_gate(
                source_dot,
                dot,
                constraints=input_data.get("revision_constraints") if isinstance(input_data.get("revision_constraints"), dict) else {},
            )
            revision["quality"] = quality
            if not quality["passed"]:
                return {
                    "status": "error",
                    "title": title,
                    "summary": "The revision failed its quality gate; no lower-quality artifact was emitted.",
                    "error": quality["reason"],
                    "revision": revision,
                    "figure_kind": figure_kind,
                    "requested_figure_kind": requested_figure_kind,
                    "deliverable_assessment": _figure_assessment(
                        ctx,
                        input_data,
                        effective_inputs=effective_inputs,
                        kind_gate=kind_gate,
                        status="failed",
                        summary=str(quality["reason"]),
                    ),
                }
        else:
            dot = _figure_dot(figure_kind, title)

        await _progress(progress_callback, "render graphviz", 0.35)
        rendered = await _render_graphviz(dot)

        await _progress(progress_callback, "save artifacts", 0.55)
        artifacts = []
        artifact_meta = _revision_artifact_meta(revision)
        dot_art = await _store_artifact(
            ctx,
            dot.encode("utf-8"),
            title=f"{title} DOT",
            fmt="dot",
            mime="text/vnd.graphviz",
            meta=artifact_meta,
        )
        if dot_art:
            artifacts.append(dot_art)
        svg_bytes = rendered.get("svg") or _fallback_svg(figure_kind, title).encode("utf-8")
        svg_art = await _store_artifact(ctx, svg_bytes, title=f"{title} SVG", fmt="svg", mime="image/svg+xml", meta=artifact_meta)
        if svg_art:
            artifacts.append(svg_art)
        if rendered.get("png"):
            png_art = await _store_artifact(ctx, rendered["png"], title=f"{title} PNG", fmt="png", mime="image/png", meta=artifact_meta)
            if png_art:
                artifacts.append(png_art)
        input_art = await _store_artifact(
            ctx,
            json.dumps(
                {
                    "schema": "omni.figure_input/v1",
                    "prompt": prompt,
                    "title": title,
                    "figure_kind": figure_kind,
                    "requested_figure_kind": requested_figure_kind,
                    "effective_inputs": effective_inputs,
                    "revision_mode": revision.get("mode", ""),
                    "source_task_id": revision.get("source_task_id", ""),
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
            title=f"{title} Input",
            fmt="json",
            mime="application/json",
            kind="data",
            meta=artifact_meta,
        )
        if input_art:
            artifacts.append(input_art)

        await _progress(progress_callback, "record provenance", 0.75)
        research = await _record_provenance(ctx, figure_kind, title, artifacts, rendered.get("cmd", ""))
        figure_bundle = await _build_figure_bundle(
            ctx,
            title=title,
            artifacts=artifacts,
            run_id=str(research.get("run_id") or ""),
        )
        # Emit the self-contained provenance record last so it can reference the
        # rendered files, the research run, and the verified bundle manifest. It
        # lands next to the figure (``figures/<name>.provenance.json``) when the
        # launch directory is trusted, completing the portable bundle.
        provenance_art = await _write_provenance(
            ctx,
            title=title,
            figure_kind=figure_kind,
            requested_figure_kind=requested_figure_kind,
            effective_inputs=effective_inputs,
            prompt=prompt,
            command=rendered.get("cmd", ""),
            artifacts=artifacts,
            research=research,
            figure_bundle=figure_bundle,
            meta=artifact_meta,
        )
        if provenance_art:
            artifacts.append(provenance_art)

        await _progress(progress_callback, "done", 1.0)
        formats = ", ".join(
            a["format"].upper() for a in artifacts if a.get("format") in {"dot", "svg", "png"}
        )
        gate_code = str(kind_gate.get("code") or "")
        status = "partial" if gate_code == "generic_despite_domain_terms" else "ok"
        warning = ""
        if gate_code == "generic_despite_domain_terms":
            warning = (
                "The instruction names domain components that the generic template does "
                "not render; the figure is a degraded placeholder. Provide figure_kind "
                "(rag/transformer) or a more specific instruction for a full diagram."
            )
        assessment = _figure_assessment(
            ctx,
            input_data,
            effective_inputs=effective_inputs,
            kind_gate=kind_gate,
            status="degraded" if warning else "passed",
            summary=warning or f"Effective figure template `{figure_kind}` matched the provider check.",
            evidence_refs=[
                str(artifact.get("uri") or "")
                for artifact in artifacts
                if artifact.get("uri")
            ],
        )
        return {
            "status": status,
            **({"outcome": kind_gate} if gate_code else {}),
            **({"warning": warning, "recoverable": True} if warning else {}),
            "title": title,
            "figure_kind": figure_kind,
            "requested_figure_kind": requested_figure_kind,
            "effective_inputs": effective_inputs,
            "deliverable_assessment": assessment,
            "summary": f"Generated an auditable, reproducible {title} ({formats}).",
            "caption": _caption(figure_kind),
            "artifacts": artifacts,
            "dot_uri": _first_uri(artifacts, "dot"),
            "svg_uri": _first_uri(artifacts, "svg"),
            "png_uri": _first_uri(artifacts, "png"),
            "run_id": research.get("run_id", ""),
            "research": research,
            "figure_bundle": figure_bundle,
            **({"revision": revision} if revision else {}),
            "next_actions": [
                "Use /task show <id> to inspect DOT/SVG/PNG artifacts and the audit trace.",
                "Use /task attach <id> to continue revising the figure in the current session.",
                "Use /verify --session to audit claims and supporting evidence.",
            ],
        }


async def _progress(callback: Any, stage: str, pct: float = 0.0, **data: Any) -> None:
    if callback is None:
        return
    try:
        result = callback(stage, pct, **data)
    except TypeError:
        result = callback(stage, pct)
    if hasattr(result, "__await__"):
        await result


def _figure_kind(input_data: dict[str, Any]) -> str:
    value = str(input_data.get("figure_kind") or input_data.get("template") or "generic").strip().lower()
    return value if value in {"rag", "transformer", "generic"} else "generic"


# Template signatures: the component vocabulary each built-in template can
# actually render. This is *skill content* (mirrored in SKILL.md metadata), not
# runtime routing — it never selects a skill, it only keeps this skill from
# rendering a meaningless generic diagram when its own instruction names
# components that one of its templates covers.
_TEMPLATE_SIGNATURES: dict[str, tuple[str, ...]] = {
    # English-only by repo contract (user language belongs to model I/O, not
    # runtime assets); component names are near-universally written in Latin
    # script even inside non-English instructions.
    "rag": (
        "rag", "query", "retriev", "rerank", "vector", "embedding", "llm",
        "grounded", "citation",
    ),
    "transformer": (
        "transformer", "encoder", "decoder", "attention", "multi-head",
        "self-attention", "cross-attention",
    ),
}


def _signature_hits(text: str) -> dict[str, int]:
    lowered = (text or "").lower()
    return {
        kind: sum(1 for token in tokens if token in lowered)
        for kind, tokens in _TEMPLATE_SIGNATURES.items()
    }


def _resolve_creation_kind(
    input_data: dict[str, Any],
    *,
    prompt: str,
    title: str,
    source_text: str = "",
) -> tuple[str, dict[str, Any]]:
    """Provider-owned quality gate for the effective figure template.

    A planner may mis-set ``figure_kind`` under multi-deliverable load even when
    the instruction plainly names components a built-in template renders.

    - a clear signature wins over both ``generic`` and a contradictory declared
      template;
    - domain-flavoured but ambiguous instructions keep ``generic`` and are
      honestly reported as degraded instead of shipping four empty boxes as a
      finished figure.
    """
    declared = _figure_kind(input_data)
    hits = _signature_hits(f"{title}\n{prompt}\n{source_text}")
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    best_kind, best_count = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best_count >= 2 and best_count > runner_up:
        if best_kind == declared:
            return declared, {}
        return best_kind, {
            "code": (
                "template_upgraded"
                if declared == "generic"
                else "template_corrected"
            ),
            "requested_kind": declared,
            "figure_kind": best_kind,
            "signature_hits": dict(hits),
        }
    if declared == "generic" and sum(hits.values()) >= 2:
        return "generic", {
            "code": "generic_despite_domain_terms",
            "requested_kind": "generic",
            "figure_kind": "generic",
            "signature_hits": dict(hits),
        }
    return declared, {}


def _effective_inputs(
    input_data: dict[str, Any],
    *,
    prompt: str,
    title: str,
    requested_figure_kind: str,
    figure_kind: str,
    revision: bool,
) -> dict[str, Any]:
    """Auditable inputs actually used by the provider, excluding local paths."""

    return {
        "input": prompt,
        "title": title,
        "requested_figure_kind": requested_figure_kind,
        "figure_kind": figure_kind,
        "revision_mode": "major" if revision else "",
        "source_task_id": str(input_data.get("source_task_id") or ""),
        "source_artifact_uri": str(input_data.get("source_artifact_uri") or ""),
    }


def _figure_assessment(
    ctx: Any,
    input_data: dict[str, Any],
    *,
    effective_inputs: dict[str, Any],
    kind_gate: dict[str, Any],
    status: str,
    summary: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Return the portable provider-owned deliverable assessment envelope."""

    deliverable_id = str(
        input_data.get("deliverable_id")
        or input_data.get("deliverable")
        or "artifact"
    )
    authority = getattr(ctx, "provider_authority", None)
    authority_fingerprint = (
        str(authority.get("fingerprint") or "")
        if isinstance(authority, dict)
        else ""
    )
    contract_hash = authority_fingerprint or hashlib.sha256(
        b"scientific-figure:quality-contract:v1"
    ).hexdigest()
    step_id = str(
        getattr(ctx, "workflow_step_key", "")
        or input_data.get("workflow_step_id")
        or deliverable_id
    )
    return {
        "schema": "omni.deliverable-assessment/v1",
        "deliverable_id": deliverable_id,
        "provider_binding_id": str(
            input_data.get("provider_binding_id")
            or f"skill:scientific-figure:{deliverable_id}"
        ),
        "provider": "scientific-figure",
        "provider_authority_fingerprint": authority_fingerprint,
        "contract_hash": contract_hash,
        "step_id": step_id,
        "feedback": summary,
        "status": status,
        # Template selection is deterministic for unchanged inputs. Replaying
        # it cannot improve quality; a repair must first change the instruction
        # or selected template.
        "retryable": False,
        "effective_inputs": dict(effective_inputs),
        "criteria": [
            {
                "criterion_id": "figure_matches_instruction",
                "status": status,
                "summary": summary,
                "evidence_refs": list(evidence_refs or []),
                **({"details": dict(kind_gate)} if kind_gate else {}),
            }
        ],
        "evidence_refs": [
            str(item)
            for item in evidence_refs or []
            if str(item)
        ],
        "summary": summary,
    }


def _figure_dot(kind: str, title: str) -> str:
    if kind == "rag":
        return _rag_dot(title)
    if kind == "transformer":
        return _transformer_dot(title)
    return _generic_dot(title)


def _source_dot(input_data: dict[str, Any]) -> str:
    inline = str(input_data.get("source_artifact_dot") or "")
    if inline.strip():
        return inline
    path = str(input_data.get("source_artifact_path") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""


def _dot_title(dot: str) -> str:
    match = re.search(r"graph\s*\[[^\]]*label\s*=\s*\"([^\"]+)\"", dot or "", flags=re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r'label\s*=\s*"([^"]+)"', dot or "")
    return match.group(1) if match else ""


def _major_revision_dot(source_dot: str, *, prompt: str, title: str, kind: str) -> str:
    """Preserve the source DOT and add domain-appropriate engineering detail."""
    base = _retitle_dot(source_dot, title)
    if kind == "rag":
        addition = _rag_engineering_addition()
    elif kind == "transformer":
        addition = _transformer_engineering_addition()
    else:
        addition = _generic_engineering_addition(prompt)
    return _append_dot_body(base, addition)


def _retitle_dot(dot: str, title: str) -> str:
    if not title:
        return dot
    return re.sub(
        r"(graph\s*\[[^\]]*label\s*=\s*)\"[^\"]+\"",
        rf'\1"{title}"',
        dot,
        count=1,
        flags=re.DOTALL,
    )


def _append_dot_body(dot: str, addition: str) -> str:
    stripped = dot.rstrip()
    if stripped.endswith("}"):
        return stripped[:-1].rstrip() + "\n\n" + addition.rstrip() + "\n}\n"
    return stripped + "\n\n" + addition.rstrip() + "\n"


def _rag_engineering_addition() -> str:
    return """  subgraph cluster_engineering {
    label="Engineering Enhancements";
    color="#0f766e";
    bgcolor="#ecfeff";
    node [shape=box, style="rounded,filled", fillcolor="#ccfbf1", color="#0d9488"];
    ingestion [label="Data Ingestion\\nPDF / HTML / DB / API"];
    chunking [label="Parsing + Chunking\\nsemantic / sliding window"];
    hybrid [label="Hybrid Retrieval\\nDense + BM25 + filters"];
    cache [label="Query Cache\\n+ embedding cache"];
    evals [label="Eval Harness\\nfaithfulness / recall / latency"];
    observability [label="Observability\\ntrace, cost, drift"];
    feedback [label="Feedback Loop\\nlogs -> corpus updates"];
  }
  ingestion -> chunking -> hybrid;
  cache -> hybrid [style=dashed, label="reuse"];
  hybrid -> evals [style=dashed, label="quality gate"];
  evals -> observability -> feedback;
  feedback -> ingestion [style=dashed, label="continuous improvement"];
  retriever -> hybrid [style=dashed, label="upgrade path"];
  reranker -> evals [style=dashed, label="rank quality"];
  answer -> feedback [style=dashed, label="user feedback"];"""


def _transformer_engineering_addition() -> str:
    return """  subgraph cluster_engineering {
    label="Implementation Notes";
    color="#0f766e";
    bgcolor="#ecfeff";
    node [shape=box, style="rounded,filled", fillcolor="#ccfbf1", color="#0d9488"];
    tokens [label="Tokenizer + Batching"];
    masks [label="Attention Masks\\ncausal / padding"];
    kv_cache [label="KV Cache\\nfor decoding"];
    training [label="Training Loop\\nloss + optimizer"];
    evals [label="Evaluation\\nperplexity / BLEU"];
  }
  tokens -> masks -> training -> evals;
  dec_mask -> kv_cache [style=dashed, label="inference"];
  dec_out -> evals [style=dashed, label="metrics"];"""


def _generic_engineering_addition(prompt: str) -> str:
    del prompt
    label = "Revision Detail"
    return f"""  subgraph cluster_revision {{
    label="{label}";
    color="#0f766e";
    bgcolor="#ecfeff";
    node [shape=box, style="rounded,filled", fillcolor="#ccfbf1", color="#0d9488"];
    requirements [label="Requirements"];
    implementation [label="Implementation"];
    validation_gate [label="Validation Gate"];
    observability [label="Observability"];
    requirements -> implementation -> validation_gate -> observability;
  }}"""


def _quality_gate(source_dot: str, revised_dot: str, *, constraints: dict[str, Any]) -> dict[str, Any]:
    allow_simplification = bool(constraints.get("allow_simplification"))
    source_nodes = _node_count(source_dot)
    revised_nodes = _node_count(revised_dot)
    source_edges = _edge_count(source_dot)
    revised_edges = _edge_count(revised_dot)
    min_nodes = int(constraints.get("min_nodes") or (0 if allow_simplification else source_nodes))
    min_edges = int(constraints.get("min_edges") or (0 if allow_simplification else source_edges))
    generic_rejected = bool(constraints.get("reject_generic_template")) and "input -> method -> validation -> output" in revised_dot
    passed = (
        (allow_simplification or revised_nodes >= min_nodes)
        and (allow_simplification or revised_edges >= min_edges)
        and not generic_rejected
    )
    reason = ""
    if revised_nodes < min_nodes:
        reason = f"revised node count {revised_nodes} is below required {min_nodes}"
    elif revised_edges < min_edges:
        reason = f"revised edge count {revised_edges} is below required {min_edges}"
    elif generic_rejected:
        reason = "revision fell back to generic template"
    return {
        "passed": passed,
        "reason": reason,
        "source_nodes": source_nodes,
        "revised_nodes": revised_nodes,
        "source_edges": source_edges,
        "revised_edges": revised_edges,
    }


def _revision_artifact_meta(revision: dict[str, Any]) -> dict[str, Any]:
    if not revision:
        return {}
    return {
        "revision_mode": revision.get("mode", ""),
        "revision_of": revision.get("source_artifact_path", ""),
        "source_task_id": revision.get("source_task_id", ""),
        "quality": revision.get("quality", {}),
    }


def _node_count(dot: str) -> int:
    labels = re.findall(r'label\s*=\s*"[^"]+"', dot or "")
    return len(labels)


def _edge_count(dot: str) -> int:
    return len(re.findall(r"->", dot or ""))


def _transformer_dot(title: str) -> str:
    return f"""digraph Transformer {{
  graph [rankdir=LR, bgcolor="white", fontname="Helvetica", labelloc=t, label="{title}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica", fontsize=14];
  edge [color="#475569", fontname="Helvetica", fontsize=11];

  subgraph cluster_encoder {{
    label="Encoder stack (N x)";
    color="#0f766e";
    enc_in [label="Input embeddings\\n+ positional encoding"];
    enc_attn [label="Multi-head\\nself-attention"];
    enc_norm1 [label="Add & LayerNorm"];
    enc_ffn [label="Position-wise\\nfeed-forward"];
    enc_norm2 [label="Add & LayerNorm"];
    enc_in -> enc_attn -> enc_norm1 -> enc_ffn -> enc_norm2;
  }}

  subgraph cluster_decoder {{
    label="Decoder stack (N x)";
    color="#7c3aed";
    dec_in [label="Shifted output embeddings\\n+ positional encoding"];
    dec_mask [label="Masked multi-head\\nself-attention"];
    dec_norm1 [label="Add & LayerNorm"];
    dec_cross [label="Encoder-decoder\\ncross-attention"];
    dec_norm2 [label="Add & LayerNorm"];
    dec_ffn [label="Position-wise\\nfeed-forward"];
    dec_norm3 [label="Add & LayerNorm"];
    dec_out [label="Linear + Softmax\\noutput probabilities"];
    dec_in -> dec_mask -> dec_norm1 -> dec_cross -> dec_norm2 -> dec_ffn -> dec_norm3 -> dec_out;
  }}

  enc_norm2 -> dec_cross [label="encoder output"];
}}"""


def _rag_dot(title: str) -> str:
    return f"""digraph RAG {{
  graph [rankdir=LR, bgcolor="white", fontname="Helvetica", labelloc=t, label="{title}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica", fontsize=14];
  edge [color="#475569", fontname="Helvetica", fontsize=11];

  user [label="User Query"];
  planner [label="Query Rewrite\\n+ Intent"];
  embed [label="Embedding Model"];
  index [label="Document Chunks\\n+ Metadata"];
  vectordb [label="Vector DB"];
  retriever [label="Retriever\\nTop-k Candidates"];
  reranker [label="Reranker\\n+ Context Compression"];
  context [label="Prompt Assembly\\nGrounded Context"];
  llm [label="LLM Generator"];
  verify [label="Citation Verification\\n+ Guardrails"];
  answer [label="Grounded Answer\\nwith Sources"];

  user -> planner -> embed -> vectordb -> retriever -> reranker -> context -> llm -> verify -> answer;
  index -> vectordb [label="offline indexing"];
  user -> context [style=dashed, label="task instruction"];
  context -> verify [style=dashed, label="source spans"];
}}"""


def _generic_dot(title: str) -> str:
    return f"""digraph ScientificFigure {{
  graph [rankdir=LR, bgcolor="white", fontname="Helvetica", labelloc=t, label="{title}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica", fontsize=14];
  edge [color="#475569", fontname="Helvetica", fontsize=11];
  input [label="Inputs"];
  method [label="Method"];
  validation [label="Validation"];
  output [label="Outputs"];
  input -> method -> validation -> output;
}}"""


async def _render_graphviz(dot: str) -> dict[str, Any]:
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return {"cmd": "dot not found; used SVG fallback"}
    out: dict[str, Any] = {"cmd": f"{dot_bin} -Tsvg/-Tpng"}
    for fmt in ("svg", "png"):
        proc = await asyncio.create_subprocess_exec(
            dot_bin,
            f"-T{fmt}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(dot.encode("utf-8"))
        if proc.returncode == 0 and stdout:
            out[fmt] = stdout
        else:
            out[f"{fmt}_error"] = stderr.decode("utf-8", "replace")[:500]
    return out


async def _store_artifact(
    ctx: Any,
    data: bytes,
    *,
    title: str,
    fmt: str,
    mime: str,
    kind: str = "figure",
    meta: dict[str, Any] | None = None,
    record_format: str | None = None,
) -> dict[str, str] | None:
    if ctx is None or getattr(ctx, "artifacts", None) is None:
        return None
    stored = await ctx.artifacts.put_bytes(
        data,
        kind=kind,
        title=title,
        ext=fmt,
        mime=mime,
        session_id=getattr(ctx, "session_id", ""),
        task_id=getattr(ctx, "task_id", ""),
        subtask_id=getattr(ctx, "subtask_id", ""),
        workflow_run_id=getattr(ctx, "workflow_run_id", ""),
        meta=meta,
    )
    # Shared record shape + user-facing path (launch-dir copy when mirrored).
    # ``record_format`` lets a compound on-disk extension (``provenance.json``)
    # keep a clean result label (``json``) without polluting format lookups.
    return stored.result_record(title=title, format=record_format or fmt)


async def _write_provenance(
    ctx: Any,
    *,
    title: str,
    figure_kind: str,
    requested_figure_kind: str,
    effective_inputs: dict[str, Any],
    prompt: str,
    command: str,
    artifacts: list[dict[str, str]],
    research: dict[str, Any],
    figure_bundle: dict[str, Any],
    meta: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Write a self-contained ``<figure>.provenance.json`` beside the figure.

    Co-located with the rendered image and its ``.dot`` source, this record
    turns ``figures/`` into a portable, auditable bundle: it pins the exact
    inputs, the managed artifact URIs, the research run + its claims/evidence/
    sources, the figure-bundle manifest and its hash verification, and the
    captured environment lock — everything needed to reproduce or audit the
    figure without the durable store.
    """
    if ctx is None or getattr(ctx, "artifacts", None) is None:
        return None
    try:
        from omni.research.tools import capture_env_lock

        env_lock = capture_env_lock()
    except Exception:  # noqa: BLE001
        env_lock = {}
    document = {
        "schema": "omni.figure_provenance/v1",
        "created_at": datetime.now(UTC).isoformat(),
        "title": title,
        "figure_kind": figure_kind,
        "requested_figure_kind": requested_figure_kind,
        "effective_inputs": dict(effective_inputs),
        "prompt": prompt,
        "command": command or "graphviz dot render",
        "files": [
            {
                "name": Path(str(a.get("path") or "")).name,
                "format": a.get("format", ""),
                "uri": a.get("uri", ""),
                "mime": a.get("mime", ""),
                "size_bytes": a.get("size_bytes", ""),
            }
            for a in artifacts
            if a.get("uri")
        ],
        "research": {
            "run_id": research.get("run_id", ""),
            "source_ids": list(research.get("source_ids", []) or []),
            "claim_ids": list(research.get("claim_ids", []) or []),
            "evidence_ids": list(research.get("evidence_ids", []) or []),
        },
        "figure_bundle": {
            "status": figure_bundle.get("status", ""),
            "manifest_uri": figure_bundle.get("manifest_uri", ""),
            "run_ids": list(figure_bundle.get("run_ids", []) or []),
            "verification": figure_bundle.get("verification", {}),
        },
        "environment": env_lock,
    }
    payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    return await _store_artifact(
        ctx,
        payload,
        title=title,
        fmt="provenance.json",
        record_format="json",
        mime="application/json",
        kind="figure",
        meta=meta,
    )


async def _record_transformer_provenance(ctx: Any, title: str, artifacts: list[dict[str, str]], cmd: str) -> dict[str, Any]:
    if ctx is None or getattr(ctx, "db", None) is None:
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": ""}
    try:
        from omni.research import add_papers_to_library
        from omni.research.store import ResearchStore
        from omni.research.tools import capture_env_lock

        store = ResearchStore(ctx.db)
        source = await store.add_source(
            {
                "title": "Attention Is All You Need",
                "arxiv_id": "1706.03762",
                "url": "https://arxiv.org/abs/1706.03762",
                "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"],
                "year": "2017",
                "kind": "paper",
                "summary": "Introduces the Transformer architecture based on attention mechanisms.",
            },
            origin="scientific-figure",
        )
        try:
            add_papers_to_library(ctx.paths.library, [{
                "title": source.title,
                "arxiv_id": source.arxiv_id,
                "url": source.url,
                "authors": source.authors,
                "year": source.year,
                "summary": source.summary,
            }])
        except Exception:  # noqa: BLE001
            pass

        claim_texts = [
            "The Transformer organizes sequence-to-sequence modeling with stacked encoders and decoders.",
            "Each encoder layer contains multi-head self-attention, residual connections, layer normalization, and a feed-forward network.",
            "Each decoder layer uses masked multi-head self-attention to prevent access to future tokens.",
            "The decoder reads encoder outputs through encoder-decoder cross-attention.",
            "A linear projection and softmax produce output-token probabilities.",
        ]
        claim_ids: list[str] = []
        evidence_ids: list[str] = []
        for text in claim_texts:
            claim = await store.add_claim(text, session_id=getattr(ctx, "session_id", ""), confidence=0.86)
            ev = await store.add_evidence(
                claim.id,
                source_id=source.id,
                stance="supports",
                quote="Architecture summarized from Vaswani et al. (2017), Figure 1 and model description.",
                locator="arXiv:1706.03762",
                strength=0.82,
            )
            claim_ids.append(claim.id)
            evidence_ids.append(ev.id)

        output_uris = [a["uri"] for a in artifacts if a.get("uri")]
        run = await store.add_run(
            title=f"Render {title}",
            session_id=getattr(ctx, "session_id", ""),
            subtask_id=getattr(ctx, "subtask_id", ""),
            cmd=cmd or "graphviz dot render",
            code_uri=next((a["uri"] for a in artifacts if a.get("format") == "dot"), ""),
            env_lock=capture_env_lock(),
            output_uris=output_uris,
            metrics={"artifact_count": len(output_uris)},
            status="succeeded",
        )
        return {
            "source_ids": [source.id],
            "claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "run_id": run.id,
        }
    except Exception as exc:  # noqa: BLE001
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": "", "error": str(exc)}


async def _record_rag_provenance(ctx: Any, title: str, artifacts: list[dict[str, str]], cmd: str) -> dict[str, Any]:
    if ctx is None or getattr(ctx, "db", None) is None:
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": ""}
    try:
        from omni.research import add_papers_to_library
        from omni.research.store import ResearchStore
        from omni.research.tools import capture_env_lock

        store = ResearchStore(ctx.db)
        source = await store.add_source(
            {
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
                "arxiv_id": "2005.11401",
                "url": "https://arxiv.org/abs/2005.11401",
                "authors": ["Patrick Lewis", "Ethan Perez", "Aleksandra Piktus", "Fabio Petroni"],
                "year": "2020",
                "kind": "paper",
                "summary": "Introduces RAG models that combine parametric generation with retrieved non-parametric memory.",
            },
            origin="scientific-figure",
        )
        try:
            add_papers_to_library(ctx.paths.library, [{
                "title": source.title,
                "arxiv_id": source.arxiv_id,
                "url": source.url,
                "authors": source.authors,
                "year": source.year,
                "summary": source.summary,
            }])
        except Exception:  # noqa: BLE001
            pass

        claim_texts = [
            "RAG routes a user query through retrieval before providing relevant context to a generator.",
            "Vector indexes and metadata filters retrieve candidate evidence from external knowledge stores.",
            "Reranking or context compression improves the relevance of evidence entering the prompt.",
            "Prompt assembly combines instructions, retrieved context, and citation metadata for generation.",
            "Citation verification and guardrails reduce unsupported claims and expose evidence gaps.",
        ]
        claim_ids: list[str] = []
        evidence_ids: list[str] = []
        for text in claim_texts:
            claim = await store.add_claim(text, session_id=getattr(ctx, "session_id", ""), confidence=0.84)
            ev = await store.add_evidence(
                claim.id,
                source_id=source.id,
                stance="supports",
                quote="RAG combines retrieved non-parametric memory with sequence generation for knowledge-intensive tasks.",
                locator="arXiv:2005.11401",
                strength=0.8,
            )
            claim_ids.append(claim.id)
            evidence_ids.append(ev.id)

        output_uris = [a["uri"] for a in artifacts if a.get("uri")]
        run = await store.add_run(
            title=f"Render {title}",
            session_id=getattr(ctx, "session_id", ""),
            subtask_id=getattr(ctx, "subtask_id", ""),
            cmd=cmd or "graphviz dot render",
            code_uri=next((a["uri"] for a in artifacts if a.get("format") == "dot"), ""),
            env_lock=capture_env_lock(),
            output_uris=output_uris,
            metrics={"artifact_count": len(output_uris)},
            status="succeeded",
        )
        return {
            "source_ids": [source.id],
            "claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "run_id": run.id,
        }
    except Exception as exc:  # noqa: BLE001
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": "", "error": str(exc)}


async def _record_provenance(ctx: Any, kind: str, title: str, artifacts: list[dict[str, str]], cmd: str) -> dict[str, Any]:
    if kind == "rag":
        return await _record_rag_provenance(ctx, title, artifacts, cmd)
    if kind == "transformer":
        return await _record_transformer_provenance(ctx, title, artifacts, cmd)
    return await _record_generic_provenance(ctx, title, artifacts, cmd)


async def _record_generic_provenance(
    ctx: Any,
    title: str,
    artifacts: list[dict[str, str]],
    cmd: str,
) -> dict[str, Any]:
    if ctx is None or getattr(ctx, "db", None) is None:
        return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": ""}
    from omni.research.store import ResearchStore
    from omni.research.tools import capture_env_lock

    output_uris = [artifact["uri"] for artifact in artifacts if artifact.get("uri")]
    run = await ResearchStore(ctx.db).add_run(
        title=f"Render {title}",
        session_id=getattr(ctx, "session_id", ""),
        subtask_id=getattr(ctx, "subtask_id", ""),
        cmd=cmd or "graphviz dot render",
        code_uri=next(
            (artifact["uri"] for artifact in artifacts if artifact.get("format") == "dot"),
            "",
        ),
        env_lock=capture_env_lock(),
        output_uris=output_uris,
        metrics={"artifact_count": len(output_uris)},
        status="succeeded",
    )
    return {"source_ids": [], "claim_ids": [], "evidence_ids": [], "run_id": run.id}


async def _build_figure_bundle(
    ctx: Any,
    *,
    title: str,
    artifacts: list[dict[str, str]],
    run_id: str,
) -> dict[str, Any]:
    if ctx is None or getattr(ctx, "artifacts", None) is None or not run_id:
        return {"status": "unavailable", "reason": "managed artifacts or research run unavailable"}
    figure_uri = _first_uri(artifacts, "png") or _first_uri(artifacts, "svg")
    code_uri = _first_uri(artifacts, "dot")
    data_uri = _first_uri(artifacts, "json")
    if not figure_uri or not code_uri or not data_uri:
        return {"status": "unavailable", "reason": "figure/code/data artifact missing"}
    from omni.research.artifacts import build_figure_bundle, verify_figure_bundle

    bundle = await build_figure_bundle(
        artifacts=ctx.artifacts,
        figure_uri=figure_uri,
        code_uri=code_uri,
        data_uris=[data_uri],
        run_ids=[run_id],
        session_id=getattr(ctx, "session_id", ""),
        task_id=getattr(ctx, "task_id", ""),
        subtask_id=getattr(ctx, "subtask_id", ""),
        title=f"{title} Figure Bundle",
    )
    if bundle.get("status") != "ok":
        return bundle
    verification = await verify_figure_bundle(
        artifacts=ctx.artifacts,
        manifest=dict(bundle.get("manifest") or {}),
    )
    return {
        "status": "passed" if verification.get("passed") else "failed",
        "manifest_uri": bundle.get("manifest_uri", ""),
        "figure_uri": figure_uri,
        "code_uri": code_uri,
        "data_uris": [data_uri],
        "run_ids": [run_id],
        "verification": verification,
    }


def _first_uri(artifacts: list[dict[str, str]], fmt: str) -> str:
    return next((a.get("uri", "") for a in artifacts if a.get("format") == fmt), "")


def _caption(kind: str) -> str:
    if kind == "rag":
        return (
            "RAG combines query rewriting, vector retrieval, reranking or context compression, "
            "prompt assembly, generation, and citation guardrails to ground answers in external evidence."
        )
    if kind == "transformer":
        return (
            "The Transformer uses stacked encoders and decoders with multi-head attention, residual "
            "connections, layer normalization, feed-forward networks, masked attention, and cross-attention."
        )
    return "The figure shows the main relationships among inputs, methods, validation, and outputs."


def _fallback_svg(kind: str, title: str) -> str:
    if kind == "rag":
        return _rag_fallback_svg(title)
    if kind == "generic":
        return _generic_fallback_svg(title)
    return _transformer_fallback_svg(title)


def _transformer_fallback_svg(title: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="520" viewBox="0 0 960 520">
  <rect width="100%" height="100%" fill="white"/>
  <text x="480" y="36" text-anchor="middle" font-family="Helvetica" font-size="24">{title}</text>
  <rect x="80" y="90" width="280" height="340" rx="10" fill="#f8fafc" stroke="#0f766e" stroke-width="3"/>
  <text x="220" y="125" text-anchor="middle" font-family="Helvetica" font-size="18">Encoder stack (N x)</text>
  <text x="120" y="180" font-family="Helvetica" font-size="16">Input Embedding + Pos Encoding</text>
  <text x="120" y="235" font-family="Helvetica" font-size="16">Multi-head Self-Attention</text>
  <text x="120" y="290" font-family="Helvetica" font-size="16">Add &amp; LayerNorm</text>
  <text x="120" y="345" font-family="Helvetica" font-size="16">Feed Forward + Add &amp; Norm</text>
  <rect x="600" y="90" width="280" height="340" rx="10" fill="#faf5ff" stroke="#7c3aed" stroke-width="3"/>
  <text x="740" y="125" text-anchor="middle" font-family="Helvetica" font-size="18">Decoder stack (N x)</text>
  <text x="640" y="175" font-family="Helvetica" font-size="16">Shifted Output Embedding</text>
  <text x="640" y="225" font-family="Helvetica" font-size="16">Masked Self-Attention</text>
  <text x="640" y="275" font-family="Helvetica" font-size="16">Cross-Attention</text>
  <text x="640" y="325" font-family="Helvetica" font-size="16">Feed Forward + Add &amp; Norm</text>
  <text x="640" y="375" font-family="Helvetica" font-size="16">Linear + Softmax</text>
  <line x1="360" y1="275" x2="600" y2="275" stroke="#475569" stroke-width="3" marker-end="url(#arrow)"/>
  <defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="5" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="#475569"/></marker></defs>
</svg>"""


def _rag_fallback_svg(title: str) -> str:
    labels = [
        "User Query",
        "Vector DB",
        "Retriever",
        "Reranker",
        "Grounded Context",
        "LLM",
        "Citation Verification",
        "Answer",
    ]
    width = 160 * len(labels) + 80
    boxes = []
    arrows = []
    for index, label in enumerate(labels):
        x = 40 + index * 160
        boxes.append(
            f'<rect x="{x}" y="130" width="130" height="58" rx="8" fill="#f8fafc" stroke="#334155"/>'
            f'<text x="{x + 65}" y="155" text-anchor="middle" font-family="Helvetica" font-size="13">{label}</text>'
        )
        if index < len(labels) - 1:
            arrows.append(
                f'<line x1="{x + 130}" y1="159" x2="{x + 156}" y2="159" '
                'stroke="#475569" stroke-width="2" marker-end="url(#arrow)"/>'
            )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="320" viewBox="0 0 {width} 320">
  <defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="5" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="#475569"/></marker></defs>
  <rect width="100%" height="100%" fill="white"/>
  <text x="{width / 2:.0f}" y="54" text-anchor="middle" font-family="Helvetica" font-size="24">{title}</text>
  {''.join(boxes)}
  {''.join(arrows)}
</svg>"""


def _generic_fallback_svg(title: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="760" height="260" viewBox="0 0 760 260">
  <defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="5" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="#475569"/></marker></defs>
  <rect width="100%" height="100%" fill="white"/>
  <text x="380" y="48" text-anchor="middle" font-family="Helvetica" font-size="22">{title}</text>
  <rect x="60" y="120" width="120" height="58" rx="8" fill="#f8fafc" stroke="#334155"/><text x="120" y="154" text-anchor="middle" font-family="Helvetica" font-size="14">Inputs</text>
  <rect x="240" y="120" width="120" height="58" rx="8" fill="#f8fafc" stroke="#334155"/><text x="300" y="154" text-anchor="middle" font-family="Helvetica" font-size="14">Method</text>
  <rect x="420" y="120" width="120" height="58" rx="8" fill="#f8fafc" stroke="#334155"/><text x="480" y="154" text-anchor="middle" font-family="Helvetica" font-size="14">Validation</text>
  <rect x="600" y="120" width="120" height="58" rx="8" fill="#f8fafc" stroke="#334155"/><text x="660" y="154" text-anchor="middle" font-family="Helvetica" font-size="14">Outputs</text>
  <line x1="180" y1="149" x2="238" y2="149" stroke="#475569" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="360" y1="149" x2="418" y2="149" stroke="#475569" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="540" y1="149" x2="598" y2="149" stroke="#475569" stroke-width="2" marker-end="url(#arrow)"/>
</svg>"""
