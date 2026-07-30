"""Fail-closed changed-code coverage evidence for CI and releases."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class DiffCoverageEvidenceError(ValueError):
    """Coverage or Git evidence is absent, malformed, or ambiguous."""


@dataclass(frozen=True)
class ChangedFileCoverage:
    """Coverage of executable lines changed in one Python source file."""

    path: str
    changed_executable_lines: int
    covered_changed_lines: int
    missing_lines: tuple[int, ...]


@dataclass(frozen=True)
class DiffCoverageReport:
    """Serializable result of the changed-code coverage release gate."""

    schema_version: int
    gate: str
    passed: bool
    applicable: bool
    baseline_kind: str
    base_ref: str
    base_sha: str
    candidate_ref: str
    candidate_sha: str
    minimum_percent: float
    coverage_percent: float | None
    changed_executable_lines: int
    covered_changed_lines: int
    missing_changed_lines: int
    files: tuple[ChangedFileCoverage, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible artifact payload."""
        return asdict(self)


def parse_changed_lines(
    patch: str,
    *,
    source_prefix: str = "cli/src/omni",
) -> dict[str, frozenset[int]]:
    """Parse added-side line ranges from a zero-context unified Git diff."""
    prefix = source_prefix.strip("/")
    changed: dict[str, set[int]] = {}
    current_path = ""
    for raw_line in patch.splitlines():
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].split("\t", 1)[0]
            if candidate == "/dev/null":
                current_path = ""
                continue
            current_path = candidate.removeprefix("b/").removeprefix("./")
            if (
                current_path != prefix
                and not current_path.startswith(f"{prefix}/")
            ) or not current_path.endswith(".py"):
                current_path = ""
            continue
        if not current_path:
            continue
        match = _HUNK_RE.match(raw_line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        if count:
            changed.setdefault(current_path, set()).update(
                range(start, start + count)
            )
    return {path: frozenset(lines) for path, lines in sorted(changed.items())}


def _normalize_coverage_path(
    raw_path: str,
    *,
    repository_root: Path,
    source_prefix: str,
) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repository_root.resolve()).as_posix()
        except ValueError as exc:
            raise DiffCoverageEvidenceError(
                f"coverage path is outside repository: {raw_path}"
            ) from exc
    normalized = path.as_posix().removeprefix("./")
    if normalized.startswith("src/omni/") and source_prefix.startswith("cli/"):
        return f"cli/{normalized}"
    return normalized


def _line_set(
    value: Any,
    *,
    path: str,
    field: str,
) -> frozenset[int]:
    if not isinstance(value, list) or any(
        not isinstance(line, int) or isinstance(line, bool) or line <= 0
        for line in value
    ):
        raise DiffCoverageEvidenceError(
            f"coverage file {path!r} has invalid {field}"
        )
    return frozenset(value)


