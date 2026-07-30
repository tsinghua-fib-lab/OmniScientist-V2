"""Human-facing time formatting for CLI and channel task views.

Thin re-export of the canonical helpers in :mod:`omni.core.timefmt` so existing
``from omni.cli.timefmt import ...`` call sites keep working while there is a
single source of truth for UTC→local conversion (persistence stays UTC; display
converts to the process-local timezone).
"""

from __future__ import annotations

from omni.core.timefmt import (
    coerce_datetime as _coerce_datetime,
)
from omni.core.timefmt import (
    format_local_iso,
    format_local_time,
)

__all__ = ["_coerce_datetime", "format_local_iso", "format_local_time"]
