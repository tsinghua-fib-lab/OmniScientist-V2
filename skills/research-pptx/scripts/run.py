#!/usr/bin/env python3
"""Portable runner for research-pptx (Claude Code / Codex / OpenClaw).

Self-contained: imports the skill's own engine.py by path, never imports omni.
Stores the PPTX into ./out/ and returns a file:// uri.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

SKILL = "research-pptx"
_SKILL_DIR = Path(__file__).resolve().parent.parent


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "research_pptx_engine", _SKILL_DIR / "engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["research_pptx_engine"] = module
    spec.loader.exec_module(module)
    return module.ResearchPptxEngine


class _MockArtifacts:
    def __init__(self, out_dir: Path) -> None:
        self._dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    async def put_bytes(
        self,
        data,
        *,
        kind,
        title,
        ext,
        mime,
        session_id="",
        task_id="",
        subtask_id="",
        workflow_run_id="",
    ):
        del kind, session_id, task_id, subtask_id, workflow_run_id
        safe = "".join(c for c in title if c.isalnum() or c in " _-")[:60].strip() or "deck"
        path = self._dir / f"{safe}.{ext}"
        path.write_bytes(data)
        return type(
            "_Stored",
            (),
            {
                "uri": f"file://{path}",
                "path": path,
                "mime": mime,
                "size_bytes": len(data),
            },
        )()

    async def get_bytes(self, uri):
        return Path(uri.replace("file://", ""))


class _MockPaths:
    def __init__(self, root: Path) -> None:
        self.artifacts_dir = root / "out"


class _MockCtx:
    def __init__(self, llm) -> None:
        root = Path.cwd()
        self.llm = llm
        self.artifacts = _MockArtifacts(root / "out")
        self.paths = _MockPaths(root)
        self.session_id = ""
        self.db = None  # provenance tools no-op without a store


def _make_llm():
    import os
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OMNI_MODEL_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OMNI_MODEL_API_KEY")
    model = os.environ.get("OMNI_MODEL", "gpt-4o-mini")
    if not (base_url and api_key):
        return None

    import httpx

    class _LLM:
        async def chat(self, system, user, *, temperature=0.4, max_tokens=4096):
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}],
                        "temperature": temperature, "max_tokens": max_tokens},
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
    return _LLM()


async def _run(payload: dict) -> dict:
    llm = _make_llm()
    if llm is None:
        return {"status": "error", "skill": SKILL,
                "error": "set OPENAI_BASE_URL + OPENAI_API_KEY to run standalone."}
    # Portable single-shot runners have no durable state for review/resume.
    # Omni/MCP owns that lifecycle; direct execution always renders once.
    payload.pop("resume_token", None)
    payload.pop("approved_plan", None)
    payload["review_mode"] = "none"
    engine = _load_engine()()
    engine.ctx = _MockCtx(llm)
    return await engine.execute(**payload)


def _load_payload(args) -> dict:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the portable research-pptx skill.")
    parser.add_argument("--json", help="Input JSON object.")
    parser.add_argument(
        "--json-file",
        help="UTF-8 JSON file. Prefer this on Windows/PowerShell; --json quoting is unreliable there.",
    )
    parser.add_argument("--self-test", action="store_true", help="Offline smoke test.")
    args = parser.parse_args(argv)

    if args.self_test:
        # Verify engine + siblings import cleanly without network/LLM.
        try:
            _load_engine()
            print(json.dumps({"status": "ok", "skill": SKILL, "portable_runner": True}))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"status": "error", "skill": SKILL, "error": str(exc)}))
            return 0

    try:
        result = asyncio.run(_run(_load_payload(args)))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "skill": SKILL, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
