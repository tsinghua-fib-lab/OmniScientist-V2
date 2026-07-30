"""What a paper-review run is allowed to leave behind.

A review is written section by section so no single generation has to hold the
whole thing, and the sections are staged in a directory beside the output. That
staging directory used to survive the merge, so every review left a
``review-sections/`` folder in whatever directory the user launched omni from —
this repository included. These tests pin the cleanup and, more importantly, the
one case where cleaning up would destroy the artifact itself.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[3] / "skills" / "paper-review"


def _merge_module() -> Any:
    path = SKILL_ROOT / "scripts" / "merge_review_sections.py"
    spec = importlib.util.spec_from_file_location("paper_review_merge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_module() -> Any:
    path = SKILL_ROOT / "scripts" / "extract_pdf_text.py"
    spec = importlib.util.spec_from_file_location("paper_review_extract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pdf_runtime_module() -> Any:
    path = SKILL_ROOT / "scripts" / "pdf_runtime.py"
    spec = importlib.util.spec_from_file_location("paper_review_pdf_runtime_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _staged(tmp_path: Path) -> Path:
    sections = tmp_path / "review-sections"
    sections.mkdir()
    (sections / "01-summary.md").write_text("# Summary\n", encoding="utf-8")
    (sections / "02-strengths.md").write_text("# Strengths\n", encoding="utf-8")
    return sections


def test_the_staging_directory_does_not_outlive_the_merge(tmp_path: Path) -> None:
    module = _merge_module()
    sections = _staged(tmp_path)
    output = tmp_path / "reviews" / "paper-iclr-review.md"

    assert module.merge_sections(sections, output) == 2
    assert output.read_text(encoding="utf-8").startswith("# Summary")
    assert not sections.exists()


def test_a_caller_that_wants_the_sections_keeps_them(tmp_path: Path) -> None:
    """Inspecting a partial run is worth an opt-out, not a second merge."""
    module = _merge_module()
    sections = _staged(tmp_path)

    module.merge_sections(sections, tmp_path / "review.md", discard_sections=False)

    assert sorted(path.name for path in sections.glob("*.md")) == [
        "01-summary.md",
        "02-strengths.md",
    ]


def test_cleanup_never_deletes_the_artifact_it_just_wrote(tmp_path: Path) -> None:
    """An output inside the staging directory makes the two indistinguishable."""
    module = _merge_module()
    sections = _staged(tmp_path)
    output = sections / "review.md"

    module.merge_sections(sections, output)

    assert output.read_text(encoding="utf-8").startswith("# Summary")
    assert sections.exists()


def test_an_empty_staging_directory_is_a_failed_run_not_an_empty_review(
    tmp_path: Path,
) -> None:
    module = _merge_module()
    empty = tmp_path / "review-sections"
    empty.mkdir()

    with pytest.raises(ValueError):
        module.merge_sections(empty, tmp_path / "review.md")
    assert empty.exists()


def test_cleanup_leaves_a_directory_that_is_not_only_ours(tmp_path: Path) -> None:
    """``--input-dir`` is a shell argument, so cleanup runs on evidence not trust.

    The abbreviation that makes this urgent is ``--input-dir .``: a recursive
    delete of the argument takes the launch directory with it, which is the
    worst possible outcome for a script whose whole purpose is tidiness.
    """
    module = _merge_module()
    sections = _staged(tmp_path)
    notes = sections / "notes.txt"
    notes.write_text("not ours\n", encoding="utf-8")

    module.merge_sections(sections, tmp_path / "review.md")

    assert notes.read_text(encoding="utf-8") == "not ours\n"
    assert (sections / "01-summary.md").exists()


def test_no_instruction_points_at_a_bundled_file_by_a_bare_path() -> None:
    """The rule is stated in the manifest; nothing checked the manifest kept it.

    A bare ``references/…`` resolves against the launch directory, so the model
    copies the bundled file there to read it and leaves the copy behind. One
    such path survived the change that introduced the rule, precisely because
    the rule was prose and had no test.
    """
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    bare = [
        line.strip()
        for line in text.splitlines()
        for marker in ("`references/", "`scripts/")
        if marker in line
    ]
    assert bare == []


def test_cleanup_spares_a_subdirectory_it_did_not_stage(tmp_path: Path) -> None:
    module = _merge_module()
    sections = _staged(tmp_path)
    (sections / "figures").mkdir()

    module.merge_sections(sections, tmp_path / "review.md")

    assert (sections / "figures").is_dir()


def test_pdf_text_extraction_falls_back_from_pymupdf_to_pypdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _extract_module()
    fitz = ModuleType("fitz")

    def broken_open(_path: str) -> None:
        raise ValueError("damaged xref")

    fitz.open = broken_open  # type: ignore[attr-defined]
    pypdf = ModuleType("pypdf")

    class Page:
        @staticmethod
        def extract_text() -> str:
            return "Text recovered by the fallback parser."

    class PdfReader:
        def __init__(self, _path: str) -> None:
            self.pages = [Page()]

    pypdf.PdfReader = PdfReader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", fitz)
    monkeypatch.setitem(sys.modules, "pypdf", pypdf)

    assert module.extract_pdf_text("paper.pdf") == (
        "Text recovered by the fallback parser."
    )


def test_pdf_text_extraction_reports_failures_from_both_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _extract_module()
    fitz = ModuleType("fitz")
    pypdf = ModuleType("pypdf")

    def broken_open(_path: str) -> None:
        raise ValueError("damaged xref")

    class BrokenPdfReader:
        def __init__(self, _path: str) -> None:
            raise ValueError("invalid page tree")

    fitz.open = broken_open  # type: ignore[attr-defined]
    pypdf.PdfReader = BrokenPdfReader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", fitz)
    monkeypatch.setitem(sys.modules, "pypdf", pypdf)

    with pytest.raises(RuntimeError) as raised:
        module.extract_pdf_text("paper.pdf")

    assert "PyMuPDF failed: damaged xref" in str(raised.value)
    assert "pypdf failed: invalid page tree" in str(raised.value)


def test_primary_parser_failure_reports_that_the_fallback_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _extract_module()
    fitz = ModuleType("fitz")

    def broken_open(_path: str) -> None:
        raise ValueError("damaged xref")

    fitz.open = broken_open  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fitz", fitz)
    monkeypatch.delitem(sys.modules, "pypdf", raising=False)
    real_import = builtins.__import__

    def import_without_pypdf(
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("pypdf is absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pypdf)

    with pytest.raises(module.PdfFallbackUnavailableError) as raised:
        module.extract_pdf_text("paper.pdf")

    assert "pypdf fallback is not installed" in str(raised.value)


def test_private_pypdf_runtime_is_pinned_verified_and_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _pdf_runtime_module()
    monkeypatch.setattr(module.sys, "path", list(module.sys.path))
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: "/usr/bin/uv" if name == "uv" else None,
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        target = Path(argv[argv.index("--target") + 1])
        requirements = Path(argv[argv.index("--requirements") + 1])
        requirement_text = requirements.read_text(encoding="utf-8")
        assert module.PYPDF_SPEC in requirement_text
        assert module.PYPDF_WHEEL_SHA256 in requirement_text
        (target / "pypdf").mkdir()
        (target / "pypdf" / "__init__.py").write_text(
            '__version__ = "6.14.2"\n',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    cache_dir = tmp_path / "cache"

    first = module.ensure_pypdf_runtime(cache_dir)
    second = module.ensure_pypdf_runtime(cache_dir)

    assert first["installed"] is True
    assert second["installed"] is False
    assert len(calls) == 1
    assert calls[0][0:3] == ["/usr/bin/uv", "pip", "install"]
    assert "--require-hashes" in calls[0]
    runtime_dir = module.pypdf_runtime_dir(cache_dir)
    assert runtime_dir.is_dir()
    assert str(runtime_dir) in module.sys.path
    assert runtime_dir.is_relative_to(cache_dir.resolve())
