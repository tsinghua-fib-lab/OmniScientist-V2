"""Hard tool-call accounting independent from transcript closure."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolExecutionBudget:
    limit: int | None
    requested: int = 0
    admitted: int = 0
    completed: int = 0
    rejected: int = 0
    parent: ToolExecutionBudget | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.limit is not None:
            self.limit = max(0, int(self.limit))

    def admit(self, requested: int) -> int:
        count = max(0, int(requested))
        self.requested += count
        capacity = count if self.limit is None else max(0, self.limit - self.admitted)
        admitted = min(count, capacity)
        if self.parent is not None and admitted:
            admitted = self.parent.admit(admitted)
        self.admitted += admitted
        self.rejected += count - admitted
        return admitted

    def mark_completed(self, count: int = 1) -> None:
        before = self.completed
        self.completed = min(self.admitted, self.completed + max(0, int(count)))
        delta = self.completed - before
        if self.parent is not None and delta:
            self.parent.mark_completed(delta)

    def reject(self, requested: int) -> None:
        count = max(0, int(requested))
        self.requested += count
        self.rejected += count

    @property
    def remaining(self) -> int | None:
        local = None if self.limit is None else max(0, self.limit - self.admitted)
        parent = self.parent.remaining if self.parent is not None else None
        if local is None:
            return parent
        if parent is None:
            return local
        return min(local, parent)

    @property
    def enforced(self) -> bool:
        return self.limit is not None or bool(
            self.parent is not None and self.parent.enforced
        )

    @property
    def exhausted(self) -> bool:
        return self.rejected > 0 or (self.enforced and self.remaining == 0)

    def snapshot(self) -> dict[str, int | bool | None]:
        return {
            "limit": self.limit,
            "requested": self.requested,
            "admitted": self.admitted,
            "completed": self.completed,
            "rejected": self.rejected,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "enforced": self.enforced,
        }


__all__ = ["ToolExecutionBudget"]
