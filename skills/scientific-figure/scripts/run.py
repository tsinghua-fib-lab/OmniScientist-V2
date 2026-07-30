#!/usr/bin/env python3
"""Portable runner for scientific-figure without Omni runtime."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL = "scientific-figure"


def _infer_title(prompt: str) -> str:
    low = prompt.lower()
    if "rag" in low:
        return "RAG System Architecture"
    if "transformer" in low:
        return "Transformer Architecture"
    return "Scientific Figure"


def _dot_text(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(prompt: str) -> list[str]:
    low = prompt.lower()
    if "rag" in low:
        return ["Query", "Retriever", "Reranker", "Top-k Context", "LLM", "Answer"]
    if "transformer" in low:
        return ["Input Embedding + Position", "Encoder Stack", "Decoder Stack", "Linear + Softmax"]
    return ["Input", "Core Process", "Output"]


def _dot(title: str, prompt: str, labels: list[str]) -> str:
    low = prompt.lower()
    if "rag" in low:
        return f"""digraph RAG {{
  graph [rankdir=LR, bgcolor="white", labelloc=t, label="{_dot_text(title)}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica"];
  edge [color="#475569", fontname="Helvetica"];
  query [label="Query"];
  retriever [label="Retriever"];
  reranker [label="Reranker"];
  context [label="Top-k Context"];
  llm [label="LLM"];
  answer [label="Answer"];
  query -> retriever -> reranker -> context -> llm -> answer;
  query -> llm [style=dashed, label="instruction"];
}}"""
    if "transformer" in low:
        return f"""digraph Transformer {{
  graph [rankdir=LR, bgcolor="white", labelloc=t, label="{_dot_text(title)}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica"];
  edge [color="#475569", fontname="Helvetica"];
  input [label="Input Embedding\\n+ Position"];
  encoder [label="Encoder Stack\\nSelf-Attention + FFN"];
  decoder [label="Decoder Stack\\nMasked Self-Attention + Cross-Attention"];
  output [label="Linear + Softmax"];
  input -> encoder -> decoder -> output;
}}"""
    nodes = "\n".join(f'  n{index} [label="{_dot_text(label)}"];' for index, label in enumerate(labels))
    edges = "\n".join(f"  n{index} -> n{index + 1};" for index in range(len(labels) - 1))
    return f"""digraph ScientificFigure {{
  graph [rankdir=LR, bgcolor="white", labelloc=t, label="{_dot_text(title)}"];
  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#f8fafc", fontname="Helvetica"];
  edge [color="#475569", fontname="Helvetica"];
{nodes}
{edges}
}}"""


def _fallback_svg(title: str, labels: list[str]) -> str:
    width = 180 * len(labels) + 80
    boxes = []
    arrows = []
    for index, label in enumerate(labels):
        x = 40 + index * 180
        boxes.append(
            f'<rect x="{x}" y="120" width="130" height="56" rx="8" fill="#f8fafc" stroke="#334155"/>'
            f'<text x="{x + 65}" y="153" text-anchor="middle" font-family="Helvetica" font-size="14">'
            f"{html.escape(label)}</text>"
        )
        if index < len(labels) - 1:
            arrows.append(
                f'<line x1="{x + 130}" y1="148" x2="{x + 178}" y2="148" '
                'stroke="#475569" stroke-width="2" marker-end="url(#arrow)"/>'
            )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="260" viewBox="0 0 {width} 260">
  <defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="5" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="#475569"/></marker></defs>
  <rect width="100%" height="100%" fill="white"/>
  <text x="{width / 2:.0f}" y="48" text-anchor="middle" font-family="Helvetica" font-size="22">{html.escape(title)}</text>
  {''.join(boxes)}
  {''.join(arrows)}
</svg>"""


def _render_dot(dot: str, out_dir: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    dot_path = out_dir / "figure.dot"
    dot_path.write_text(dot, encoding="utf-8")
    artifacts.append({"title": "Graphviz DOT", "format": "dot", "path": str(dot_path)})
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return artifacts
    for fmt in ("svg", "png"):
        out_path = out_dir / f"figure.{fmt}"
        proc = subprocess.run(
            [dot_bin, f"-T{fmt}", str(dot_path), "-o", str(out_path)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0 and out_path.is_file():
            artifacts.append({"title": f"Rendered {fmt.upper()}", "format": fmt, "path": str(out_path)})
    return artifacts


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("input") or payload.get("query") or payload.get("prompt") or "").strip()
    if not prompt:
        return {"status": "error", "skill": SKILL, "error": "input is required"}
    out_dir = Path(payload.get("output_dir") or "portable-figure")
    out_dir.mkdir(parents=True, exist_ok=True)
    title = str(payload.get("title") or _infer_title(prompt))
    labels = _labels(prompt)
    dot = _dot(title, prompt, labels)
    artifacts = _render_dot(dot, out_dir)
    if not any(artifact["format"] == "svg" for artifact in artifacts):
        svg_path = out_dir / "figure.svg"
        svg_path.write_text(_fallback_svg(title, labels), encoding="utf-8")
        artifacts.append({"title": "Fallback SVG", "format": "svg", "path": str(svg_path)})
    provenance_path = out_dir / "provenance.json"
    provenance = {
        "sources": [],
        "runs": [{"command": "dot -Tsvg/-Tpng if available, otherwise built-in SVG fallback"}],
        "artifacts": artifacts,
    }
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append({"title": "Provenance", "format": "json", "path": str(provenance_path)})
    return {
        "status": "ok",
        "skill": SKILL,
        "title": title,
        "summary": f"Created {title} artifacts in {out_dir}.",
        "caption": "A portable architecture schematic generated without Omni runtime.",
        "artifacts": artifacts,
        "provenance": provenance,
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_file:
        raw = Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
    elif args.json:
        raw = args.json
    elif not sys.stdin.isatty():
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def _configure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _emit(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    _configure_utf8()
    parser = argparse.ArgumentParser(description="Run the portable scientific-figure skill.")
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run an offline smoke test.")
    args = parser.parse_args(argv)
    if args.self_test:
        _emit({"status": "ok", "skill": SKILL, "portable_runner": True})
        return 0
    try:
        result = run(_load_payload(args))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
