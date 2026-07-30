"""Unified scheduling contract + application service.

One canonical request (:mod:`omni.scheduling.contracts`) and one application
service (:mod:`omni.scheduling.service`) sit behind every schedule-creation
surface — the ``omni schedule`` CLI and the ``schedule_task`` agent tool — so
they cannot drift in argument names, trigger vocabulary, or time semantics. This
mirrors Codex's "one core, thin adapters" and its request/response-by-id
approval, made durable for Omni's cross-process IM + background-service model.
"""

from __future__ import annotations

from omni.scheduling.contracts import (
    GOAL_SKILL,
    TRIGGER_CRON,
    TRIGGER_INTERVAL,
    TRIGGER_ONCE,
    ScheduleActor,
    ScheduleCreateRequest,
    ScheduleCreateResult,
    ScheduleTrigger,
    cron_trigger,
    interval_trigger,
    once_trigger,
    to_cli_argv,
)
from omni.scheduling.service import ScheduleService

__all__ = [
    "GOAL_SKILL",
    "TRIGGER_CRON",
    "TRIGGER_INTERVAL",
    "TRIGGER_ONCE",
    "ScheduleActor",
    "ScheduleCreateRequest",
    "ScheduleCreateResult",
    "ScheduleService",
    "ScheduleTrigger",
    "cron_trigger",
    "interval_trigger",
    "once_trigger",
    "to_cli_argv",
]
