"""What a ReAct iteration pays before any transcript exists.

A ReAct loop re-sends the system prompt *and* the provider ``tools`` array on
every iteration, so anything duplicated between them is paid twice per
iteration for the whole run. These tests pin the two rules that keep that fixed
cost honest:

* the prompt names the turn's tools but does not restate their schemas — the
  ``tools`` array is the single authority for descriptions and parameters;
* documentation that the model genuinely needs while filling a call lives on
  the parameter it belongs to, said once, not restated at tool level.

Assertions here are structural (substring / relative length), never token
counts, so they hold whichever token estimator is active.
"""

from __future__ import annotations

import json

from omni.agent.schedule_tools import _SCHEDULE_TASK_SPEC
from omni.core.react_agent import ToolSpec
from omni.core.system_prompt import build_system_prompt, render_tool_catalog

_LONG_DESC = (
    "Search the installed skill catalog by metadata and return names, "
    "descriptions, and usage guidance for every match."
)
_TOOLS = [
    ToolSpec("find_skill", _LONG_DESC, {"type": "object", "properties": {}}),
    ToolSpec("read_file", "Read a UTF-8 text file from the working directory.", {}),
    ToolSpec("bash", "Run a shell command.", {}),
]


def test_tool_catalog_names_every_tool():
    block = render_tool_catalog(_TOOLS)
    assert block.startswith("[Available tools]")
    for tool in _TOOLS:
        assert tool.name in block


def test_tool_catalog_does_not_restate_the_schemas_the_tools_array_carries():
    """The prompt's roster must not be a second copy of the tools array.

    Fails before the names-only catalog: the old renderer emitted each name
    followed by 160 characters of its description, duplicating text the provider
    already receives in ``tools``.
    """
    block = render_tool_catalog(_TOOLS)
    for tool in _TOOLS:
        assert tool.description not in block
    # No per-tool prose at all: the block costs the names, and nothing else.
    assert len(block) <= len("[Available tools]\n") + sum(
        len(t.name) + 2 for t in _TOOLS
    )


def test_system_prompt_does_not_duplicate_any_tool_description():
    prompt = build_system_prompt(role="R", tools=_TOOLS, project_name="p")
    assert _LONG_DESC not in prompt
    # The roster survives, because the tool-use rules refer to it.
    assert "find_skill" in prompt


def test_no_tool_description_is_long_enough_to_be_a_manual():
    """Tool descriptions state what a tool is for; parameters document themselves.

    Fails before the schedule tightening: ``schedule_task`` carried a second
    copy of the whole time-grounding contract at tool level (1,277 characters)
    on top of the parameter docs that state it where it is used.
    """
    for spec in (_SCHEDULE_TASK_SPEC,):
        assert len(spec.description) <= 800, spec.name


def test_schedule_time_grounding_survives_on_the_parameters_that_own_it():
    """Tightening moved the AM/PM contract; it must not have dropped it.

    The rule that an ambiguous hour is left unresolved for confirmation is what
    stops the agent inventing a 24-hour time, so it has to remain somewhere the
    model reads while filling the call.
    """
    props = _SCHEDULE_TASK_SPEC.parameters["properties"]
    when = json.dumps(props["when"], ensure_ascii=False)
    assert "day_period" in when
    assert "24-hour" in when
    # And the machine-value triggers still say when *not* to use them.
    assert "when" in props["at"]["description"]
