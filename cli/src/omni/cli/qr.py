"""Terminal QR rendering used by channel login."""

from __future__ import annotations

from omni.cli.render import console, info, warn


def render_terminal_qr(payload: str) -> None:
    """Render a QR code in the terminal, falling back to the raw payload."""
    try:
        import qrcode
    except ImportError:
        warn("The qrcode package is not installed, so the QR code cannot be rendered here.")
        info(payload)
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    for row in qr.get_matrix():
        console.print("".join("██" if cell else "  " for cell in row), highlight=False)
