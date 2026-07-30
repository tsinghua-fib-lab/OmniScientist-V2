"""Reusable Chromium inspection for self-contained scientific posters."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    for _candidate in Path(__file__).resolve().parents:
        if (_candidate / "posterlib" / "paths.py").is_file():
            sys.path.insert(0, str(_candidate))
            break

from posterlib.paths import SKILL_ROOT

if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

import poster_core  # noqa: E402 - copied Skill bootstraps its own root

from posterlib.content.planning import typography_metrics  # noqa: E402
from posterlib.runtime import browser_scripts  # noqa: E402
from posterlib.runtime.capability import (  # noqa: E402
    classify_chromium_failure,
    missing_result,
)
from posterlib.visual import visual_evidence  # noqa: E402

POSTER_SELECTOR = poster_core.POSTER_ROOT_SELECTOR
DEFAULT_VIEWPORT_WIDTH = 1400
DEFAULT_VIEWPORT_HEIGHT = 1000
ASPECT_RATIO_TOLERANCE = 0.005
FONT_SIZE_TOLERANCE_MM = 0.02
ADVISORY_FONT_REFERENCE_MM = 12.0 * 25.4 / 72.0

DELIVERY_INTEGRITY_GEOMETRY_CODES = frozenset(
    {
        "element_content_overflow",
        "element_outside_poster",
        "module_overlap",
        "poster_scroll_overflow",
    }
)

HARD_BLOCKER_CODES = (
    frozenset(
        {
            "blocked_network_request",
            "duplicate_poster_id",
            "empty_poster_id",
            "invalid_poster_measurement",
            "invalid_poster_root_count",
            "invalid_source_figure_hash",
            "missing_poster",
            "missing_source_figure",
            "missing_title_band",
            "missing_usable_source_figure",
            "missing_visible_content",
            "missing_visible_modules",
            "poster_aspect_ratio_mismatch",
            "poster_physical_size_mismatch",
            "source_figure_clipped",
            "source_figure_hidden",
            "source_figure_not_rendered",
            "source_figure_outside_poster",
            "unusable_source_figure",
            "zero_element_rect",
            "zero_poster_rect",
        }
    )
    | DELIVERY_INTEGRITY_GEOMETRY_CODES
)

FONT_READY_JS = browser_scripts.load("wait_for_assets.js")
INSPECT_JS = browser_scripts.load("inspect_dom.js", bind_poster_selector=True)


def _validate_report(
    report: dict[str, Any],
    blocked_requests: list[str],
    *,
    expected_page: dict[str, float] | None = None,
    expected_source_figure_sha256s: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Classify deterministic safety, geometry, asset, and typography findings."""

    raw_warnings = report.get("warnings")
    warnings = (
        [item for item in raw_warnings if isinstance(item, dict)]
        if isinstance(raw_warnings, list)
        else []
    )
    warnings.extend(
        {
            "code": "blocked_network_request",
            "url": url,
            "message": "External resources are forbidden in self-contained poster HTML.",
        }
        for url in sorted(set(blocked_requests))
    )
    poster = report.get("poster")
    if not isinstance(poster, dict):
        warnings.append(
            {"code": "missing_poster", "message": "Poster root was not measured."}
        )
        report["warnings"] = _normalize_warning_severity(warnings)
        return report["warnings"]

    for field in ("width", "height", "width_mm", "height_mm"):
        if not _positive_number(poster.get(field)):
            warnings.append(
                {
                    "code": "invalid_poster_measurement",
                    "field": field,
                    "message": "Poster measurements must be positive finite numbers.",
                }
            )
    if all(
        _positive_number(poster.get(field))
        for field in ("width", "height", "width_mm", "height_mm")
    ):
        rendered_ratio = float(poster["width"]) / float(poster["height"])
        physical_ratio = float(poster["width_mm"]) / float(poster["height_mm"])
        relative_error = abs(rendered_ratio - physical_ratio) / physical_ratio
        if relative_error > ASPECT_RATIO_TOLERANCE:
            warnings.append(
                {
                    "code": "poster_aspect_ratio_mismatch",
                    "relative_error": relative_error,
                    "message": "Rendered geometry must match the physical page aspect ratio.",
                }
            )

    if expected_page is not None:
        rendered_width = _finite_float(poster.get("width_mm"))
        rendered_height = _finite_float(poster.get("height_mm"))
        expected_width = _finite_float(expected_page.get("width_mm"))
        expected_height = _finite_float(expected_page.get("height_mm"))
        values = (rendered_width, rendered_height, expected_width, expected_height)
        if all(value is not None and value > 0 for value in values):
            assert rendered_width is not None
            assert rendered_height is not None
            assert expected_width is not None
            assert expected_height is not None
            width_error = abs(rendered_width - expected_width) / expected_width
            height_error = abs(rendered_height - expected_height) / expected_height
            if max(width_error, height_error) > 0.01:
                warnings.append(
                    {
                        "code": "poster_physical_size_mismatch",
                        "rendered_width_mm": rendered_width,
                        "rendered_height_mm": rendered_height,
                        "expected_width_mm": expected_width,
                        "expected_height_mm": expected_height,
                        "message": "Rendered poster dimensions must match @page.",
                    }
                )

    modules = report.get("modules")
    if not isinstance(modules, list) or not modules:
        warnings.append(
            {
                "code": "missing_visible_modules",
                "message": "Poster must contain visible scientific modules.",
            }
        )
    if not isinstance(report.get("title_band"), dict):
        warnings.append(
            {
                "code": "missing_title_band",
                "message": "Poster needs one visible data-poster-title-band header.",
            }
        )
    source_figures = report.get("source_figures")
    source_figures = source_figures if isinstance(source_figures, dict) else {}
    referenced = _hash_set(source_figures.get("referenced_sha256s"))
    usable = _hash_set(source_figures.get("usable_sha256s"))
    readable_recorded = "readable_sha256s" in source_figures
    readable = _hash_set(source_figures.get("readable_sha256s"))
    if expected_source_figure_sha256s is None:
        warnings.append(
            {
                "code": "source_figure_check_unavailable",
                "severity": "warning",
                "message": (
                    "Source-figure availability was not supplied; inspection cannot "
                    "prove figure use."
                ),
            }
        )
    elif expected_source_figure_sha256s and not referenced:
        warnings.append(
            {
                "code": "missing_source_figure",
                "observed": {
                    "expected_count": len(expected_source_figure_sha256s),
                    "referenced_count": 0,
                },
                "message": "The PDF yielded usable figures but the poster references none.",
            }
        )
    elif expected_source_figure_sha256s and not usable.intersection(
        expected_source_figure_sha256s
    ):
        warnings.append(
            {
                "code": "missing_usable_source_figure",
                "observed": {
                    "expected_count": len(expected_source_figure_sha256s),
                    "usable_count": 0,
                },
                "message": (
                    "A prepared PDF figure must have a valid hash, render at nonzero "
                    "size, remain visible and unclipped, and stay inside the poster."
                ),
            }
        )
    elif referenced - usable:
        warnings.append(
            {
                "code": "unusable_source_figure",
                "observed": {
                    "referenced_count": len(referenced),
                    "usable_count": len(usable),
                },
                "message": (
                    "Every referenced PDF figure must render visibly, unclipped, and "
                    "inside the poster."
                ),
            }
        )
    if readable_recorded and usable - readable:
        warnings.append(
            {
                "code": "source_figure_below_readability_target",
                "observed": {
                    "usable_count": len(usable),
                    "readable_count": len(readable),
                },
                "target": {
                    "minimum_area_ratio": 0.005,
                    "minimum_orientation_extent_ratio": 0.18,
                },
                "message": (
                    "A usable source figure is below the advisory conference-viewing "
                    "size target."
                ),
            }
        )

    type_records = report.get("typography")
    rendered_width_mm = _finite_float(poster.get("width_mm"))
    if isinstance(type_records, list) and rendered_width_mm is not None:
        minimums = typography_metrics(rendered_width_mm)
        fallback_emitted = False
        for item in type_records:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "body")
            font_size_mm = _finite_float(item.get("font_size_mm"))
            reading_target_mm = _typography_target_mm(role, minimums)
            if font_size_mm is not None and font_size_mm < ADVISORY_FONT_REFERENCE_MM:
                warnings.append(
                    {
                        "code": "type_below_advisory_reference",
                        "poster_id": item.get("poster_id"),
                        "role": role,
                        "observed": {"font_size_mm": font_size_mm},
                        "target": {"minimum_mm": ADVISORY_FONT_REFERENCE_MM},
                        "message": "Poster type is below the advisory 12 pt readability reference.",
                    }
                )
            elif (
                font_size_mm is not None
                and font_size_mm + FONT_SIZE_TOLERANCE_MM < reading_target_mm
            ):
                warnings.append(
                    {
                        "code": "type_below_reading_target",
                        "poster_id": item.get("poster_id"),
                        "role": role,
                        "observed": {"font_size_mm": font_size_mm},
                        "target": {"minimum_mm": reading_target_mm},
                        "message": (
                            "Poster type is below its advisory conference-viewing target."
                        ),
                    }
                )
            line_count = item.get("line_count")
            display_risk = role in {"title", "focal"} and (
                item.get("requested_face_available") is False
                or (
                    isinstance(line_count, int)
                    and not isinstance(line_count, bool)
                    and line_count >= 4
                )
            )
            if display_risk and not fallback_emitted:
                fallback_emitted = True
                warnings.append(
                    {
                        "code": "typography_availability_risk",
                        "severity": "warning",
                        "poster_id": item.get("poster_id"),
                        "observed": {
                            "line_count": line_count,
                            "font_family": item.get("font_family"),
                            "requested_face_available": item.get(
                                "requested_face_available"
                            ),
                        },
                        "message": (
                            "The display face is unavailable or wraps too deeply; "
                            "consider a local fallback."
                        ),
                    }
                )

    elements = report.get("elements")
    if not isinstance(elements, list) or not any(
        isinstance(item, dict)
        and item.get("visible") is True
        and item.get("content") is True
        for item in (elements if isinstance(elements, list) else [])
    ):
        warnings.append(
            {
                "code": "missing_visible_content",
                "message": "Poster must contain visible scientific text or visual evidence.",
            }
        )
    report["warnings"] = _normalize_warning_severity(warnings)
    return report["warnings"]


