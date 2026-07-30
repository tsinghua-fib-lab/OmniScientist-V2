"""Deterministic research-quality checks for claims, statistics, and artifacts.

The evaluator deliberately avoids an LLM judge.  It consumes structured
evidence bindings and numeric/reproducibility contracts, making the result
stable enough for CI while still accepting richer, domain-specific fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CITATION_RE = re.compile(r"\[(S\d+)\]", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """One auditable quality assertion."""

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class QualityDimension:
    """A scored group of research-quality checks."""

    name: str
    checks: list[QualityCheck] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)

    @property
    def score(self) -> float:
        if self.metrics.get("score") is not None:
            return self.metrics["score"]
        return sum(item.passed for item in self.checks) / len(self.checks) if self.checks else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "metrics": {key: round(value, 4) for key, value in self.metrics.items()},
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(slots=True)
class ResearchQualityReport:
    """Aggregate quality report used by the CLI and CI."""

    dimensions: list[QualityDimension]

    @property
    def score(self) -> float:
        return sum(item.score for item in self.dimensions) / len(self.dimensions) if self.dimensions else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.dimensions) and all(item.passed for item in self.dimensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 4),
            "dimensions": {item.name: item.to_dict() for item in self.dimensions},
        }


def evaluate_citation_fidelity(payload: dict[str, Any]) -> QualityDimension:
    """Score whether claims cite existing evidence that is declared supportive.

    ``claims[].supported_by`` is the preferred oracle for CI.  When omitted, a
    citation is considered structurally supported when it resolves to a source;
    semantic entailment should then be supplied by a domain-specific extension.
    """
    raw_sources = payload.get("sources") or {}
    if isinstance(raw_sources, list):
        sources = {
            str(item.get("id") or item.get("label") or "").upper(): item
            for item in raw_sources
            if isinstance(item, dict)
        }
    else:
        sources = {str(key).upper(): value for key, value in dict(raw_sources).items()}
    claims = list(payload.get("claims") or [])
    if not claims and payload.get("text"):
        claims = [
            {"text": sentence, "citations": _CITATION_RE.findall(sentence)}
            for sentence in re.split(r"(?<=[.!?。！？])\s*", str(payload["text"]))
            if sentence.strip()
        ]

    cited = 0
    resolved = 0
    supported = 0
    covered_claims = 0
    checks: list[QualityCheck] = []
    for index, raw_claim in enumerate(claims, start=1):
        claim = raw_claim if isinstance(raw_claim, dict) else {"text": str(raw_claim)}
        citations = [
            str(item).upper()
            for item in (claim.get("citations") or _CITATION_RE.findall(str(claim.get("text", ""))))
        ]
        oracle = {str(item).upper() for item in (claim.get("supported_by") or [])}
        claim_supported = False
        for citation in citations:
            cited += 1
            exists = citation in sources
            resolved += int(exists)
            is_supported = exists and (citation in oracle if oracle else True)
            supported += int(is_supported)
            claim_supported = claim_supported or is_supported
            checks.append(QualityCheck(
                f"claim_{index}:{citation}",
                is_supported,
                "bound to supporting evidence" if is_supported else "missing or unsupported source binding",
            ))
        covered_claims += int(claim_supported)

    claim_total = len(claims)
    precision = supported / cited if cited else 0.0
    resolution = resolved / cited if cited else 0.0
    coverage = covered_claims / claim_total if claim_total else 0.0
    min_precision = float(payload.get("min_precision", 0.9))
    min_coverage = float(payload.get("min_coverage", 0.8))
    checks.extend([
        QualityCheck("citation_precision", precision >= min_precision,
                     f"{precision:.3f} >= {min_precision:.3f}"),
        QualityCheck("claim_coverage", coverage >= min_coverage,
                     f"{coverage:.3f} >= {min_coverage:.3f}"),
    ])
    return QualityDimension(
        "citation_fidelity",
        checks,
        {
            "precision": precision,
            "resolution": resolution,
            "claim_coverage": coverage,
            "score": (precision + coverage) / 2,
        },
    )


def evaluate_statistical_correctness(payload: dict[str, Any]) -> QualityDimension:
    """Evaluate reported values using tolerances and common statistical invariants."""
    checks: list[QualityCheck] = []
    for index, assertion in enumerate(payload.get("assertions") or [], start=1):
        item = dict(assertion)
        name = str(item.get("name") or f"assertion_{index}")
        kind = str(item.get("kind") or "value")
        passed = False
        detail = ""
        try:
            if kind == "interval":
                lower = float(item["lower"])
                estimate = float(item["estimate"])
                upper = float(item["upper"])
                passed = lower <= estimate <= upper
                detail = f"{lower} <= {estimate} <= {upper}"
            elif kind in {"probability", "p_value", "proportion"}:
                value = float(item["reported"])
                passed = 0.0 <= value <= 1.0
                detail = f"0 <= {value} <= 1"
            elif kind in {"sample_size", "count"}:
                value = float(item["reported"])
                passed = value > 0 and value.is_integer()
                detail = f"positive integer n={value:g}"
            elif kind in {"nonnegative", "variance", "standard_error"}:
                value = float(item["reported"])
                passed = value >= 0.0
                detail = f"{value} >= 0"
            elif kind == "confidence_level":
                value = float(item["reported"])
                passed = 0.0 < value < 1.0
                detail = f"0 < {value} < 1"
            elif kind == "degrees_of_freedom":
                value = float(item["reported"])
                passed = value > 0.0
                detail = f"df={value:g} > 0"
            else:
                reported = float(item["reported"])
                expected = float(item["expected"])
                atol = float(item.get("atol", 1e-9))
                rtol = float(item.get("rtol", 1e-6))
                passed = math.isclose(reported, expected, rel_tol=rtol, abs_tol=atol)
                detail = f"reported={reported:g}, expected={expected:g}, atol={atol:g}, rtol={rtol:g}"
        except (KeyError, TypeError, ValueError) as exc:
            detail = f"invalid statistical assertion: {exc}"
        checks.append(QualityCheck(name, passed, detail))
    return QualityDimension("statistical_correctness", checks)


def evaluate_figure_consistency(payload: dict[str, Any]) -> QualityDimension:
    """Check that a figure still matches its declared code, data, and experiment runs."""
    manifest = dict(payload.get("manifest") or payload)
    actual = {str(key): str(value) for key, value in dict(payload.get("actual_hashes") or {}).items()}
    figure = manifest.get("figure") if isinstance(manifest.get("figure"), dict) else {}
    code = manifest.get("code") if isinstance(manifest.get("code"), dict) else {}
    data = [item for item in manifest.get("data") or [] if isinstance(item, dict)]
    run_ids = [str(value) for value in manifest.get("run_ids") or [] if str(value)]
    checks = [
        QualityCheck(
            "schema",
            manifest.get("schema") == "omni.figure_bundle/v1",
            f"schema={manifest.get('schema')!r}",
        ),
        QualityCheck("figure_declared", bool(figure.get("uri")), "figure artifact declared"),
        QualityCheck("code_declared", bool(code.get("uri")), "generation code declared"),
        QualityCheck("data_declared", bool(data), f"data artifacts={len(data)}"),
        QualityCheck("run_binding", bool(run_ids), f"run_ids={len(run_ids)}"),
    ]
    for role, entries in (("figure", [figure]), ("code", [code]), ("data", data)):
        for entry in entries:
            uri = str(entry.get("uri") or "")
            expected = str(entry.get("sha256") or "")
            observed = actual.get(uri, "")
            checks.append(QualityCheck(
                f"{role}_hash:{uri}",
                bool(uri and _SHA256_RE.fullmatch(expected) and observed == expected),
                f"actual={observed or 'missing'} expected={expected or 'missing'}",
            ))
    return QualityDimension("figure_consistency", checks)


def evaluate_reproducibility(payload: dict[str, Any]) -> QualityDimension:
    """Validate an ``omni.repro_bundle/v1``-style manifest and optional artifact."""
    manifest = dict(payload.get("manifest") or payload)
    artifact = dict(manifest.get("artifact") or {})
    creation = dict(manifest.get("creation") or {})
    environment = dict(manifest.get("environment") or {})
    stochastic = bool(payload.get("stochastic", manifest.get("stochastic", True)))
    digest = str(artifact.get("sha256") or "")
    checks = [
        QualityCheck("schema", manifest.get("schema") == "omni.repro_bundle/v1",
                     f"schema={manifest.get('schema')!r}"),
        QualityCheck("artifact_hash", bool(_SHA256_RE.fullmatch(digest)),
                     "sha256 present and well formed" if digest else "sha256 missing"),
        QualityCheck("creation_entrypoint", bool(creation.get("command") or creation.get("code_file")),
                     "command or code_file declared"),
        QualityCheck("inputs", isinstance(creation.get("inputs"), dict), "input snapshot declared"),
        QualityCheck("environment", bool(environment.get("env_lock") and environment.get("python")),
                     "env_lock and Python version declared"),
        QualityCheck("seed", (creation.get("seed") is not None) or not stochastic,
                     "seed declared" if stochastic else "deterministic run"),
    ]
    artifact_path = str(payload.get("artifact_path") or "")
    if artifact_path:
        path = Path(artifact_path).expanduser()
        actual = _sha256_file(path) if path.is_file() else ""
        checks.append(QualityCheck(
            "artifact_bytes",
            bool(actual and actual.lower() == digest.lower()),
            f"actual={actual or 'missing'} expected={digest or 'missing'}",
        ))
    return QualityDimension("reproducibility", checks)


def evaluate_research_quality(payload: dict[str, Any]) -> ResearchQualityReport:
    """Evaluate every quality dimension present in ``payload``."""
    dimensions: list[QualityDimension] = []
    if "citation" in payload:
        dimensions.append(evaluate_citation_fidelity(dict(payload["citation"] or {})))
    if "statistics" in payload:
        dimensions.append(evaluate_statistical_correctness(dict(payload["statistics"] or {})))
    if "reproducibility" in payload:
        dimensions.append(evaluate_reproducibility(dict(payload["reproducibility"] or {})))
    if "figure_consistency" in payload:
        dimensions.append(evaluate_figure_consistency(dict(payload["figure_consistency"] or {})))
    return ResearchQualityReport(dimensions)


def load_quality_payload(path: Path | None = None) -> dict[str, Any]:
    """Load a user payload or the bundled offline quality fixture."""
    source = path or (
        Path(__file__).resolve().parent.parent / "data" / "research_quality" / "baseline.json"
    )
    return dict(json.loads(source.read_text(encoding="utf-8")))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "QualityCheck",
    "QualityDimension",
    "ResearchQualityReport",
    "evaluate_citation_fidelity",
    "evaluate_statistical_correctness",
    "evaluate_reproducibility",
    "evaluate_figure_consistency",
    "evaluate_research_quality",
    "load_quality_payload",
]
