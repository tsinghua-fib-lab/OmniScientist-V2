"""Composer-side ``[Image #N]`` placeholders for a pasted clipboard image.

Codex renders these as structured textarea elements. Omni's dock is a plain
buffer, so the placeholder is literal text. On submit we inject an ``@path``
mention for any placeholder that is still present — the same contract the
paperclip and ``@draft.png`` already use — and drop attachments whose
placeholder the user deleted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from omni.cli.clipboard_paste import (
    PasteImageError,
    failed_paste_message,
    paste_image_to_temp_png,
)
from omni.core.file_mentions import format_mention, parse_mentions
from omni.core.user_inputs import next_image_placeholder


def bind_composer_images(
    text: str,
    attachments: Sequence[tuple[str, Path | str]],
    *,
    cwd: Path | None = None,
) -> str:
    """Keep ``[Image #N]`` and append ``@path`` for still-present images."""
    mentioned = {str(mention.path) for mention in parse_mentions(text, cwd=cwd) if mention.exists}
    extras: list[str] = []
    for placeholder, raw_path in attachments:
        if placeholder not in text:
            continue
        path = Path(raw_path)
        uri = str(path)
        if uri in mentioned or format_mention(path) in text:
            continue
        extras.append(format_mention(path))
        mentioned.add(uri)
    if not extras:
        return text
    body = text.rstrip()
    block = "\n".join(extras)
    return f"{body}\n{block}" if body else block


@dataclass
class ComposerImages:
    """Pending clipboard images keyed by the placeholder still in the draft."""

    items: list[tuple[str, Path]] = field(default_factory=list)
    dest_dir: Path | None = None

    def attach(self, path: Path, text: str) -> str:
        """Record ``path`` and return the placeholder to insert at the cursor."""
        placeholder = next_image_placeholder(text)
        self.items.append((placeholder, path))
        return placeholder

    def expand(self, text: str, *, cwd: Path | None = None) -> str:
        """Rewrite a submitted draft so ``@`` mentions cover live images."""
        return bind_composer_images(text, self.items, cwd=cwd)

    def discard_missing(self, text: str) -> None:
        """Drop attachments whose placeholder is no longer in ``text``."""
        self.items = [item for item in self.items if item[0] in text]

    def clear(self) -> None:
        self.items.clear()

    def paste_into(self, buffer: object, *, on_error, on_status=None) -> bool:
        """Ctrl+V handler: clipboard → workspace ``inputs/`` → ``[Image #N]``.

        ``buffer`` is a prompt_toolkit ``Buffer`` (``.text`` / ``insert_text``).
        Errors use Codex's ``Failed to paste image: …`` wording.
        """
        try:
            path, info = paste_image_to_temp_png(self.dest_dir)
        except PasteImageError as exc:
            on_error(failed_paste_message(exc))
            return False
        placeholder = self.attach(path, str(getattr(buffer, "text", "")))
        insert = getattr(buffer, "insert_text", None)
        if callable(insert):
            insert(f"{placeholder} ")
        if on_status is not None:
            on_status(
                f"attached {placeholder} · {info.width}×{info.height} "
                f"{info.encoded_format.label()}"
            )
        return True
