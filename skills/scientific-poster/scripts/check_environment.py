#!/usr/bin/env python3
"""Detect optional scientific-poster capabilities without changing the environment."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import poster_core  # noqa: E402
from posterlib.runtime.capability import (  # noqa: E402
    CAPABILITY_REQUIREMENTS,
    classify_chromium_failure,
    install_argv,
    probe_python_packages,
)


async def _probe_chromium() -> dict[str, Any]:
    """Launch a real page so an importable but unconfigured browser is detected."""

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(
                    "<!doctype html><title>scientific-poster probe</title>"
                )
                await page.close()
            finally:
                await browser.close()
    except Exception as exc:
        return {
            "available": False,
            "status": classify_chromium_failure(exc),
            "detail": repr(exc),
        }
    return {"available": True, "status": "ok"}


async def check_environment() -> dict[str, Any]:
    """Re-probe imports and Chromium for each invocation."""

    packages = probe_python_packages()
    components: dict[str, dict[str, Any]] = {
        name: {
            "available": available,
            "status": "ok" if available else "missing",
        }
        for name, available in packages.items()
    }
    components["chromium"] = (
        await _probe_chromium()
        if packages["playwright"]
        else {
            "available": False,
            "status": "blocked",
            "detail": "Install Playwright before probing Chromium.",
        }
    )
    capabilities: dict[str, dict[str, Any]] = {}
    for capability, required in CAPABILITY_REQUIREMENTS.items():
        missing = sorted(
            name
            for name in required
            if components[name]["status"] in {"missing", "blocked"}
        )
        failed = sorted(
            name for name in required if components[name]["status"] == "error"
        )
        capabilities[capability] = {
            "available": not missing and not failed,
            "missing": missing,
            "failed": failed,
            "install_argv": install_argv(capability) if missing else [],
        }
    failed = any(value["failed"] for value in capabilities.values())
    ready = not failed and all(value["available"] for value in capabilities.values())
    outcome = (
        "capability_probe_failed"
        if failed
        else "capabilities_ready"
        if ready
        else "missing_capability"
    )
    return poster_core.outcome_result(
        outcome,
        summary=(
            "All optional scientific-poster capabilities are ready."
            if ready
            else "One or more configured scientific-poster capabilities failed to start."
            if failed
            else "One or more optional scientific-poster capabilities are missing."
        ),
        python=sys.executable,
        components=components,
        capabilities=capabilities,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install only currently missing allowlisted optional dependencies, then re-probe",
    )
    return parser.parse_args(argv)


def _install_missing(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Run deduplicated allowlisted installer argv sequentially without a shell."""

    capabilities = report.get("capabilities")
    if not isinstance(capabilities, dict):
        return []
    commands: list[list[str]] = []
    for capability in sorted(capabilities):
        record = capabilities.get(capability)
        if not isinstance(record, dict) or record.get("available") is True:
            continue
        raw_commands = record.get("install_argv")
        if isinstance(raw_commands, list):
            commands.extend(
                [str(part) for part in argv]
                for argv in raw_commands
                if isinstance(argv, list) and argv
            )
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for argv in commands:
        marker = tuple(argv)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                check=False,
                timeout=600,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            records.append(
                {
                    "argv": argv,
                    "returncode": None,
                    "stdout": "",
                    "stderr": str(exc)[-4000:],
                    "error_kind": type(exc).__name__,
                }
            )
            break
        record = {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
        records.append(record)
        if completed.returncode != 0:
            break
    return records


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Probe, optionally install explicit allowlisted gaps, and re-probe on success."""

    report = await check_environment()
    if not args.install:
        return report
    attempts = _install_missing(report)
    successful = bool(attempts) and all(
        item.get("returncode") == 0 for item in attempts
    )
    if successful:
        report = await check_environment()
        install_summary = "Missing optional dependencies were installed and capabilities were re-probed."
    elif not attempts:
        install_summary = (
            "No allowlisted installer command applies to the current capability report."
        )
    elif attempts[-1].get("returncode") is None:
        install_summary = "Installation could not start or complete; check permissions and the recorded error."
    else:
        install_summary = "Installation stopped after the first command failure."
    return {
        **report,
        "install_attempts": attempts,
        "install_summary": install_summary,
    }


def main(argv: list[str] | None = None) -> int:
    """Print exactly one machine-readable capability report."""

    args = _parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        result = poster_core.outcome_result(
            "capability_probe_failed",
            summary="Capability probing failed.",
            error=repr(exc),
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
