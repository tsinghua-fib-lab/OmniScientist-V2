#!/usr/bin/env python3
"""Merge ordered Markdown review sections into one review artifact."""

from __future__ import annotations

import argparse
from pathlib import Path


def merge_sections(input_dir: Path, output: Path, *, discard_sections: bool = True) -> int:
    """Join the numbered sections into ``output`` and drop the scratch directory.

    The sections are a staging area — the review is written a piece at a time so
    no single generation has to hold it all — and once merged they are a
    duplicate of the artifact. Removing them here rather than asking the model to
    tidy up is what keeps a review run from leaving ``review-sections/`` behind
    in whatever directory the user launched from.
    """
    sections = sorted(
        path for path in input_dir.glob("*.md") if path.name != output.name
    )
    if not sections:
        raise ValueError(f"No Markdown sections found in {input_dir}")

    output.parent.mkdir(parents=True, exist_ok=True)
    content = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in sections)
    output.write_text(content.rstrip() + "\n", encoding="utf-8")
    if discard_sections and input_dir.resolve() not in output.resolve().parents:
        _discard_staging(input_dir, sections)
    return len(sections)


def _discard_staging(input_dir: Path, sections: list[Path]) -> None:
    """Remove the staging directory, and only ever that.

    ``--input-dir`` is whatever the model typed into a shell call, so this must
    delete on evidence rather than on trust: a recursive delete of that argument
    would take the launch directory with it the first time the model abbreviated
    it to ``.``. A directory holding anything this run did not just merge is not
    a staging directory, and nothing in it is ours to remove.
    """
    if any(path not in set(sections) for path in input_dir.iterdir()):
        return
    for path in sections:
        path.unlink()
    input_dir.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--keep-sections",
        action="store_true",
        help="Leave the staging directory in place instead of discarding it.",
    )
    args = parser.parse_args()
    count = merge_sections(args.input_dir, args.output, discard_sections=not args.keep_sections)
    print(f"Merged {count} Markdown sections into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