class ChromiumInspectionSession:
    """Reuse one Chromium process while isolating each poster in a fresh context."""

    def __init__(self) -> None:
        self.playwright: Any = None
        self.browser: Any = None
        self.error: BaseException | None = None
        self.error_stage = "launch"
        self.launch_count = 0

    async def __aenter__(self) -> ChromiumInspectionSession:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            self.error = exc
            self.error_stage = "import"
            return self
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch()
            self.launch_count += 1
        except Exception as exc:  # noqa: BLE001 - browser boundary is external
            self.error = exc
            self.error_stage = "launch"
        return self

    async def inspect_document(
        self,
        html_path: Path,
        out_dir: Path,
        **options: Any,
    ) -> dict[str, Any]:
        """Measure one document using a fresh page and browser context."""

        return await _inspect_target(
            html_path,
            out_dir,
            session=self,
            viewport_width=int(options.pop("viewport_width", DEFAULT_VIEWPORT_WIDTH)),
            viewport_height=int(
                options.pop("viewport_height", DEFAULT_VIEWPORT_HEIGHT)
            ),
            **options,
        )

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        await _close_quietly(self.browser, "close")
        await _close_quietly(self.playwright, "stop")

    def failure_result(self) -> dict[str, Any]:
        """Return the structured capability/runtime result for a failed launch."""

        assert self.error is not None
        if (
            self.error_stage == "import"
            or classify_chromium_failure(self.error) == "missing"
        ):
            return missing_result(
                "browser-inspection",
                dependency="playwright" if self.error_stage == "import" else "chromium",
                stage=self.error_stage,
                error=self.error,
            )
        return _stage_error(
            "chromium_inspection_failed",
            self.error_stage,
            str(self.error),
            self.error,
        )


