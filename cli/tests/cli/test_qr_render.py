"""Terminal QR rendering contracts.

The WeChat login QR is the first thing a new user sees, and it has to stay
scannable on every terminal we ship to: a colour terminal gets the compact
half-block symbol, everything else degrades to something still readable rather
than to a solid block of glyphs.
"""

from __future__ import annotations

import io
import re
import sys

import pytest
from rich.console import Console

from omni.cli import qr, render

# A representative WeChat ClawBot login URL (89 bytes), which is what sets the
# symbol's version and therefore its size on screen.
WECHAT_URL = (
    "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=c85e219d21c55a88ddb6c5d6cf89e874&bot_type=3"
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _AsciiOut(io.StringIO):
    encoding = "ascii"


def _render(monkeypatch: pytest.MonkeyPatch, payload: str = WECHAT_URL, **console_kwargs):
    """Render ``payload`` through a controlled console; return (raw, plain lines).

    ``height`` is pinned alongside ``width`` because Rich only honours an
    explicit size when both are given, and these tests turn on exact widths.
    """
    buf = console_kwargs.pop("file", None) or io.StringIO()
    # Which console generation is in play is part of what these tests control.
    # Rich infers legacy conhost from the host OS, so on a Windows runner every
    # modern-terminal test below silently became the legacy one and asserted a
    # symbol the renderer is right to refuse there. The legacy test says so.
    console_kwargs.setdefault("legacy_windows", False)
    console = Console(file=buf, no_color=False, height=60, **console_kwargs)
    monkeypatch.setattr(qr, "console", console)
    # ``warn``/``info`` resolve render's own console, so route it here too and
    # keep every line the user would see in one buffer.
    monkeypatch.setattr(render, "console", console)
    qr.render_terminal_qr(payload)
    raw = buf.getvalue()
    return raw, _ANSI.sub("", raw).rstrip("\n").split("\n")


def test_colour_terminal_draws_a_compact_half_block_symbol(monkeypatch):
    modules = len(qr.qr_matrix(WECHAT_URL))
    raw, lines = _render(monkeypatch, force_terminal=True, color_system="truecolor", width=100)

    # One cell per module across, two module rows per line down: the symbol is
    # visually square because a terminal cell is about twice as tall as wide.
    assert len(lines) == (modules + 1) // 2
    assert {len(line) for line in lines} == {modules}
    # Explicit black-on-white, so a dark terminal theme does not invert the code.
    assert "\x1b[30;47m" in raw and "\x1b[37;40m" in raw


def test_the_symbol_fits_a_standard_eighty_by_twenty_four_terminal(monkeypatch):
    _, lines = _render(monkeypatch, force_terminal=True, color_system="truecolor", width=100)

    assert len(lines) <= 22 and len(lines[0]) <= 80


def test_without_colour_the_symbol_keeps_its_compact_size(monkeypatch):
    """A pairing QR streamed into the transcript arrives through a pipe.

    Rich reports no colour there, but the module pairs can still be spelled out
    with distinct glyphs, so the symbol must not double in both dimensions.
    """
    modules = len(qr.qr_matrix(WECHAT_URL))
    raw, lines = _render(monkeypatch, force_terminal=True, color_system=None, width=100)

    assert len(lines) == (modules + 1) // 2
    assert {len(line) for line in lines} == {modules}
    assert "\x1b[" not in raw
    # Every module pair needs its own glyph; a single one would be a solid slab.
    assert {"▀", "▄", "█", " "} >= set("".join(lines))
    assert {"▀", "▄", "█"} <= set("".join(lines))


def test_a_terminal_without_block_glyphs_defers_to_the_printed_link(monkeypatch):
    raw, _ = _render(monkeypatch, file=_AsciiOut(), force_terminal=True, width=100)

    assert "▀" not in raw and "█" not in raw
    assert "use the link below" in raw


def test_a_terminal_narrower_than_the_symbol_defers_to_the_printed_link(monkeypatch):
    raw, _ = _render(monkeypatch, force_terminal=True, color_system="truecolor", width=20)

    assert "▀" not in raw
    # The advice is wrapped to the very width it is complaining about, so the
    # phrase reaches the reader split across lines.
    assert "widen the window" in " ".join(_ANSI.sub("", raw).split())


def test_a_legacy_windows_console_defers_to_the_printed_link(monkeypatch):
    # Rich downgrades styling on legacy conhost, and a half block that loses its
    # background colour is a solid glyph — worse than showing no QR at all.
    raw, _ = _render(
        monkeypatch,
        force_terminal=True,
        color_system="truecolor",
        width=100,
        legacy_windows=True,
    )

    assert "▀" not in raw
    assert "use the link below" in raw


def test_qr_matrix_is_none_without_the_qrcode_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "qrcode", None)

    assert qr.qr_matrix(WECHAT_URL) is None


def test_missing_qrcode_dependency_prints_the_payload(monkeypatch):
    monkeypatch.setattr(qr, "qr_matrix", lambda payload: None)
    raw, _ = _render(monkeypatch, force_terminal=True, color_system="truecolor", width=100)

    assert WECHAT_URL in _ANSI.sub("", raw)
