"""Host-owned annotations on compute/shell failures.

Codex returns exec output and lets the model retry. Omni adds one generic
hint when a missing path still contains a literal ``$VAR`` that this process
already exported — no command rewrite, no deliverable-specific branches.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_MISSING_PATH = re.compile(
    r"(No such file or directory|FileNotFoundError|ENOENT)",
    re.IGNORECASE,
)
_ENV_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def unexpanded_env_hint(text: str, env: Mapping[str, str]) -> str:
    """Explain a missing path that still contains a live ``$VAR`` token."""
    blob = str(text or "")
    if not blob or not env or not _MISSING_PATH.search(blob):
        return ""
    names: list[str] = []
    for match in _ENV_TOKEN.finditer(blob):
        name = match.group(1)
        if name in env and name not in names:
            names.append(name)
    if not names:
        return ""
    resolved = ", ".join(f"{name}={env[name]}" for name in names)
    listed = ", ".join(f"${name}" for name in names)
    return (
        f"[unexpanded-env] path contains literal {listed}. "
        f"resolved: {resolved}. "
        "In Python/Node read the environment (os.environ['NAME'] or "
        "`from omni_io import output_path`). "
        'In bash use "$NAME". A single-quoted heredoc does not expand.'
    )


__all__ = ["unexpanded_env_hint"]
