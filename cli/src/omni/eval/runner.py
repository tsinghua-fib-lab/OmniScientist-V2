"""Run a scenario corpus and aggregate the capability scoreboard.

Scenarios are executed **sequentially**: each one isolates the process
environment (OMNI_HOME/HOME/CWD), so they must not overlap.
"""

from __future__ import annotations

from pathlib import Path

from omni.eval.harness import run_scenario
from omni.eval.report import BenchmarkReport
from omni.eval.scenarios import Scenario, load_scenarios


async def run_benchmark(
    scenarios: list[Scenario] | None = None,
    *,
    source: Path | None = None,
    tag: str | None = None,
    persona: str | None = None,
) -> BenchmarkReport:
    """Score a corpus offline and return a :class:`BenchmarkReport`.

    Pass ``scenarios`` directly, or a ``source`` file/dir to load from (defaults
    to the bundled corpus). ``tag``/``persona`` filter the loaded corpus.
    """
    if scenarios is None:
        scenarios = load_scenarios(source, tag=tag, persona=persona)
    report = BenchmarkReport()
    for scenario in scenarios:
        report.results.append(await run_scenario(scenario))
    return report


__all__ = ["run_benchmark"]
