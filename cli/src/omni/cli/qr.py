"""Terminal QR rendering used by channel login.

Drawn with upper-half blocks (``▀``): the foreground colour paints the upper
module of a cell and the background colour the lower one, so a symbol needs
half as many lines as it has module rows. One cell per module horizontally
(instead of two) then keeps it visually square, because a terminal cell is
roughly twice as tall as it is wide.

Combined with the lowest error-correction level — the payload is displayed
pristine on screen, so extra redundancy only inflates the symbol — a WeChat
login URL drops from 90x45 cells to 39x20 and fits an 80x24 terminal.

Modules are painted in explicit black-on-white so the result is a real QR code
rather than a colour-inverted one on the dark terminal themes most users run.
Where colour is unavailable — piped output, ``NO_COLOR`` — the same geometry is
kept by picking one of ``▀▄█``/space per module pair instead.
"""

from __future__ import annotations

from rich.text import Text

from omni.cli.render import console, info, warn

_UPPER_HALF = "▀"
_LOWER_HALF = "▄"
_FULL = "█"
_BLOCKS = _UPPER_HALF + _LOWER_HALF + _FULL
_DARK = "black"
_LIGHT = "white"

_GLYPHS = {
    (True, True): _FULL,
    (True, False): _UPPER_HALF,
    (False, True): _LOWER_HALF,
    (False, False): " ",
}

Matrix = list[list[bool]]


def render_terminal_qr(payload: str) -> None:
    """Render a QR code in the terminal, falling back to the raw payload.

    Silent when the code cannot be drawn legibly (missing dependency, terminal
    too narrow, no block glyphs): every caller prints the underlying URL or
    pairing code next to the QR, which stays usable on its own.
    """
    matrix = qr_matrix(payload)
    if matrix is None:
        warn("The qrcode package is not installed, so the QR code cannot be rendered here.")
        info(payload)
        return
    width = len(matrix[0])
    if width > console.width:
        warn(
            f"This QR code needs {width} columns but the terminal is {console.width}; "
            "widen the window and retry, or open the link below instead."
        )
        return
    if not _can_render(_BLOCKS):
        warn("This terminal cannot draw block characters; use the link below instead.")
        return
    if _colour_available():
        _print_coloured(matrix)
    else:
        _print_monochrome(matrix)


def qr_matrix(payload: str) -> Matrix | None:
    """Module matrix for ``payload``, or ``None`` when ``qrcode`` is missing."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.get_matrix()


def _colour_available() -> bool:
    """Whether the console can paint distinct foreground and background colours.

    A half block encodes its two modules as foreground and background, so
    without colour every cell collapses into one solid glyph and the symbol
    becomes unscannable. ``NO_COLOR=1``, ``TERM=dumb`` and piped output all land
    here and must take the monochrome path instead.
    """
    return console.color_system is not None and not console.no_color


def _can_render(chars: str) -> bool:
    """Whether the active console can emit every character in ``chars``.

    Legacy Windows consoles are excluded outright: Rich downgrades styling and
    box drawing there, and a mangled symbol is worse than the printed URL.
    """
    if console.legacy_windows:
        return False
    try:
        chars.encode(console.encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _print_coloured(matrix: Matrix) -> None:
    """One ``▀`` per module pair, painted black on white."""
    for top, bottom in _module_pairs(matrix):
        line = Text(no_wrap=True)
        for dark_top, dark_bottom in zip(top, bottom, strict=True):
            fg = _DARK if dark_top else _LIGHT
            bg = _DARK if dark_bottom else _LIGHT
            line.append(_UPPER_HALF, style=f"{fg} on {bg}")
        console.print(line, highlight=False)


def _print_monochrome(matrix: Matrix) -> None:
    """Same geometry without colour, choosing a glyph per module pair.

    Piped output and ``NO_COLOR`` terminals land here. Dark modules take the
    terminal's own foreground, so the symbol is colour-inverted on a dark
    theme — scanners read that fine, and it keeps the compact size rather than
    doubling both dimensions to spell the modules out in full blocks.
    """
    for top, bottom in _module_pairs(matrix):
        line = Text(no_wrap=True)
        for pair in zip(top, bottom, strict=True):
            line.append(_GLYPHS[pair])
        console.print(line, highlight=False)


def _module_pairs(matrix: Matrix) -> list[tuple[list[bool], list[bool]]]:
    """Rows taken two at a time, padding an odd matrix with a light row."""
    light_row = [False] * len(matrix[0])
    return [
        (matrix[index], matrix[index + 1] if index + 1 < len(matrix) else light_row)
        for index in range(0, len(matrix), 2)
    ]
