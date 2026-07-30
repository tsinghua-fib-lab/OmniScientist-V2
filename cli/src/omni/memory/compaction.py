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
import string
from collections.abc import Awaitable, Callable
from typing import Any

from omni.config.settings import OmniSettings
from omni.core.llm.client import chat_result
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


_NON_ASCII_BYTES = bytes(range(0x80, 0x100))
_ASCII_WORD_BYTES = (
    string.ascii_letters + string.digits + string.whitespace
).encode("ascii")

# Bytes per token, by the only two things that actually move the rate. Measured
# against cl100k_base over the text a turn really sends — the assembled system
# prompt, the OpenAI schemas of the full default tool surface, tool observations
# built from real paper abstracts, a folded summary, this repository's own source
# and docs, and Chinese documentation:
#
#   ASCII words and whitespace   ~5.0-5.5   English prose, identifiers, markdown
#   ASCII punctuation            ~1.5       JSON structure, brackets, operators
#   anything non-ASCII           ~1.9-3.0   a CJK character is one token, 3 bytes
#
# One divisor over all bytes cannot serve those at once, and the shipped 3.0 was
# roughly the CJK rate applied to everything: it over-counted English research
# prose by 94% while *under*-counting Chinese markdown by 24% and Greek by 31%.
# Splitting the count is what removes the spread instead of relocating it — a
# recalibrated single divisor keeps the same 2.1x spread and moves its error onto
# the side that loses a request.
#
# Each figure rounds against its measurement, so the estimate lands at or above a
# real count for every sample (1.01x to 1.26x). That direction is deliberate:
# over-counting compacts earlier than it had to, while under-counting grows a
# transcript into a request the provider refuses.
_BYTES_PER_TOKEN_WIDE = 1.9
_BYTES_PER_TOKEN_WORD = 5.0
_BYTES_PER_TOKEN_PUNCT = 1.5


def _heuristic_tokens(text: str) -> int:
    """Conservative token estimate from a UTF-8 byte census, no tokenizer needed.

    Three ``bytes.translate`` passes rather than a character loop: this runs over
    every message of every iteration, and a transcript near its ceiling is
    megabytes of text.
    """
    raw = text.encode("utf-8")
    ascii_only = raw.translate(None, _NON_ASCII_BYTES)
    punct = len(ascii_only.translate(None, _ASCII_WORD_BYTES))
    return (
        int(
            (len(raw) - len(ascii_only)) / _BYTES_PER_TOKEN_WIDE
            + (len(ascii_only) - punct) / _BYTES_PER_TOKEN_WORD
            + punct / _BYTES_PER_TOKEN_PUNCT
        )
        + 1
    )


def estimate_tokens(text: str) -> int:
    """Token estimate for compaction thresholds.

    Uses the real ``tiktoken`` (cl100k_base) tokenizer when the optional
    dependency is installed, so budgets match what the model actually sees;
    otherwise falls back to a calibrated UTF-8 byte estimate.

    The fallback is the path that matters, and it has to be in the same units as
    the limits it is compared against. ``cost.max_total_tokens`` is enforced on
    provider-reported usage and a context window is the provider's own count, so
    an estimator that runs a third to two-thirds high does not merely mis-report
    — it makes every compaction threshold fire that much earlier than its
    arithmetic intends, discarding context to save a budget that was never at
    risk.
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
                "Preserve the language of each source statement; do not translate it. "
                "Name the task that produced each finished deliverable; where the transcript does not "
                "say which task produced it, record that rather than assigning one. Report what was "
                "claimed as claimed — an earlier turn saying work is complete is not evidence that it is."
            )
            answer = await chat_result(llm, system, transcript)
            out = answer.content
            if on_llm_call is not None:
                try:
                    await on_llm_call(system, transcript, out)
                except Exception:  # noqa: BLE001
                    logger.debug("compaction observer failed", exc_info=True)
            # This note replaces the messages it summarises, so a half-written
            # one does not merely read badly — it becomes the only record of
            # what those turns decided, and the bullets after the cut are gone
            # for every turn that follows. The heuristic summary below is
            # blunter but complete, which is the property that matters here.
            if answer.truncated_by_output_cap:
                logger.warning(
                    "[compaction] discarded a summary cut off at the output cap (%d chars); "
                    "falling back to the deterministic summary",
                    len(out),
                )
            elif out and out.strip():
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
    tasks: list[str] = []
    for m in convo:
        text = str(m.get("content") or "")
        refs.extend(_ARTIFACT_RE.findall(text))
        tasks.extend(_TASK_RE.findall(text))
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
    if tasks:
        # Which tasks the window covered. Without them the summary states that
        # deliverables exist and leaves the reader to guess whose they are, which
        # is how one task's reply came to carry another's report.
        lines.append("Tasks in this window: " + ", ".join(list(dict.fromkeys(tasks))[:8]))
    return "\n".join(lines)