async def inspect_document(
    html_path: Path,
    out_dir: Path,
    *,
    scale: float = 2.0,
    expected_source_figure_sha256s: set[str] | None = None,
    capture_screenshot: bool = True,
) -> dict[str, Any]:
    """Inspect local poster HTML with real Chromium geometry."""

    async with ChromiumInspectionSession() as session:
        return await session.inspect_document(
            html_path,
            out_dir,
            scale=scale,
            expected_source_figure_sha256s=expected_source_figure_sha256s,
            capture_screenshot=capture_screenshot,
        )


async def _inspect_target(
    html_path: Path,
    out_dir: Path,
    *,
    scale: float,
    viewport_width: int,
    viewport_height: int,
    expected_source_figure_sha256s: set[str] | None,
    capture_screenshot: bool,
    session: ChromiumInspectionSession | None = None,
) -> dict[str, Any]:
    if not _positive_number(scale) or viewport_width <= 0 or viewport_height <= 0:
        return _stage_error(
            "invalid_inspection_options",
            "options",
            "Scale and viewport dimensions must be positive.",
        )
    html_path = html_path.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    if not html_path.is_file():
        return _stage_error(
            "html_not_found", "input", f"HTML file not found: {html_path}"
        )
    try:
        static_report = poster_core.validate_poster_html(
            html_path.read_text(encoding="utf-8")
        )
        expected_page = static_report.get("page")
    except (OSError, UnicodeError) as exc:
        return _stage_error("source_read_failed", "input", str(exc), exc)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _stage_error("inspection_output_failed", "output", str(exc), exc)
    if session is None:
        async with ChromiumInspectionSession() as managed:
            return await managed.inspect_document(
                html_path,
                out_dir,
                scale=scale,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                expected_source_figure_sha256s=expected_source_figure_sha256s,
                capture_screenshot=capture_screenshot,
            )
    if session.error is not None or session.browser is None:
        return session.failure_result()

    browser = session.browser
    context = page = None
    blocked_requests: list[str] = []
    target_url = html_path.as_uri()
    try:
        try:
            context = await browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
                device_scale_factor=float(scale),
            )
            page = await context.new_page()

            async def block_external_requests(route: Any) -> None:
                url = route.request.url
                document = (
                    url == target_url and route.request.resource_type == "document"
                )
                embedded = url.startswith(("data:", "blob:"))
                if document or embedded:
                    await route.continue_()
                else:
                    blocked_requests.append(url)
                    await route.abort("blockedbyclient")

            await page.route("**/*", block_external_requests)
            await page.goto(target_url, wait_until="load")
            await page.evaluate(FONT_READY_JS)
        except Exception as exc:
            return _stage_error(
                "chromium_inspection_failed", "navigation", str(exc), exc
            )
        try:
            locator = page.locator(POSTER_SELECTOR)
            if await locator.count() == 1:
                box = await locator.bounding_box()
                if box is not None:
                    await page.set_viewport_size(
                        {
                            "width": max(
                                viewport_width, math.ceil(box["x"] + box["width"] + 32)
                            ),
                            "height": max(
                                viewport_height,
                                math.ceil(box["y"] + box["height"] + 32),
                            ),
                        }
                    )
                    await page.evaluate(FONT_READY_JS)
            report = await page.evaluate(INSPECT_JS)
            if not isinstance(report, dict):
                raise TypeError("Chromium returned a non-object report")
            warnings = _validate_report(
                report,
                blocked_requests,
                expected_page=expected_page
                if isinstance(expected_page, dict)
                else None,
                expected_source_figure_sha256s=expected_source_figure_sha256s,
            )
            report_path = _persist_report(report, out_dir)
        except Exception as exc:
            return _stage_error("dom_evaluation_failed", "evaluate", str(exc), exc)
        screenshot_path: Path | None = None
        evidence_bundle: dict[str, Any] | None = None
        evidence_path: Path | None = None
        if capture_screenshot and await locator.count() == 1:
            try:
                screenshot_path = out_dir / "poster.png"
                screenshot_path.unlink(missing_ok=True)
                await locator.screenshot(path=str(screenshot_path))
            except Exception as exc:
                return _stage_error("screenshot_failed", "screenshot", str(exc), exc)
            try:
                evidence_bundle, evidence_path = visual_evidence.build_visual_evidence(
                    report=report,
                    screenshot_path=screenshot_path,
                    out_dir=out_dir,
                )
            except Exception as exc:  # noqa: BLE001 - evidence is advisory
                warning = {
                    "code": "visual_evidence_unavailable",
                    "severity": "warning",
                    "message": f"Visual evidence bundle could not be created: {exc}",
                }
                warnings.append(warning)
                report["warnings"] = warnings
                report_path = _persist_report(report, out_dir)
        common: dict[str, Any] = {
            "report": report,
            "report_path": str(report_path),
            "warnings": warnings,
        }
        if screenshot_path is not None:
            common["screenshot_path"] = str(screenshot_path)
        if evidence_bundle is not None and evidence_path is not None:
            common["visual_evidence"] = evidence_bundle
            common["visual_evidence_path"] = str(evidence_path)
        blocking_warnings = [
            warning for warning in warnings if warning.get("severity") == "error"
        ]
        if blocking_warnings:
            return poster_core.outcome_result(
                "inspection_blocked",
                summary="Rendered inspection found approval-blocking poster issues.",
                **common,
            )
        return poster_core.outcome_result(
            "inspection_complete",
            summary="Rendered poster passed Chromium inspection.",
            **common,
        )
    finally:
        await _close_quietly(page, "close")
        await _close_quietly(context, "close")


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _hash_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {
        str(item).lower()
        for item in value
        if re.fullmatch(r"[0-9a-f]{64}", str(item).lower()) is not None
    }


