"""Canonical, provider-safe tool-call transcript normalization.

Tool budgets, cancellation, policy rejection, and process crashes may stop a
call before its handler runs.  The model protocol still requires every emitted
tool call to have exactly one result.  This module repairs that structural
contract independently from whether execution succeeded.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_MESSAGE_KEYS = {"role", "content", "name", "tool_call_id", "tool_calls"}


@dataclass(frozen=True, slots=True)
class NormalizedToolTranscript:
    messages: list[dict[str, Any]]
    repairs: list[str] = field(default_factory=list)
    valid: bool = True


def normalize_tool_transcript(messages: list[dict[str, Any]]) -> NormalizedToolTranscript:
    """Return a canonical transcript with every tool call structurally closed."""
    out: list[dict[str, Any]] = []
    repairs: list[str] = []
    pending: dict[str, str] = {}
    used_ids: set[str] = set()
    result_id_queues: dict[str, deque[str]] = {}
    generated = 0

    def flush_pending() -> None:
        for call_id, name in list(pending.items()):
            out.append(_aborted_result(call_id, name))
            repairs.append(f"missing_tool_result:{call_id}")
            pending.pop(call_id, None)

    for raw in messages:
        if not isinstance(raw, dict):
            repairs.append("invalid_message")
            continue
        role = str(raw.get("role") or "")
        if role not in {"system", "user", "assistant", "tool"}:
            repairs.append(f"invalid_role:{role or 'missing'}")
            continue

        if role == "tool":
            raw_call_id = str(raw.get("tool_call_id") or "")
            queue = result_id_queues.get(raw_call_id)
            call_id = queue.popleft() if queue else raw_call_id
            if not call_id or call_id not in pending:
                repairs.append(f"orphan_tool_result:{call_id or 'missing'}")
                continue
            message = {key: value for key, value in raw.items() if key in _MESSAGE_KEYS and value is not None}
            message["role"] = "tool"
            message["tool_call_id"] = call_id
            message["name"] = pending[call_id]
            message["content"] = _content_text(message.get("content"))
            out.append(message)
            pending.pop(call_id, None)
            continue

        if pending:
            flush_pending()

        message = {key: value for key, value in raw.items() if key in _MESSAGE_KEYS and value is not None}
        message["role"] = role
        if role != "assistant" or not raw.get("tool_calls"):
            if "content" in message:
                message["content"] = _content_text(message.get("content"))
            out.append(message)
            continue

        calls: list[dict[str, Any]] = []
        result_id_queues = {}
        for raw_call in raw.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                repairs.append("invalid_tool_call")
                continue
            function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            name = str(function.get("name") or raw_call.get("name") or "unknown_tool")
            original_id = str(raw_call.get("id") or "")
            call_id = original_id
            if not call_id or call_id in used_ids:
                original = call_id or "missing"
                while not call_id or call_id in used_ids:
                    generated += 1
                    call_id = f"omni_call_{generated}"
                repairs.append(f"normalized_tool_call_id:{original}->{call_id}")
            used_ids.add(call_id)
            result_id_queues.setdefault(original_id, deque()).append(call_id)
            arguments = function.get("arguments", raw_call.get("arguments", {}))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False, default=str)
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
            pending[call_id] = name
        message["content"] = _content_text(message.get("content"))
        message["tool_calls"] = calls
        out.append(message)

    flush_pending()
    return NormalizedToolTranscript(messages=out, repairs=repairs, valid=_is_valid(out))


def _aborted_result(call_id: str, name: str) -> dict[str, Any]:
    content = json.dumps(
        {
            "status": "aborted",
            "reason": "tool call was not completed before the next model turn",
            "retryable": True,
        },
        ensure_ascii=False,
    )
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_valid(messages: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    for message in messages:
        if pending and message.get("role") != "tool":
            return False
        if message.get("role") == "assistant":
            pending = {str(call.get("id") or "") for call in message.get("tool_calls") or []}
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                return False
            pending.remove(call_id)
    return not pending


__all__ = ["NormalizedToolTranscript", "normalize_tool_transcript"]
