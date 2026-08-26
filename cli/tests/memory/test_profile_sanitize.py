from omni.memory.compiler import CompiledMemoryContext, _render
from omni.memory.profile_sanitize import is_tool_capability_ban, strip_tool_capability_bans
from omni.memory.service import ScoredMemory
from omni.storage.models import MemoryEntryORM


def test_strip_tool_capability_bans_keeps_real_preferences() -> None:
    raw = (
        "- 用户偏好中文回复；回复简洁。\n"
        "- 禁止使用 write_file、run_skill、spawn_subagents。\n"
        "- 本项目默认 Vancouver 引用。\n"
        "- never use bash for installs\n"
    )
    cleaned = strip_tool_capability_bans(raw)
    assert "write_file" not in cleaned
    assert "spawn_subagents" not in cleaned
    assert "never use bash" not in cleaned
    assert "中文回复" in cleaned
    assert "Vancouver" in cleaned
    assert is_tool_capability_ban("禁止使用 write_file、run_skill")
    assert not is_tool_capability_ban("用户偏好中文回复")


def test_compiler_drops_tool_ban_lines() -> None:
    entries = [
        ScoredMemory(
            entry=MemoryEntryORM(
                layer="M4",
                memory_type="user_profile",
                scope="user",
                summary="禁止使用 write_file、run_skill、spawn_subagents。",
            ),
            score=1.0,
        ),
        ScoredMemory(
            entry=MemoryEntryORM(
                layer="M4",
                memory_type="preference",
                scope="user",
                summary="用户偏好中文回复",
            ),
            score=0.9,
        ),
    ]
    text = _render(entries, role="planner", budget_chars=800)
    assert isinstance(text, str)
    assert "write_file" not in text
    assert "中文回复" in text
    assert isinstance(CompiledMemoryContext(), CompiledMemoryContext)