def _typography_target_mm(role: str, metrics: dict[str, float]) -> float:
    if role == "title":
        return metrics["title_min_mm"]
    if role in {"caption", "identity", "provenance"}:
        return metrics["provenance_min_mm"]
    return metrics["body_min_mm"]


def _normalize_warning_severity(
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings = _aggregate_typography_warnings(warnings)
    normalized = [
        {
            **warning,
            "severity": "error"
            if warning.get("code") in HARD_BLOCKER_CODES
            else "warning",
        }
        for warning in warnings
    ]
    return _deduplicate_warnings(normalized)


def _aggregate_typography_warnings(
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse element-level advisory type measurements by semantic role."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    retained: list[dict[str, Any]] = []
    for warning in warnings:
        if warning.get("code") != "type_below_reading_target":
            retained.append(warning)
            continue
        role = str(warning.get("role") or "body")
        grouped.setdefault(role, []).append(warning)
    for role, items in sorted(grouped.items()):
        measured = [
            value
            for item in items
            if (
                value := _finite_float((item.get("observed") or {}).get("font_size_mm"))
            )
            is not None
        ]
        targets = [
            value
            for item in items
            if (value := _finite_float((item.get("target") or {}).get("minimum_mm")))
            is not None
        ]
        if not measured or not targets:
            retained.extend(items)
            continue
        retained.append(
            {
                "code": "type_below_reading_target",
                "role": role,
                "observed": {
                    "minimum_font_size_mm": min(measured),
                    "affected_elements": len(items),
                },
                "target": {"minimum_mm": max(targets)},
                "message": (
                    "Some poster type in this semantic role is below its advisory "
                    "conference-viewing target."
                ),
            }
        )
    return retained


def _deduplicate_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for warning in warnings:
        marker = json.dumps(warning, sort_keys=True, ensure_ascii=True, default=repr)
        if marker not in seen:
            unique.append(warning)
            seen.add(marker)
    return unique


def _persist_report(report: dict[str, Any], out_dir: Path) -> Path:
    report_path = out_dir / "dom-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report_path


def _stage_error(
    code: str,
    stage: str,
    message: str,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    result = poster_core.outcome_result(
        code,
        summary=message,
        error=message,
        error_info={"stage": stage},
    )
    if exc is not None:
        result["error_info"]["exception"] = repr(exc)
    return result


async def _close_quietly(value: Any, method: str) -> None:
    if value is None:
        return
    try:
        await getattr(value, method)()
    except Exception:  # noqa: BLE001 - cleanup cannot replace the primary result
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True, help="Path to poster.html")
    parser.add_argument("--out", default="inspection", help="Output directory")
    parser.add_argument("--width", type=int, default=DEFAULT_VIEWPORT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_VIEWPORT_HEIGHT)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument(
        "--expected-source-figure-sha256",
        action="append",
        default=None,
        help="Expected prepared PDF figure hash; repeat for each figure",
    )
    parser.add_argument(
        "--source-figure-manifest-known",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--no-screenshot", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


async def inspect(args: argparse.Namespace) -> dict[str, Any]:
    raw_hashes = args.expected_source_figure_sha256
    expected_hashes = _hash_set(raw_hashes or [])
    if raw_hashes is not None and len(expected_hashes) != len(set(raw_hashes)):
        return _stage_error(
            "invalid_inspection_options",
            "options",
            "Expected source-figure hashes must be lowercase SHA-256 values.",
        )
    expected = (
        expected_hashes
        if args.source_figure_manifest_known or raw_hashes is not None
        else None
    )
    return await _inspect_target(
        Path(args.html),
        Path(args.out),
        scale=args.scale,
        viewport_width=args.width,
        viewport_height=args.height,
        expected_source_figure_sha256s=expected,
        capture_screenshot=not args.no_screenshot,
    )


def main() -> int:
    try:
        result = asyncio.run(inspect(parse_args()))
    except Exception as exc:  # noqa: BLE001 - CLI boundary always emits structured JSON
        result = _stage_error("inspection_failed", "main", str(exc), exc)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
