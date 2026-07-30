"""Detect requests that point at the agent's own prior work.

Codex principle: *never ask the user to re-clarify something the agent can look
up.* When a request refers to earlier output ("regenerate the last figure",
"that report", "you gave me..."), the right move is to look it up with tools,
not to short-circuit to a clarifying question.

This is a deliberately small, bilingual marker set used only to *bias toward
looking* — it never suppresses a legitimate question and never routes work by
itself. Worst case it enables read/recall tools for a turn that did not strictly
need them, which is safe. It is intentionally separate from
``omni.runtime.taskref`` (which stays language-neutral because it rewrites
commands); here we only nudge intent, so a bounded phrase list is acceptable.

Non-ASCII (CJK) markers are written as ``\\uXXXX`` escapes so this runtime source
stays ASCII (the production source is English-only), while matching the real
characters at runtime.
"""

from __future__ import annotations

import re

# Chinese referential markers, escaped to keep the source ASCII. Glosses:
#   recently / just-now(1) / just-now(2) / last-time / previously(1) /
#   previously(2) / that-one / that-copy / that-image / that-article /
#   above / regenerate / generate-again / you-gave-me / you-earlier /
#   you-just / previous-(one) / just-generated
_CJK_MARKERS = (
    "\u6700\u8fd1",
    "\u521a\u624d",
    "\u521a\u521a",
    "\u4e0a\u6b21",
    "\u4e4b\u524d",
    "\u5148\u524d",
    "\u90a3\u4e2a",
    "\u90a3\u4efd",
    "\u90a3\u5f20",
    "\u90a3\u7bc7",
    "\u4e0a\u9762",
    "\u91cd\u65b0\u751f\u6210",
    "\u518d\u751f\u6210",
    "\u4f60\u7ed9\u6211",
    "\u4f60\u4e4b\u524d",
    "\u4f60\u521a",
    "\u4e4b\u524d\u7684",
    "\u521a\u751f\u6210",
)
# English markers, matched as whole words so they never fire inside a token.
_EN_MARKERS = (
    "again",
    "regenerate",
    "previous",
    "previously",
    "earlier",
    "last time",
    "the one you",
    "you generated",
    "you made",
    "you created",
    "you gave",
    "that figure",
    "that diagram",
    "that report",
    "that chart",
)
_EN_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(m) for m in _EN_MARKERS) + r")(?![a-z])",
    re.IGNORECASE,
)


def references_prior_work(text: str) -> bool:
    """Whether ``text`` refers to the agent's own earlier output.

    Used to prefer an act-and-look-up turn (tools enabled) over a history-blind
    clarifying question. Detection is best-effort and additive only.
    """
    if not text:
        return False
    if any(marker in text for marker in _CJK_MARKERS):
        return True
    return bool(_EN_RE.search(text))
