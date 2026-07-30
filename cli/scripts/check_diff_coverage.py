#!/usr/bin/env python3
"""Enforce the changed-code coverage release criterion."""

from __future__ import annotations

import argparse
from pathlib import Path

from omni.eval.diff_coverage_gate import (
    DiffCoverageEvidenceError,
    baseline_kind,
    collect_changed_lines,
    evaluate_diff_coverage,
    initialize_fail_closed_report,
    load_coverage_lines,
    resolve_comparison,
    write_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-json", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--fallback-base-ref", default="")
    parser.add_argument("--candidate-ref", default="HEAD")
    parser.add_argument("--repository-root", default=Path.cwd(), type=Path)
    parser.add_argument("--source-prefix", default="cli/src/omni")
    parser.add_argument("--minimum-percent", default=80.0, type=float)
    parser.add_argument("--test-outcome", default="success")
    return parser


def main() -> int:
    args = _parser().parse_args()
    initialize_fail_closed_report(
        args.report,
        base_ref=args.base_ref,
        candidate_ref=args.candidate_ref,
        failure_stage="diff_coverage_started",
    )
    try:
        if args.test_outcome != "success":
            raise DiffCoverageEvidenceError(
                f"coverage test suite outcome was {args.test_outcome!r}"
            )
        resolved_base, base_sha, candidate_sha = resolve_comparison(
            args.repository_root,
            base_ref=args.base_ref,
            candidate_ref=args.candidate_ref,
            fallback_base_ref=args.fallback_base_ref,
        )
        changed_lines = collect_changed_lines(
            args.repository_root,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            source_prefix=args.source_prefix,
        )
        coverage_lines = load_coverage_lines(
            args.coverage_json,
            repository_root=args.repository_root,
            source_prefix=args.source_prefix,
        )
        report = evaluate_diff_coverage(
            changed_lines=changed_lines,
            coverage_lines=coverage_lines,
            base_ref=resolved_base,
            base_sha=base_sha,
            baseline_kind=baseline_kind(resolved_base),
            candidate_ref=args.candidate_ref,
            candidate_sha=candidate_sha,
            minimum_percent=args.minimum_percent,
        )
    except DiffCoverageEvidenceError as exc:
        initialize_fail_closed_report(
            args.report,
            base_ref=args.base_ref,
            candidate_ref=args.candidate_ref,
            failure_stage="evidence_invalid",
            error=str(exc),
        )
        return 2
    write_report(args.report, report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
