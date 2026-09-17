"""Offline regressions for candidate retries and nested execution deadlines."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from posterlib.generation import model_runtime  # noqa: E402
from posterlib.workflows import request_normalization, runtime_budget  # noqa: E402

HTML = "<!doctype html><html><body>verified title</body></html>"


def test_default_allows_three_repairs():
    calls = []

    async def chat(*args, **kwargs):
        calls.append(args)
        return HTML if len(calls) == 4 else HTML.replace("verified title", "wrong")

    result = asyncio.run(
        model_runtime.request_html(
            SimpleNamespace(chat=chat),
            system="draft",
            user="paper",
            repair_system="repair",
            repair_context="verified paper",
            validate=lambda value: {"status": "ok" if "verified title" in value else "error"},
        )
    )
    assert result == HTML
    assert len(calls) == 4


@pytest.mark.parametrize(
    "wrapper", ["{}", "Here is the poster:\n```html\n{}\n```\nDone.", "Poster:\n{}\nDone."]
)
def test_wrapping_does_not_spend_repair_budget(wrapper):
    async def chat(*args, **kwargs):
        return wrapper.format(HTML)

    assert (
        asyncio.run(
            model_runtime.request_html(
                SimpleNamespace(chat=chat),
                system="",
                user="",
                repair_system="",
                repair_context="",
                max_repair_attempts=0,
                validate=lambda value: {"status": "ok"},
            )
        )
        == HTML
    )


@pytest.mark.parametrize("value", [HTML + HTML, "<div>preamble</div>" + HTML, HTML[:-7]])
def test_ambiguous_or_incomplete_document_is_rejected(value):
    assert model_runtime._normalize_complete_html_response(value) is None


@pytest.mark.parametrize("value", [True, -1, 1.5, 11, "3"])
def test_invalid_repair_limits(value):
    with pytest.raises(model_runtime.ModelBoundaryError, match="max_repair_attempts"):
        request_normalization.repair_attempts({"max_repair_attempts": value})


def test_explicit_repair_limit_and_default():
    assert request_normalization.repair_attempts({}) == 3
    assert request_normalization.repair_attempts({"max_repair_attempts": 0}) == 0
    assert request_normalization.repair_attempts({"max_repair_attempts": 5}) == 5


def test_workflow_budget_is_configurable_but_host_still_wins():
    assert runtime_budget.workflow_deadline(None, 100, {"workflow_timeout_seconds": 1200}) == 1300
    assert (
        runtime_budget.workflow_deadline(
            SimpleNamespace(execution_deadline=700), 100, {"workflow_timeout_seconds": 1200}
        )
        == 690
    )
    assert runtime_budget.workflow_deadline(None, 100) == 1890


@pytest.mark.parametrize("value", [True, -1, 0, float("inf"), float("nan"), 1801, "900"])
def test_invalid_workflow_budget(value):
    with pytest.raises(model_runtime.ModelBoundaryError, match="workflow_timeout_seconds"):
        runtime_budget.workflow_deadline(None, 100, {"workflow_timeout_seconds": value})


def test_visual_revision_uses_available_time_without_shorter_default_cap():
    deadline = runtime_budget.visual_loop_deadline(None, 100)
    assert deadline == 1890
    assert request_normalization.authoring_transport_options({})[0] == 900


@pytest.mark.parametrize("action", ["draft", "estimate", "revise"])
def test_invalid_budget_is_rejected_at_public_boundary(action):
    data = {
        "action": action,
        "source_text": "paper",
        "input": "poster",
        "source_html_uri": "poster.html",
        "feedback": "fix layout",
        "max_repair_attempts": -1,
    }
    _, error = request_normalization.validate_action_boundary(data)
    assert error is not None
    assert "max_repair_attempts" in str(error)


def test_shared_deadline_caps_each_model_call(monkeypatch):
    monkeypatch.setattr(model_runtime, "_monotonic", lambda: 100)
    assert model_runtime._remaining_timeout(900, deadline=180, boundary_label="HTML") == 80
    with pytest.raises(model_runtime.ModelBoundaryError):
        model_runtime._remaining_timeout(900, deadline=99, boundary_label="HTML")


def test_skill_manifest_supports_the_default_envelope():
    import yaml

    manifest = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text()
    metadata = yaml.safe_load(manifest.split("---", 2)[1])["metadata"]["helixforge"]
    assert metadata["execution"]["max_seconds"] == 1800
    schema = metadata["input_schema"]["properties"]
    assert schema["max_repair_attempts"]["default"] == 3
    assert schema["workflow_timeout_seconds"]["default"] == 1790


@pytest.mark.parametrize("remaining, expected", [(500, 590), (0, 100), (-10, 100)])
def test_live_host_clock_takes_precedence_over_stale_deadline(remaining, expected):
    ctx = SimpleNamespace(
        execution_clock=SimpleNamespace(remaining=lambda: remaining), execution_deadline=50
    )
    assert runtime_budget.workflow_deadline(ctx, 100) == expected
