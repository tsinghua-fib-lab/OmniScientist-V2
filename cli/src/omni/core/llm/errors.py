"""Provider-neutral LLM error classification and user-safe messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMErrorInfo:
    category: str
    terminated_reason: str
    user_message: str
    retryable: bool = False
    fallback_allowed: bool = False
    status_code: int | None = None
    request_id: str = ""
    internal_detail: str = ""


class LLMProviderError(RuntimeError):
    """Classified provider failure; ``str(exc)`` is safe for end users."""

    def __init__(self, info: LLMErrorInfo) -> None:
        super().__init__(info.user_message)
        self.info = info
        self.status_code = info.status_code
        self.retryable = info.retryable
        self.fallback_allowed = info.fallback_allowed


# The reason code for a response the output-token cap cut short. It is the
# host's ceiling, not a provider fault, so it is neither retryable nor
# recoverable by failing over: the identical request would be cut at the
# identical place.
OUTPUT_CAP_TRUNCATED_REASON = "output_cap_truncated"


class LLMOutputTruncated(LLMProviderError):
    """The response stopped at the output-token cap partway through the answer.

    Raised only where the caller has no other channel to learn it — a streaming
    generator, which has already handed every delta to its consumer and cannot
    hand back a finish reason afterwards. Callers holding a
    :class:`~omni.core.llm.client.ChatWithToolsResult` read ``finish_reason``
    instead and keep the partial text.
    """

    def __init__(self, *, model: str = "", produced_chars: int = 0) -> None:
        super().__init__(
            LLMErrorInfo(
                category="output_truncated",
                terminated_reason=OUTPUT_CAP_TRUNCATED_REASON,
                user_message=(
                    "The answer stopped at the model's output-token limit and is "
                    "incomplete. Ask for the remainder, or request the answer in "
                    "sections."
                ),
                internal_detail=(
                    f"model={model or '-'} produced_chars={produced_chars} "
                    "finish_reason=length"
                ),
            )
        )
        self.produced_chars = produced_chars


def classify_llm_exception(exc: Exception) -> LLMErrorInfo:
    if isinstance(exc, LLMProviderError):
        return exc.info
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) else None
    detail = _response_detail(response) or str(exc)
    lower = detail.lower()
    request_id = _request_id(response)

    if status == 400 and any(
        marker in lower
        for marker in ("tool output", "tool_call", "tool call", "function call", "function_call")
    ):
        return LLMErrorInfo(
            category="transcript_invalid",
            terminated_reason="llm_transcript_invalid",
            user_message=(
                "The model service rejected the tool-call transcript. Omni retained the current results "
                "and stopped further calls; retry directly or resume from the saved run."
            ),
            status_code=status,
            request_id=request_id,
            internal_detail=detail,
        )
    if status in {401, 403}:
        return LLMErrorInfo(
            "authentication", "llm_auth_error",
            "Model authentication failed. Check the configured provider API key and access permissions.",
            fallback_allowed=True, status_code=status,
            request_id=request_id, internal_detail=detail,
        )
    if status == 404:
        return LLMErrorInfo(
            "configuration", "llm_configuration_error",
            "The model or endpoint is unavailable. Check provider, base_url, and model settings.",
            fallback_allowed=True, status_code=status,
            request_id=request_id, internal_detail=detail,
        )
    if status == 429:
        return LLMErrorInfo(
            "rate_limit", "llm_rate_limited", "The model service is rate limiting requests. Retry later.",
            retryable=True, fallback_allowed=True, status_code=status,
            request_id=request_id, internal_detail=detail,
        )
    if status is not None and 500 <= status < 600:
        return LLMErrorInfo(
            "unavailable", "llm_unavailable", "The model service is temporarily unavailable; this turn stopped.",
            retryable=True, fallback_allowed=True, status_code=status,
            request_id=request_id, internal_detail=detail,
        )
    if status is not None and 400 <= status < 500:
        return LLMErrorInfo(
            "invalid_request", "llm_invalid_request", "The model service rejected the request. Check model compatibility and configuration.",
            fallback_allowed=True, status_code=status,
            request_id=request_id, internal_detail=detail,
        )
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return LLMErrorInfo(
            "timeout", "llm_timeout", "The model call timed out; existing results were retained.",
            retryable=True, fallback_allowed=True, internal_detail=detail,
        )
    if any(token in type(exc).__name__.lower() for token in ("connect", "network", "protocol")):
        return LLMErrorInfo(
            "unavailable", "llm_unavailable", "Could not connect to the model service; existing results were retained.",
            retryable=True, fallback_allowed=True, internal_detail=detail,
        )
    return LLMErrorInfo(
        "unknown", "llm_error", "The model call did not complete; Omni retained the current results.",
        internal_detail=detail,
    )


def from_http_status_error(exc: Exception) -> LLMProviderError:
    return LLMProviderError(classify_llm_exception(exc))


def _response_detail(response: Any) -> str:
    if response is None:
        return ""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:2000]
        return json.dumps(payload, ensure_ascii=False, default=str)[:2000]
    try:
        return str(response.text or "")[:2000]
    except Exception:  # noqa: BLE001
        return ""


def _request_id(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    return str(headers.get("x-request-id") or headers.get("request-id") or "")


__all__ = [
    "OUTPUT_CAP_TRUNCATED_REASON",
    "LLMErrorInfo", "LLMOutputTruncated", "LLMProviderError",
    "classify_llm_exception", "from_http_status_error",
]
