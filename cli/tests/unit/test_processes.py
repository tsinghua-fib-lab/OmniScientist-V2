from __future__ import annotations

import asyncio

import pytest

from omni.runtime import processes


def test_process_group_options_are_platform_specific(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(processes.os, "name", "posix")
    assert processes.process_group_options() == {"start_new_session": True}

    monkeypatch.setattr(processes.os, "name", "nt")
    options = processes.process_group_options()
    assert set(options) == {"creationflags"}
    assert isinstance(options["creationflags"], int)


@pytest.mark.asyncio
async def test_stop_process_tree_escalates_and_reaps(monkeypatch) -> None:  # noqa: ANN001
    stopped = asyncio.Event()
    calls: list[bool] = []

    class Process:
        pid = 123
        returncode = None

        async def wait(self) -> int:
            await stopped.wait()
            self.returncode = -9
            return self.returncode

    proc = Process()

    def signal_process(_proc, *, force: bool = False) -> None:  # noqa: ANN001
        calls.append(force)
        if force:
            stopped.set()

    monkeypatch.setattr(processes, "signal_process_tree", signal_process)

    await processes.stop_process_tree(proc, grace_seconds=0.01)  # type: ignore[arg-type]

    assert calls == [False, True]
    assert proc.returncode == -9
