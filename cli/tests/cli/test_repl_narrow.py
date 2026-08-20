"""Narrow-terminal chrome: footer, placeholder, meta, and approval modal.

Codex keeps instructional chrome on one row and tests 20/40-column terminals
so a silent clip cannot land again. These lock the same contract for Omni's
inline dock without starting a full Application.
"""

from __future__ import annotations

import asyncio

import pytest

from omni.cli.repl_layout import (
    COMPOSER_PLACEHOLDER,
    COMPOSER_PLACEHOLDER_MEDIUM,
    COMPOSER_PLACEHOLDER_NARROW,
    COMPOSER_PLACEHOLDER_SHIFT,
    center_truncate_path,
    clip_display,
    display_width,
    fit_hint_parts,
    newline_hint,
    placeholder_for_width,
)
from omni.cli.repl_tui import ApprovalOption, ReplTui


def _set_columns(tui: ReplTui, columns: int, rows: int = 24) -> None:
    tui.terminal_size = lambda: (rows, columns)  # type: ignore[method-assign]


def _visible(tui: ReplTui) -> str:
    return "".join(text for _style, text in tui.footer_fragments())


def test_clip_display_uses_display_width_and_marks_the_cut():
    assert clip_display("hello", 10) == "hello"
    assert clip_display("hello world", 8) == "hello w…"
    assert display_width(clip_display("工程架构图", 5)) == 5
    assert clip_display("工程架构图", 5).endswith("…")


def test_fit_hint_parts_drops_whole_tokens_before_clipping_the_label():
    parts = [
        "auto mode",
        "Enter send",
        "Ctrl+J newline",
        "select to copy",
        "Ctrl+D exit",
    ]
    drop = ("select to copy", "Ctrl+J newline", "Ctrl+D exit", "Enter send")

    wide = fit_hint_parts(parts, 80, drop_order=drop)
    assert wide == parts

    mid = fit_hint_parts(parts, 40, drop_order=drop)
    assert "Enter send" in mid
    assert "select to copy" not in mid
    assert display_width(" · ".join(mid)) <= 40

    narrow = fit_hint_parts(parts, 20, drop_order=drop)
    assert narrow == ["auto mode"]
    assert display_width(" · ".join(narrow)) <= 20


def test_fit_hint_parts_clips_status_so_interrupt_keys_remain():
    parts = [
        "Paper text ready; starting full-manuscript understanding · 0s",
        "Enter steer",
        "Tab queue",
        "Esc stop",
        "Ctrl+D exit",
    ]
    fitted = fit_hint_parts(parts, 40, drop_order=("Tab queue", "Enter steer"))
    text = " · ".join(fitted)
    assert "Esc stop" in text
    assert "Ctrl+D exit" in text
    assert "…" in fitted[0]
    assert display_width(text) <= 40
    assert "Enter st" not in text  # never split a key


def test_center_truncate_path_keeps_the_basename():
    path = "/Users/antonio/work/omniscientist_v2/very/long/research/workspace"
    out = center_truncate_path(path, 28)
    assert out.endswith("workspace")
    assert "…" in out
    assert display_width(out) <= 28
    assert center_truncate_path("RAG 工程架构图", 8).endswith("…")


def test_placeholder_for_width_compacts_instead_of_overflowing():
    assert placeholder_for_width(80) == COMPOSER_PLACEHOLDER
    assert placeholder_for_width(80, shift_enter_ready=True) == COMPOSER_PLACEHOLDER_SHIFT
    assert placeholder_for_width(40) == COMPOSER_PLACEHOLDER_MEDIUM
    assert placeholder_for_width(20) == COMPOSER_PLACEHOLDER_NARROW
    assert display_width(placeholder_for_width(40)) <= 40
    assert display_width(placeholder_for_width(20)) <= 20
    assert newline_hint(False) == "Ctrl+J newline"
    assert newline_hint(True) == "Shift+Enter newline"


