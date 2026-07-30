"""Canonical time formatting + local-timezone context.

Persistence stays UTC in SQLite, and SQLite returns *naive* datetimes on read,
so every naive value is interpreted as UTC here. Anything shown to a human **or
to the model** is then converted to the process-local timezone with an explicit
offset, so the CLI, channels, task payloads, and the system prompt all agree on
the wall-clock time.

This mirrors Codex's ``local_time_context`` (``core/session/turn_context.rs``):
surface the local ``current_date`` plus an IANA ``timezone`` label so the model
reasons about time in the operator's zone instead of UTC. Codex reads the zone
via the ``iana_time_zone`` crate and falls back to ``Etc/UTC``; we do the
zero-dependency equivalent and always keep the numeric offset as a fallback.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_OFFSET_TZ = re.compile(r"([+-])(\d{2}):?(\d{2})")


def _parse_timezone(name: str | None) -> timezone | ZoneInfo | None:
    """Accept an IANA name, ``UTC+08:00``, or a labelled ``Asia/Shanghai (+08:00)``."""
    raw = str(name or "").strip()
    if not raw:
        return None
    token = raw.split()[0].strip("()")
    try:
        return ZoneInfo(token)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    match = _OFFSET_TZ.search(raw)
    if match is None:
        return None
    sign = 1 if match.group(1) == "+" else -1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta)


def ensure_aware(dt: datetime) -> datetime:
    """Interpret a naive DB timestamp as UTC (SQLite drops tzinfo on read)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def coerce_datetime(value: Any) -> datetime | None:
    """Best-effort parse of a datetime-ish value into an aware datetime.

    Accepts a ``datetime`` (naive → UTC) or an ISO-8601 string (``Z`` allowed).
    Returns ``None`` for empty/unparseable input so callers can show a default.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return ensure_aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def to_local(value: Any, tz: str | None = None) -> datetime | None:
    """Coerce then convert to ``tz`` or the process-local timezone."""
    dt = coerce_datetime(value)
    if dt is None:
        return None
    zone = _parse_timezone(tz)
    return dt.astimezone(zone) if zone is not None else dt.astimezone()


def format_local_time(
    value: Any,
    *,
    default: str = "-",
    with_tz: bool = True,
    tz: str | None = None,
) -> str:
    """Format a datetime-ish value in local time for humans."""
    dt = to_local(value, tz)
    if dt is None:
        return default
    fmt = "%Y-%m-%d %H:%M:%S %z" if with_tz else "%Y-%m-%d %H:%M:%S"
    return dt.strftime(fmt)


def format_local_iso(value: Any, *, default: str = "") -> str:
    """Return local ISO-8601 text (with offset) for machine-readable output.

    This is what task payloads hand the model, so a stored UTC ``created_at``
    reaches the model as e.g. ``2026-07-24T15:13:24+08:00`` instead of a bare,
    unlabelled UTC wall-clock time that renders 8 hours behind.
    """
    dt = to_local(value)
    return dt.isoformat(timespec="seconds") if dt is not None else default


def local_timezone_name() -> str:
    """Best-effort IANA name for the process-local zone, or ``""`` if unknown.

    Zero-dependency (the numeric offset is always available separately, so no
    caller depends on this succeeding): prefer a ``TZ`` env such as
    ``Asia/Shanghai``, else resolve the ``/etc/localtime`` symlink target under
    ``zoneinfo/``.
    """
    tz = os.environ.get("TZ", "").strip()
    if "/" in tz:
        return tz
    try:
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = os.readlink(link)
            marker = "zoneinfo/"
            if marker in target:
                return target.split(marker, 1)[-1]
    except OSError:
        pass
    return ""


@dataclass(frozen=True, slots=True)
class LocalTimeContext:
    """Local time facts injected into the model's session context."""

    now: datetime  # aware, process-local
    current_date: str  # YYYY-MM-DD (local)
    offset: str  # e.g. "+08:00" ("" only on platforms that omit %z)
    name: str  # IANA name, or "" when undetectable
    timezone: str  # human label: "Asia/Shanghai (+08:00)" or "+08:00"


def local_time_context(now: datetime | None = None) -> LocalTimeContext:
    """Local date + timezone label for the model, à la Codex ``local_time_context``.

    ``now`` defaults to the wall clock; a naive ``now`` is interpreted as local
    (Python's ``astimezone`` convention), an aware one is converted to local.
    """
    base = now if now is not None else datetime.now(UTC)
    local = base.astimezone()
    raw = local.strftime("%z")  # +0800 / -0500 / "" on exotic platforms
    offset = f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw
    name = local_timezone_name()
    label = f"{name} ({offset})" if name and offset else (name or offset or "UTC")
    return LocalTimeContext(
        now=local,
        current_date=local.strftime("%Y-%m-%d"),
        offset=offset,
        name=name,
        timezone=label,
    )
