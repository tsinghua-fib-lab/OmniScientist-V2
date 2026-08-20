"""`omni web` — loopback SPA over the same cwd-keyed stores as the CLI."""

from __future__ import annotations

import sys

import typer

from omni.cli.render import error, warn
from omni.web.bind import DEFAULT_HOST, DEFAULT_PORT, ready_url, validate_bind_host
from omni.web.static import (
    WebUiMissing,
    ensure_web_ui,
    package_version,
    spa_version,
    web_dist_dir,
)

app_help = "Serve the local Omni web UI on loopback (default 127.0.0.1:1088)."


def _web_ui_mismatch_warning(*, ui_version: str, package_version: str) -> str:
    """Explain recovery without pretending an up-to-date check reinstalls assets."""
    return (
        f"Web UI {ui_version} does not match OmniScientist {package_version}. "
        "Reinstall the package and UI together, then restart omni web. "
        "For a published install, use `omni update` to install a newer published release; "
        "use `omni update --force` only when that release is known good and local assets "
        "are damaged. From a source checkout, use `omni update --local`. If either update "
        "path is unavailable or fails, run the repository installer: "
        "`./cli/scripts/install.sh --local` on macOS/Linux or "
        "`powershell -ExecutionPolicy Bypass -File .\\cli\\scripts\\install.ps1 -Local` "
        "on Windows."
    )


def web_command(
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Bind address (loopback only)."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="TCP port."),
) -> None:
    """Host the web surface. Directory identity is chosen in the UI, not here."""
    try:
        host = validate_bind_host(host)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc

    try:
        import uvicorn
    except ImportError as exc:
        error("omni web requires the [web] extra: pip install 'OmniScientist-V2[web]'")
        raise typer.Exit(2) from exc

    try:
        from omni.web.app import create_app
    except ImportError as exc:
        error("omni web requires the [web] extra: pip install 'OmniScientist-V2[web]'")
        raise typer.Exit(2) from exc

    try:
        ensure_web_ui()
    except WebUiMissing as exc:
        error(str(exc).rstrip())
        raise typer.Exit(2) from exc

    app = create_app(trusted_hosts=(host,))
    url = ready_url(host, port)

    class _ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):  # noqa: ANN001
            await super().startup(sockets=sockets)
            # One plain line: Rich info() after uvicorn startup emits raw ESC
            # sequences that show up as ``^[`` in some terminals.
            ui = spa_version(web_dist_dir()) or "unversioned"
            sys.stdout.write(f"omni web: {url}  UI {ui}\n")
            sys.stdout.flush()
            pkg = package_version()
            if ui not in {"", "unversioned"} and pkg and ui != pkg:
                warn(_web_ui_mismatch_warning(ui_version=ui, package_version=pkg))

    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        log_level="warning",
    )
    server = _ReadyServer(config)
    server.run()