@pytest.mark.parametrize("columns", (20, 40, 60, 80))
def test_idle_footer_never_exceeds_the_terminal(columns: int):
    tui = ReplTui(commands=())
    _set_columns(tui, columns)
    text = tui.footer_text(width=columns)
    assert display_width(text) <= columns
    visible = _visible(tui).strip()
    assert display_width(visible) <= columns
    if columns >= 40:
        assert "Enter" in text


@pytest.mark.parametrize("columns", (20, 40, 60, 80))
def test_busy_footer_keeps_esc_and_fits(columns: int):
    tui = ReplTui(commands=())
    _set_columns(tui, columns)
    tui.set_busy(True)
    tui.set_status("Paper text ready; starting full-manuscript understanding")
    text = tui.footer_text(width=max(0, columns - 3))
    assert display_width(text) <= max(0, columns - 3)
    assert "Esc" in text
    visible = _visible(tui)
    assert display_width(visible) <= columns
    assert tui.footer_text()  # unfitted form still lists every key
    assert "Enter steer" in tui.footer_text()
    assert "Tab queue" in tui.footer_text()
    assert "Esc stop" in tui.footer_text()


def test_unfitted_footer_text_stays_complete_for_existing_callers():
    tui = ReplTui(commands=())
    assert tui.footer_text() == (
        "auto mode · Enter send · Ctrl+J newline · select to copy · Ctrl+D exit"
    )
    ready = ReplTui(commands=(), shift_enter_ready=True)
    assert ready.footer_text() == (
        "auto mode · Enter send · Shift+Enter newline · select to copy · Ctrl+D exit"
    )


def test_placeholder_tracks_terminal_width():
    tui = ReplTui(commands=())
    _set_columns(tui, 80)
    assert tui._placeholder_text() == COMPOSER_PLACEHOLDER
    ready = ReplTui(commands=(), shift_enter_ready=True)
    _set_columns(ready, 80)
    assert ready._placeholder_text() == COMPOSER_PLACEHOLDER_SHIFT
    _set_columns(tui, 40)
    assert tui._placeholder_text() == COMPOSER_PLACEHOLDER_MEDIUM
    _set_columns(tui, 20)
    assert tui._placeholder_text() == COMPOSER_PLACEHOLDER_NARROW


def test_meta_drops_long_paths_then_marks_the_cut():
    tui = ReplTui(commands=())
    tui.update_status(
        model="openai/deepseek-v4-pro",
        focus="/Users/antonio/work/omniscientist_v2/very/long/research/workspace",
        context_tokens=12_400,
        context_window=32_768,
    )
    tui.set_last_elapsed(3.25)

    _set_columns(tui, 80)
    meta80 = tui.meta_text()
    assert display_width(meta80) <= 80
    assert "ctx 12.4k/32.8k" in meta80
    assert "last 3.2s" in meta80

    _set_columns(tui, 40)
    meta40 = tui.meta_text()
    assert display_width(meta40) <= 40
    assert "workspace" in meta40 or "ctx" in meta40 or "last" in meta40

    _set_columns(tui, 20)
    assert display_width(tui.meta_text()) <= 20


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", (20, 24, 32, 40, 80))
async def test_modal_box_fits_and_hint_is_not_silently_sliced(columns: int):
    tui = ReplTui(commands=())
    _set_columns(tui, columns)
    task = asyncio.create_task(
        tui.request_approval(
            "Approval required",
            "Allow task `abc12345` to write and run sandboxed commands.",
            options=(
                ApprovalOption("approve", "Approve once"),
                ApprovalOption("deny", "Deny"),
            ),
        )
    )
    await asyncio.sleep(0)
    try:
        inner = tui._modal_width()
        box = inner + 4
        assert box <= columns
        body = "".join(text for _style, text in tui._modal_fragments())
        compact = " ".join(body.split())
        assert "Approve" in compact and "once" in compact
        assert "Deny" in compact
        # Hint is wrapped or complete — never a mid-word stump like "enter confir".
        assert "enter confir" not in compact or "enter confirm" in compact
        if columns >= 40:
            assert "esc deny" in compact or "deny" in compact.lower()
        for line in body.splitlines():
            assert display_width(line) <= columns
    finally:
        tui._resolve_modal("deny")
        await asyncio.wait_for(task, timeout=1)
