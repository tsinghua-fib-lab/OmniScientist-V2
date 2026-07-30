#!/usr/bin/env python3
"""Portable research-ideation runner with no OmniScientist dependency.

Usage:
  python3 scripts/run.py --json '{"input": "How can LLMs improve scientific reasoning?"}'
  python3 scripts/run.py --json '{"input": "...", "n_ideas": 1, "use_tools": false}'
  python3 scripts/run.py --json-file payload.json
  echo '{"input": "..."}' | python3 scripts/run.py
  python3 scripts/run.py --self-test

Environment variables:
  LLM_GATEWAY_BASE_URL  — OpenAI-compatible API base URL
  LLM_GATEWAY_API_KEY   — API key
  LLM_MODEL             — model name (default claude-sonnet-4-20250514)
  S2_API_KEY            — optional Semantic Scholar API key for higher rate limits
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)


def build_llm_config() -> dict[str, Any]:
    """Read the portable runner's OpenAI-compatible environment contract."""
    return {
        "base_url": os.getenv("LLM_GATEWAY_BASE_URL", ""),
        "api_key": os.getenv("LLM_GATEWAY_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"),
        "temperature": 0.7,
    }


def _chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _response_error_detail(response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif error:
            detail = str(error)
        elif payload.get("message"):
            detail = str(payload["message"])
    if not detail:
        detail = response.text
    return " ".join(detail.split())[:500] or response.reason_phrase or "request rejected"


class OpenAICompatibleLLM:
    """HTTP adapter used only by the copy-only portable runner."""

    def __init__(self, config: dict[str, Any]) -> None:
        from core import LLMConfigurationError

        missing = [
            name
            for name in ("base_url", "api_key", "model")
            if not str(config.get(name) or "").strip()
        ]
        if missing:
            raise LLMConfigurationError(
                "LLM endpoint/API key/model is not configured; missing: "
                + ", ".join(missing)
            )
        self._config = dict(config)
        self.temperature = config.get("temperature", 0.7)

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        from core import LLMHTTPError, LLMProtocolError

        request_payload = dict(payload)
        request_payload.setdefault("model", self._config["model"])
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config['api_key']}",
        }
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            response = client.post(
                _chat_endpoint(str(self._config["base_url"])),
                json=request_payload,
                headers=headers,
            )
        if response.is_error:
            detail = _response_error_detail(response)
            raise LLMHTTPError(
                response.status_code,
                f"LLM endpoint returned HTTP {response.status_code}: {detail}",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMProtocolError("LLM endpoint returned non-JSON data") from exc
        if not isinstance(data, dict):
            raise LLMProtocolError("LLM endpoint returned an invalid response object")
        return data


def _self_test() -> dict:
    """Validate imports and prompt formatting without network access."""
    try:
        from core import (
            CONCEPT_EXTRACTION_PROMPT,
            CONCEPT_MERGE_PROMPT,
            CRITIC_PROMPT,
            GAP_ANALYSIS_PROMPT,
            IDEA_REFINE_PROMPT,
            IDEATION_PROMPT,
            PAPER_RELEVANCE_FILTER_PROMPT,
            SEARCH_QUERY_PROMPT,
        )
        SEARCH_QUERY_PROMPT.format(research_question="test")
        PAPER_RELEVANCE_FILTER_PROMPT.format(
            research_question="test",
            paper_titles="[0] paper",
        )
        CONCEPT_EXTRACTION_PROMPT.format(title="paper", abstract="abstract")
        CONCEPT_MERGE_PROMPT.format(core_concepts="core", domain_concepts="domain")
        GAP_ANALYSIS_PROMPT.format(
            research_question="test",
            core_concepts="core",
            domain_concepts="domain",
            papers_text="paper summary",
        )
        IDEATION_PROMPT.format(
            target_gap="test", core_concepts="a, b",
            reference_papers_text="none",
        )
        CRITIC_PROMPT.format(
            title="t", background="b", related_work="r",
            gap_analysis="g", proposed_method="m",
            research_question="q", concepts="c",
        )
        IDEA_REFINE_PROMPT.format(
            title="t",
            background="b",
            related_work="r",
            gap_analysis="g",
            proposed_method="m",
            critique_text="c",
            user_feedback="f",
        )
        return {
            "status": "ok",
            "skill": "research-ideation",
            "portable_runner": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "skill": "research-ideation",
            "error": str(e),
            "summary": f"Self-test failed: {e}",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="research-ideation portable runner")
    parser.add_argument("--json", type=str, default=None, help="JSON input string")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run self-test (no network)")
    parser.add_argument("--output-dir", type=str, default=None, help="Write report & provenance here")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(_self_test(), ensure_ascii=False, indent=2))
        return

    # Read input. Portable hosts expect one structured JSON result on stdout,
    # including malformed-input failures; never leak a parser traceback.
    # Prefer --json-file (UTF-8) so PowerShell cannot strip quotes or mojibake
    # Chinese output_dir values through the console code page.
    try:
        if args.json_file:
            input_data = json.loads(
                Path(args.json_file).expanduser().read_text(encoding="utf-8-sig")
            )
        elif args.json:
            input_data = json.loads(args.json)
        elif not sys.stdin.isatty():
            raw = sys.stdin.buffer.read().decode("utf-8-sig")
            input_data = json.loads(raw) if raw.strip() else {}
        else:
            input_data = {}
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        print(json.dumps({
            "status": "error",
            "skill": "research-ideation",
            "error": "Invalid JSON input.",
            "error_info": {
                "code": "invalid_json",
                "category": "input",
                "retryable": False,
            },
        }, ensure_ascii=False))
        return
    if not isinstance(input_data, dict):
        print(json.dumps({
            "status": "error",
            "skill": "research-ideation",
            "error": "JSON input must be an object.",
            "error_info": {
                "code": "invalid_json",
                "category": "input",
                "retryable": False,
            },
        }, ensure_ascii=False))
        return

    question = input_data.get("input") or input_data.get("query") or input_data.get("topic")
    if not question:
        print(json.dumps({
            "status": "error",
            "skill": "research-ideation",
            "error": "input is required",
            "error_info": {
                "code": "missing_input",
                "category": "input",
                "retryable": False,
            },
        }, ensure_ascii=False))
        return

    from core import (
        DEFAULT_N_IDEAS,
        LiteratureSearchError,
        classify_llm_error,
        is_non_retryable_llm_error,
        run_pipeline,
    )

    start = time.time()
    secret = os.getenv("LLM_GATEWAY_API_KEY", "")

    def _progress(msg: str, frac: float) -> None:
        print(f"  [{frac*100:.0f}%] {msg}", file=sys.stderr)

    try:
        llm = OpenAICompatibleLLM(build_llm_config())
        result = run_pipeline(
            research_question=question,
            n_ideas=max(
                1,
                min(
                    5,
                    int(input_data.get("n_ideas", DEFAULT_N_IDEAS) or DEFAULT_N_IDEAS),
                ),
            ),
            use_tools=bool(input_data.get("use_tools", True)),
            progress=_progress,
            llm=llm,
        )
    except Exception as e:
        message = str(e).replace(secret, "[REDACTED]") if secret else str(e)
        literature_failure = isinstance(e, LiteratureSearchError)
        non_retryable = not literature_failure and is_non_retryable_llm_error(e)
        code = (
            "literature_search_failed"
            if literature_failure
            else classify_llm_error(e)
            if non_retryable
            else "pipeline_error"
        )
        result = {
            "status": "error",
            "skill": "research-ideation",
            "outcome": {"code": code},
            "error": message,
            "summary": f"Pipeline failed: {message}",
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

    result["skill"] = "research-ideation"
    result["elapsed_seconds"] = round(time.time() - start, 1)

    # Write portable artifacts.
    if args.output_dir and result.get("status") == "ok":
        os.makedirs(args.output_dir, exist_ok=True)

        report_path = os.path.join(args.output_dir, "report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(_build_report(result))
        result.setdefault("artifacts", []).append(
            {"path": report_path, "kind": "report", "ext": "md"}
        )

        prov_path = os.path.join(args.output_dir, "provenance.json")
        prov = {
            "skill": "research-ideation",
            "research_question": question,
            "paper_count": result.get("steps", {}).get("search", {}).get("paper_count", 0),
            "gap_count": len(result.get("steps", {}).get("gaps", [])),
            "idea_count": len(result.get("steps", {}).get("raw_ideas", [])),
            "final_idea_title": result.get("final_idea", {}).get("title", ""),
            "artifacts": result.get("artifacts", []),
        }
        with open(prov_path, "w", encoding="utf-8") as f:
            json.dump(prov, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _build_report(result: dict) -> str:
    question = result.get("research_question", "")
    lines = [f"# Research Ideation Report: {question}", ""]

    steps = result.get("steps", {})
    search = steps.get("search", {})
    if search:
        lines += [
            "## Literature Review",
            f"- Search queries: {', '.join(search.get('queries', []))}",
            f"- Relevant papers: {search.get('paper_count', 0)}",
            f"- Core concepts: {', '.join(search.get('core_concepts', [])[:10])}",
            "",
        ]

    gaps = steps.get("gaps", [])
    if gaps:
        lines += ["## Research Gaps", ""]
        for g in gaps:
            lines += [f"- **Gap {g.get('gap_id', '?')}**: {g.get('gap', '')}", ""]

    final = result.get("final_idea", {})
    if final:
        lines += [
            "## Final Idea",
            f"### {final.get('title', '')}",
            "",
            f"**Background**: {final.get('background', '')}",
            "",
            f"**Method**: {final.get('proposed_method', '')}",
            "",
        ]

    return "\n".join(lines)


if __name__ == "__main__":
    main()
