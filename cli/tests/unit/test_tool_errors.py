from __future__ import annotations

from omni.core.tool_errors import (
    FATAL_TURN,
    INVALID_ARGS,
    RETRYABLE_IO,
    SKILL_FAILED_PARTIAL,
    UNPAYABLE,
    classify_tool_error,
    short_skill_observation,
    strip_traceback,
)


def test_classify_invalid_args() -> None:
    assert (
        classify_tool_error(name="write_file", error_code="tool_arguments_invalid", status="rejected")
        == INVALID_ARGS
    )


def test_classify_retryable_io() -> None:
    assert classify_tool_error(error_code="429", status="failed") == RETRYABLE_IO
    assert classify_tool_error(error="rate limited by provider", status="failed") == RETRYABLE_IO


def test_classify_unpayable_vlm() -> None:
    assert (
        classify_tool_error(
            name="run_skill",
            error_code="vlm_unavailable",
            status="failed",
            result={"skill_name": "livefigure"},
        )
        == UNPAYABLE
    )


def test_classify_vlm_http_503_is_retryable_not_unconfigured() -> None:
    assert (
        classify_tool_error(
            name="livefigure",
            status="failed",
            error="VLM image endpoint returned HTTP 503.",
            retryable=True,
        )
        == RETRYABLE_IO
    )
    payload = short_skill_observation(
        "livefigure",
        error="VLM image endpoint returned HTTP 503.",
        status="failed",
        error_class=RETRYABLE_IO,
    )
    assert payload is not None
    assert "not configured" not in payload["hint"].lower()
    assert "retry" in payload["hint"].lower()


def test_classify_does_not_treat_get_task_object_status_as_tool_failure() -> None:
    result = {
        "ref": "task:abc",
        "task_id": "abc",
        "task_status": "failed",
        "status": "failed",
        "artifacts": [{"path": "paper.md"}],
        "failure_reason": "VLM image endpoint returned HTTP 503.",
    }
    assert (
        classify_tool_error(name="get_task", status="succeeded", result=result)
        == ""
    )
    assert short_skill_observation("get_task", result=result, status="succeeded") is None


def test_classify_skill_partial() -> None:
    assert (
        classify_tool_error(
            name="run_skill",
            status="failed",
            result={"skill_name": "research-pptx", "artifacts": [{"path": "deck.pptx"}]},
        )
        == SKILL_FAILED_PARTIAL
    )


def test_classify_fatal() -> None:
    assert classify_tool_error(error_code="storage_corrupt", status="failed") == FATAL_TURN


def test_strip_traceback_keeps_first_line() -> None:
    wall = (
        "PPTX renderer crashed\n"
        "Traceback (most recent call last):\n"
        '  File "skill.py", line 1, in <module>\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n"
    )
    assert strip_traceback(wall) == "PPTX renderer crashed"
    assert "Traceback" not in strip_traceback(wall)


def test_short_skill_observation_is_actionable() -> None:
    payload = short_skill_observation(
        "run_skill",
        result={
            "skill_name": "research-pptx",
            "error": "node is not installed\nTraceback (most recent call last):\n  File x",
        },
        status="failed",
    )
    assert payload is not None
    assert payload["error_class"] == SKILL_FAILED_PARTIAL
    assert "Traceback" not in payload["error"]
    assert "pptx" in payload["hint"].lower() or "PPTX" in payload["hint"]