def load_coverage_lines(
    coverage_path: str | Path,
    *,
    repository_root: str | Path,
    source_prefix: str = "cli/src/omni",
) -> dict[str, tuple[frozenset[int], frozenset[int]]]:
    """Load executed/missing statement lines from ``coverage json`` output."""
    path = Path(coverage_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiffCoverageEvidenceError(
            f"cannot read coverage evidence {path}: {exc}"
        ) from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise DiffCoverageEvidenceError("coverage evidence has no files object")

    root = Path(repository_root)
    loaded: dict[str, tuple[frozenset[int], frozenset[int]]] = {}
    for raw_name, raw_evidence in files.items():
        if not isinstance(raw_name, str) or not isinstance(raw_evidence, dict):
            raise DiffCoverageEvidenceError("coverage files entries are malformed")
        normalized = _normalize_coverage_path(
            raw_name,
            repository_root=root,
            source_prefix=source_prefix,
        )
        if (
            normalized != source_prefix
            and not normalized.startswith(f"{source_prefix.rstrip('/')}/")
        ) or not normalized.endswith(".py"):
            continue
        if "executed_lines" not in raw_evidence:
            raise DiffCoverageEvidenceError(
                f"coverage file {normalized!r} has no executed_lines"
            )
        if "missing_lines" not in raw_evidence:
            raise DiffCoverageEvidenceError(
                f"coverage file {normalized!r} has no missing_lines"
            )
        executed = _line_set(
            raw_evidence["executed_lines"],
            path=normalized,
            field="executed_lines",
        )
        missing = _line_set(
            raw_evidence["missing_lines"],
            path=normalized,
            field="missing_lines",
        )
        if executed & missing:
            raise DiffCoverageEvidenceError(
                f"coverage file {normalized!r} marks lines both executed and missing"
            )
        loaded[normalized] = (executed, missing)
    if not loaded:
        raise DiffCoverageEvidenceError(
            f"coverage evidence contains no Python files under {source_prefix}"
        )
    return loaded


def evaluate_diff_coverage(
    *,
    changed_lines: dict[str, frozenset[int]],
    coverage_lines: dict[str, tuple[frozenset[int], frozenset[int]]],
    base_ref: str,
    candidate_ref: str,
    base_sha: str | None = None,
    candidate_sha: str | None = None,
    baseline_kind: str = "explicit_ref",
    minimum_percent: float = 80.0,
) -> DiffCoverageReport:
    """Intersect changed lines with measured statements and enforce the floor."""
    if not base_ref.strip() or not candidate_ref.strip():
        raise DiffCoverageEvidenceError(
            "base_ref and candidate_ref are required for provenance"
        )
    if not 0.0 <= minimum_percent <= 100.0:
        raise DiffCoverageEvidenceError("minimum_percent must be between 0 and 100")

    file_reports: list[ChangedFileCoverage] = []
    total_executable = 0
    total_covered = 0
    for path, additions in sorted(changed_lines.items()):
        if path not in coverage_lines:
            raise DiffCoverageEvidenceError(
                f"missing coverage evidence for changed source file {path!r}"
            )
        executed, missing = coverage_lines[path]
        executable = additions & (executed | missing)
        covered = executable & executed
        uncovered = tuple(sorted(executable & missing))
        total_executable += len(executable)
        total_covered += len(covered)
        file_reports.append(
            ChangedFileCoverage(
                path=path,
                changed_executable_lines=len(executable),
                covered_changed_lines=len(covered),
                missing_lines=uncovered,
            )
        )

    applicable = total_executable > 0
    percentage = (
        round((total_covered / total_executable) * 100.0, 2)
        if applicable
        else None
    )
    return DiffCoverageReport(
        schema_version=1,
        gate="changed_code_coverage",
        passed=not applicable or percentage >= minimum_percent,
        applicable=applicable,
        baseline_kind=baseline_kind,
        base_ref=base_ref,
        base_sha=base_sha or base_ref,
        candidate_ref=candidate_ref,
        candidate_sha=candidate_sha or candidate_ref,
        minimum_percent=minimum_percent,
        coverage_percent=percentage,
        changed_executable_lines=total_executable,
        covered_changed_lines=total_covered,
        missing_changed_lines=total_executable - total_covered,
        files=tuple(file_reports),
    )


def _git(
    repository_root: Path,
    *arguments: str,
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise DiffCoverageEvidenceError(
            f"git {' '.join(arguments)} failed: {detail}"
        )
    return result.stdout.strip()


def _optional_git(repository_root: Path, *arguments: str) -> str:
    try:
        return _git(repository_root, *arguments)
    except DiffCoverageEvidenceError:
        return ""


def resolve_comparison(
    repository_root: str | Path,
    *,
    base_ref: str,
    candidate_ref: str,
    fallback_base_ref: str = "",
) -> tuple[str, str, str]:
    """Resolve an explicit baseline or the previous ``v*`` release tag."""
    root = Path(repository_root)
    candidate_sha = _git(
        root,
        "rev-parse",
        "--verify",
        f"{candidate_ref}^{{commit}}",
    )
    resolved_base_ref = base_ref
    if base_ref and set(base_ref) == {"0"}:
        if not fallback_base_ref:
            raise DiffCoverageEvidenceError(
                "the push has no previous commit and no fallback base ref"
            )
        fallback_sha = _optional_git(
            root,
            "rev-parse",
            "--verify",
            f"{fallback_base_ref}^{{commit}}",
        )
        base_sha = (
            _optional_git(root, "merge-base", fallback_sha, candidate_sha)
            if fallback_sha
            else ""
        )
        if base_sha and base_sha != candidate_sha:
            resolved_base_ref = f"merge-base:{fallback_base_ref}"
        else:
            base_sha = _optional_git(
                root,
                "rev-parse",
                "--verify",
                f"{candidate_sha}^",
            )
            if base_sha:
                resolved_base_ref = "candidate-parent"
            else:
                base_sha = _EMPTY_TREE_SHA
                resolved_base_ref = "empty-tree"
    elif base_ref == "previous-tag":
        resolved_base_ref = _optional_git(
            root,
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v*",
            f"{candidate_sha}^",
        )
        if not resolved_base_ref:
            if not fallback_base_ref:
                raise DiffCoverageEvidenceError(
                    "no previous v* tag and no immutable bootstrap base ref"
                )
            resolved_base_ref = f"bootstrap:{fallback_base_ref}"
            base_sha = _git(
                root,
                "rev-parse",
                "--verify",
                f"{fallback_base_ref}^{{commit}}",
            )
        else:
            base_sha = _git(
                root,
                "rev-parse",
                "--verify",
                f"{resolved_base_ref}^{{commit}}",
            )
    else:
        base_sha = _git(
            root,
            "rev-parse",
            "--verify",
            f"{resolved_base_ref}^{{commit}}",
        )
    if base_sha == candidate_sha:
        raise DiffCoverageEvidenceError(
            "comparison baseline resolves to the candidate commit"
        )
    if base_sha != _EMPTY_TREE_SHA:
        _git(
            root,
            "merge-base",
            "--is-ancestor",
            base_sha,
            candidate_sha,
        )
    return resolved_base_ref, base_sha, candidate_sha


def baseline_kind(resolved_base_ref: str) -> str:
    """Classify resolved baseline provenance for the release artifact."""
    if resolved_base_ref.startswith("bootstrap:"):
        return "immutable_bootstrap"
    if resolved_base_ref.startswith("merge-base:"):
        return "default_branch_merge_base"
    if resolved_base_ref == "candidate-parent":
        return "candidate_parent"
    if resolved_base_ref == "empty-tree":
        return "empty_tree"
    if resolved_base_ref.startswith("v"):
        return "previous_release_tag"
    return "explicit_ref"


def collect_changed_lines(
    repository_root: str | Path,
    *,
    base_sha: str,
    candidate_sha: str,
    source_prefix: str = "cli/src/omni",
) -> dict[str, frozenset[int]]:
    """Collect committed candidate changes relative to the merge base."""
    comparison = (
        (base_sha, candidate_sha)
        if base_sha == _EMPTY_TREE_SHA
        else (f"{base_sha}...{candidate_sha}",)
    )
    patch = _git(
        Path(repository_root),
        "diff",
        "--unified=0",
        "--no-ext-diff",
        "--find-renames",
        *comparison,
        "--",
        source_prefix,
    )
    return parse_changed_lines(patch, source_prefix=source_prefix)


def write_report(
    path: str | Path,
    payload: DiffCoverageReport | dict[str, Any],
) -> None:
    """Write one durable, human-readable gate artifact."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = payload.to_dict() if isinstance(payload, DiffCoverageReport) else payload
    destination.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def initialize_fail_closed_report(
    path: str | Path,
    *,
    base_ref: str,
    candidate_ref: str,
    failure_stage: str,
    error: str = "",
) -> None:
    """Create a failing artifact before evidence collection begins."""
    write_report(
        path,
        {
            "schema_version": 1,
            "gate": "changed_code_coverage",
            "passed": False,
            "baseline_kind": "unresolved",
            "base_ref": base_ref,
            "base_sha": "",
            "candidate_ref": candidate_ref,
            "candidate_sha": "",
            "failure_stage": failure_stage,
            **({"error": error} if error else {}),
        },
    )
