"""CLI channel — surfaces task completions to the terminal (daemon mode)."""

from __future__ import annotations

import asyncio

from omni.channels.base import Channel
from omni.runtime.notifications import TaskNotification


class CLIChannel(Channel):
    name = "cli"

    async def start(self) -> None:
        # The CLI channel has no inbound poll loop (that's the REPL); in the
        # daemon it simply idles so completion notifications can print.
        while True:
            await asyncio.sleep(3600)

    async def notify(self, note: TaskNotification) -> None:
        from omni.cli.render import console
        from omni.runtime.artifact_preview import inline_text_artifacts
        from omni.runtime.presentation import task_presentation_from_notification

        if note.channel != "cli":
            return
        presentation = inline_text_artifacts(
            task_presentation_from_notification(note),
            self.settings.paths.artifacts_dir,
            injection_mode=getattr(self.settings.security, "injection_defense", "flag"),
        )
        console.print("\n" + presentation.to_markdown())
        await self._record_task_delivery(note, status="sent")
