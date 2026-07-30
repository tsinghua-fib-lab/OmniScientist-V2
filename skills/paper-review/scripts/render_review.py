#!/usr/bin/env python3
"""Save final paper review Markdown files."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time


def slugify(text: str, fallback: str = "paper") -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower())
    if slug:
        return slug[:100]
    if text:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        return f"{fallback}-{digest}"
    return fallback


def save_review(
    markdown: str,
    title: str,
    venue: str,
    output_dir: str = "reviews",
    timestamp: str | None = None,
) -> str:
    """Save a review using Omni's venue-title-timestamp naming convention."""

    os.makedirs(output_dir, exist_ok=True)
    generated_at = timestamp or time.strftime("%Y%m%d-%H%M%S", time.localtime())
    filename = (
        f"omni-review-{slugify(venue, 'venue')}-"
        f"{slugify(title)}-{slugify(generated_at, 'timestamp')}.md"
    )
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown.rstrip() + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Save a review Markdown file.")
    parser.add_argument("--title", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", default="reviews")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        markdown = handle.read()
    print(save_review(markdown, args.title, args.venue, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
