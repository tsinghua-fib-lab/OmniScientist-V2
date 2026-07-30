"""Cross-platform subprocess-group lifecycle helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from contextlib import suppress
from typing import Any


def process_group_options() -> dict[str, Any]:
    """Options that give a spawned command an independently stoppable group."""
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))}
    return {}


async def stop_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 0.5,
) -> None:
    """Terminate a subprocess and its descendants, then always reap it."""
    if proc.returncode is not None:
        return
    signal_process_tree(proc, force=False)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=max(0.05, grace_seconds))
        return
    except TimeoutError:
        signal_process_tree(proc, force=True)
    with suppress(Exception):
        await proc.wait()


def signal_process_tree(proc: asyncio.subprocess.Process, *, force: bool = False) -> None:
    """Send a stop signal to a command group without waiting for completion."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        if os.name == "nt":
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            subprocess.run(  # noqa: S603,S607 - fixed Windows utility and numeric PID.
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                creationflags=flags,
            )
            return
        if force:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        return


__all__ = ["process_group_options", "signal_process_tree", "stop_process_tree"]
