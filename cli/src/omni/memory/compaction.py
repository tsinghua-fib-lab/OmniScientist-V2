"""Transcript compaction — summarise older turns so a long session fits budget.

The orchestrator persists the summary as a ``content_type="compaction"`` message
and flags the covered turns ``compacted`` (hidden from the prompt, kept for
``replay``). Crucially, durable facts are flushed to long-term memory *before*
the covered turns are hidden, so compaction never silently drops information.

This module only builds the summary text (LLM when a real provider is set, a
deterministic heuristic otherwise); the DB mechanics live in the orchestrator.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from omni.config.settings import OmniSettings
from omni.memory.sanitize import redact_secrets

logger = logging.getLogger(__name__)

_ARTIFACT_RE = re.compile(r"artifact://[A-Za-z0-9]+")
_TASK_RE = re.compile(r"task[`\s]+([A-Za-z0-9]{6,40})", re.IGNORECASE)

_MICROCOMPACT_PLACEHOLDER = "...[older tool result compacted; full input/output remains in run events]"

# Lazily-loaded real tokenizer. ``tiktoken`` is an *optional* dependency (extra
# ``tok``): if it (and its BPE vocab) load, we count real tokens; otherwise we
# permanently fall back to the offline heuristic for this process. ``_TRIED``
# guards against re-attempting a load (incl. a first-use network fetch) on every
# call. Set ``OMNI_DISABLE_TIKTOKEN=1`` to force the heuristic even when present.
_TIKTOKEN_ENC: Any | None = None
_TIKTOKEN_TRIED = False


def _tiktoken_encoder() -> Any | None:
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    _TIKTOKEN_TRIED = True
    if os.environ.get("OMNI_DISABLE_TIKTOKEN"):
        return None
    try:  # optional + offline-safe: any failure → heuristic for the whole run
        import tiktoken

        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — ImportError, or vocab fetch failing offline
        _TIKTOKEN_ENC = None
    return _TIKTOKEN_ENC


def _heuristic_tokens(text: str) -> int:
    """Conservative UTF-8 estimate with no writing-system branch."""
    return (len(text.encode("utf-8")) + 2) // 3 + 1


def estimate_tokens(text: str) -> int:
    """Token estimate for compaction thresholds.

    Uses the real ``tiktoken`` (cl100k_base) tokenizer when the optional
    dependency is installed, so budgets match what the model actually sees;
    otherwise falls back to a conservative UTF-8 byte estimate.
    """
    if not text:
        return 0
    enc = _tiktoken_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=())) + 1
        except Exception:  # noqa: BLE001 — never let counting crash a turn
            pass
    return _heuristic_tokens(text)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Sum :func:`estimate_tokens` over the ``content`` of chat messages."""
    return sum(estimate_tokens(str(m.get("content") or "")) for m in messages)


def microcompact_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = 4,
    max_chars: int = 600,
) -> int:
    """Shrink the content of *older* tool observations in place (Claude-style).

    Keeps the most recent ``keep_last`` ``role="tool"`` messages verbatim and
    truncates older ones to ``max_chars`` + a placeholder. This clears the
    cheapest, bulkiest context first (long tool dumps) during a long single
    ReAct turn without dropping the messages — so the provider-side
    ``tool_call``↔``tool_result`` linkage stays valid. Idempotent (already
    trimmed messages are skipped). Returns the number of messages trimmed.
    """
    if keep_last < 0:
        return 0
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_idxs) <= keep_last:
        return 0
    older = tool_idxs[:-keep_last] if keep_last else tool_idxs
    trimmed = 0
    for i in older:
        content = str(messages[i].get("content") or "")
        if len(content) <= max_chars or content.rstrip().endswith(_MICROCOMPACT_PLACEHOLDER):
            continue
        messages[i]["content"] = content[:max_chars].rstrip() + "\n" + _MICROCOMPACT_PLACEHOLDER
        trimmed += 1
    return trimmed


async def summarize_messages(
    llm: Any,
    settings: OmniSettings,
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 1600,
    on_llm_call: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> str:
    """Summarise older conversation messages into a compact bridge note."""
    convo = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not convo:
        return ""
    transcript = "\n".join(
        f"{m['role']}: {str(m['content'])[:400]}" for m in convo
    )[:8000]
    provider = (settings.model.provider or "mock").lower()
    if llm and provider not in ("mock", "", "offline"):
        try:
            system = (
                "Compress the older conversation into at most eight concise bullets. Preserve research "
                "goals, conclusions, decisions, artifact:// URIs, task IDs, and unresolved questions. "
                "Preserve the language of each source statement; do not translate it."
            )
            out = await llm.chat(system, transcript)
            if on_llm_call is not None:
                try:
                    await on_llm_call(system, transcript, out)
                except Exception:  # noqa: BLE001
                    logger.debug("compaction observer failed", exc_info=True)
            if out and out.strip():
                return redact_secrets(out.strip())[:max_chars]
        except Exception:  # noqa: BLE001
            pass
    return redact_secrets(_heuristic_summary(convo))[:max_chars]


def _heuristic_summary(convo: list[dict[str, Any]]) -> str:
    user_asks = [str(m["content"]).strip() for m in convo if m.get("role") == "user"]
    last_asst = next(
        (str(m["content"]) for m in reversed(convo) if m.get("role") == "assistant"), ""
    )
    refs: list[str] = []
    for m in convo:
        text = str(m.get("content") or "")
        refs.extend(_ARTIFACT_RE.findall(text))
    lines = ["Earlier conversation summary (automatic compaction):"]
    if user_asks:
        lines.append("User requests:")
        for ask in user_asks[:6]:
            lines.append(f"- {ask[:120]}")
    if last_asst:
        lines.append(f"Latest assistant progress: {last_asst[:200]}")
    if refs:
        uniq = list(dict.fromkeys(refs))[:8]
        lines.append("Referenced artifacts: " + ", ".join(uniq))
    return "\n".join(lines)
