"""Host-owned projection of tool results into the model-facing transcript.

A python-engine skill (research-ideation, research-pptx, …) returns a structured
result that is persisted in full on the task event. Re-sending that blob on every
later ReAct iteration is what turned a small research turn into a multi-million
token bill: the coordinator's prompt_tokens include the previous observation
each time. Codex keeps tool output in the transcript but shrinks it; Omni does
the same at ingest so the latest observation is already bounded, not only the
older ones that microcompact later trims.

Identifier lists (``source_ids`` and kin) are privileged: they stay complete in
the projection. When the compact JSON still overflows, Hermes-style spill writes
the full ID list where ``read_file`` can page it — under the project jail, not
``$OMNI_HOME/cache``. Codex does not open the whole home store for this; it
keeps the recoverable bytes inside the workspace. A remaining overflow uses
Codex head/tail truncation and names the original token count and line count
instead of silently cutting the prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omni.core.truncation import formatted_truncate_text

DEFAULT_OBSERVATION_MAX_CHARS = 8000
_LIST_KEEP = 8
_STR_KEEP = 480
_DICT_KEEP = 28
_ID_LIST_KEYS = frozenset({"source_ids", "claim_ids", "evidence_ids"})

# Keys a coordinator actually needs to continue after a skill returns. Everything
# else (full paper lists, slide bodies, raw abstracts) stays on the event.
_PREFERRED_KEYS = (
    "status",
    "skill_name",
    "mode",
    "summary",
    "message",
    "error",
    "warning",
    "queries",
    "query",
    "n_retrieved",
    "n_kept",
    "observation",
    "paper_count",
    "count",
    "source_ids",
    "source_ids_count",
    "source_ids_spill",
    "report_uri",
    "artifact_uri",
    "artifacts",
    "outcome",
    "usage",
    "partial_outputs",
    "result",
    "planned_skill_name",
    "capability_resolution",
    "subtask_id",
    "task_id",
    "object_kind",
    "object_id",
)


def compact_observation(
    value: Any,
    *,
    max_chars: int = DEFAULT_OBSERVATION_MAX_CHARS,
    spill_dir: str | Path | None = None,
) -> str:
    """Return a model-facing string no larger than ``max_chars``.

    ``max_chars <= 0`` disables the cap and dumps the value as JSON (or as-is
    for strings), matching the historic unbounded observation.
    """
    if isinstance(value, str):
        text = value
        projected: Any = None
    else:
        projected = _project(value)
        text = json.dumps(projected, ensure_ascii=False, default=str)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    spilled = _spill_id_lists(value, projected, spill_dir)
    footer = ""
    if spilled is not None:
        text = json.dumps(spilled, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return text
        path = spilled.get("source_ids_spill")
        if path:
            footer = f"\n\nFull source_ids saved to: {path}"
    return formatted_truncate_text(text, max_chars, footer=footer)


def _project(value: Any, *, depth: int = 0, key: str = "") -> Any:
    if isinstance(value, str):
        if len(value) <= _STR_KEEP:
            return value
        return value[:_STR_KEEP].rstrip() + "…"
    if isinstance(value, list) and key in _ID_LIST_KEYS:
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, list):
        items = [_project(item, depth=depth + 1) for item in value[:_LIST_KEEP]]
        omitted = len(value) - _LIST_KEEP
        if omitted > 0:
            items.append(f"… {omitted} more")
        return items
    if isinstance(value, dict):
        keys = _ordered_keys(value)
        return {item: _project(value[item], depth=depth + 1, key=item) for item in keys}
    return value


def _ordered_keys(payload: dict[str, Any]) -> list[str]:
    preferred = [key for key in _PREFERRED_KEYS if key in payload]
    extras = [key for key in payload if key not in preferred]
    ordered = preferred + extras
    if len(ordered) <= _DICT_KEEP:
        return ordered
    return ordered[:_DICT_KEEP]


def _collect_ids(value: Any, *, key: str = "") -> list[str]:
    if isinstance(value, list) and key in _ID_LIST_KEYS:
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        found: list[str] = []
        for item_key, item in value.items():
            found.extend(_collect_ids(item, key=str(item_key)))
        return found
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_collect_ids(item))
        return found
    return []


def _spill_id_lists(
    original: Any,
    projected: Any,
    spill_dir: str | Path | None,
) -> dict[str, Any] | None:
    if spill_dir is None or not isinstance(projected, dict):
        return None
    ids = list(dict.fromkeys(_collect_ids(original)))
    if len(ids) <= _LIST_KEEP:
        return None
    dest = Path(spill_dir)
    dest.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:12]
    path = dest / f"source_ids-{digest}.txt"
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    spilled = dict(projected)
    preview = ids[:_LIST_KEEP]
    omitted = len(ids) - len(preview)
    if omitted > 0:
        preview = [*preview, f"… {omitted} more"]
    spilled["source_ids"] = preview
    spilled["source_ids_count"] = len(ids)
    spilled["source_ids_spill"] = str(path)
    return spilled


def observation_spill_path(paths: Any) -> Path:
    """Directory for host-written ID lists the model may page with ``read_file``.

    Hermes mounts ``$HERMES_HOME/cache/spillover`` into the sandbox so the
    pointer is readable. Omni already admits ``project_dir`` as a read root and
    refuses to add ``$OMNI_HOME`` as a write root, so new spills live here.
    """
    return Path(paths.project_dir) / "cache" / "spillover"


def observation_spill_roots(paths: Any) -> list[Path]:
    """Read-only roots that may contain current or leftover spill files.

    New writes use :func:`observation_spill_path`. The legacy
    ``$OMNI_HOME/cache/spillover`` path is still admitted so a model handed that
    pointer (task ``ef3b6546``) can read it instead of wandering the jail.
    """
    roots = [observation_spill_path(paths)]
    home = getattr(paths, "home", None)
    if home is not None:
        roots.append(Path(home) / "cache" / "spillover")
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in roots:
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if path not in seen:
            seen.add(path)
            resolved.append(path)
    return resolved


__all__ = [
    "DEFAULT_OBSERVATION_MAX_CHARS",
    "compact_observation",
    "observation_spill_path",
    "observation_spill_roots",
]
