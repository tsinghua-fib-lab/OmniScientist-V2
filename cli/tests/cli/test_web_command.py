"""Focused CLI contracts for ``omni web`` startup guidance."""

from __future__ import annotations

from omni.cli.commands.web_cmd import _web_ui_mismatch_warning


def test_web_ui_mismatch_warning_offers_reliable_reinstall_paths() -> None:
    message = _web_ui_mismatch_warning(ui_version="standard", package_version="2.0.0")

    assert "Web UI standard does not match OmniScientist 2.0.0" in message
    assert "`omni update` to install a newer published release" in message
    assert "`omni update --force` only when that release is known good" in message
    assert "From a source checkout, use `omni update --local`" in message
    assert "If either update path is unavailable or fails" in message
    assert "`./cli/scripts/install.sh --local`" in message
    assert (
        "`powershell -ExecutionPolicy Bypass -File "
        ".\\cli\\scripts\\install.ps1 -Local`"
    ) in message
