"""Durable local task runtime for long-running research skills."""

from typing import TYPE_CHECKING, Any

from omni.runtime.notifications import (
    CompositeNotifier,
    InboxNotifier,
    Notifier,
    TaskNotification,
)

if TYPE_CHECKING:
    from omni.runtime.subtask_runtime import SubtaskRuntime


def __getattr__(name: str) -> Any:
    """Keep the public SubtaskRuntime export without eager circular imports."""
    if name == "SubtaskRuntime":
        from omni.runtime.subtask_runtime import SubtaskRuntime

        return SubtaskRuntime
    raise AttributeError(name)


__all__ = ["SubtaskRuntime", "Notifier", "InboxNotifier", "CompositeNotifier", "TaskNotification"]
