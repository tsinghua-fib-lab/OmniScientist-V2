# OmniScientist documentation

| Document | What it covers |
|---|---|
| [getting-started.md](getting-started.md) | Install, configure a model, first run, research workflow, IM channels |
| [commands.md](commands.md) | Full CLI reference + research commands + REPL slash commands |
| [memory.md](memory.md) | Long-term memory layers, global store, recall, notebook, and memory CLI |
| [skills.md](skills.md) | Install, trust, discover, implicitly or explicitly invoke, and adapt third-party skills; Claude Code/Codex/OpenClaw comparison |
| [autosota.md](autosota.md) | Explicit install, private configuration, and foreground launch of the external AutoSOTA CLI |
| [autosota-user-guide.md](autosota-user-guide.md) | End-to-end installation, configuration, validation, production use, Python 3.11, and security guidance |
| [uninstall.md](uninstall.md) | Ownership-aware program, integration, service, credential, and data removal |
| [cli-validation-guide.md](cli-validation-guide.md) | End-to-end CLI and REPL validation scenarios, including 1~7 workflow capability examples |
| [agent-validation-guide.md](agent-validation-guide.md) | Agent, skill, workflow, research provenance, memory, storage, and compatibility validation |
| [user-walkthrough-cases.md](user-walkthrough-cases.md) | **Source of truth** for user-perspective walkthroughs and functional validation: named user prompts, coverage inventory (every CLI group and built-in skill), output-format families, survey packs, third-party skill add/trust/invoke, long-horizon, multi-execution, and CLI/REPL |
| [testing-and-evaluation.md](testing-and-evaluation.md) | Pytest, `omni eval`, coverage, research-quality checks, and black-box harness |
| [external-benchmarks.md](external-benchmarks.md) | Natural-language black-box reliability, AstaBench, BioMysteryBench, and scoring boundaries |
| [agent-runtime-harness.md](agent-runtime-harness.md) | Runtime invariants, Plan/Review modes, hooks, steer, DAG recovery, isolation, quality eval, and domain packs |
| [harness-dimensions.md](harness-dimensions.md) ([中文](harness-dimensions_cn.md)) | Nine-dimension technical analysis of the agent harness: main loop, tool layer, context manager, permissions/sandbox, subagent dispatch, session/state, observability, hooks, error recovery — with `file:line` citations into `cli/src/omni/` |
| [harness-architecture.md](harness-architecture.md) ([中文](harness-architecture_cn.md)) | Macro view of the same nine responsibilities: what each one is, the design space, the choices the harness makes within each, and the cross-cutting properties (model-is-not-authority, reach vs exposure, host-owned analog, durable cancel, grant-shaped approvals, fail-closed, M1 isolation) that hold the system together |
| [request-lifecycle.md](request-lifecycle.md) ([中文](request-lifecycle_cn.md)) | Longitudinal view: one user request through six phases (Intake → Plan → Execute → Settle → Render → Persist), what state crosses each boundary, how the harness keeps the system consistent under failure |
| [agent-evaluation-survey.md](agent-evaluation-survey.md) ([中文](agent-evaluation-survey_cn.md)) | Supplementary survey of general-purpose and scientific agent evaluation techniques — outcome / process / efficiency / safety layers, general and domain benchmarks, harness evaluation, memory evaluation, the Eval Flywheel, and current ecosystem limitations |
| [architecture.md](architecture.md) | As-built architecture, request flow, storage model, research subsystem |
| [research-agent-design.md](research-agent-design.md) | **Product + architecture design & positioning**: capabilities, vs Claude Code/Codex, why it fits research, and a comparison with other open-source research agents |
| [../../skills/docs/authoring.md](../../skills/docs/authoring.md) | How to write skills (prompt / python / cli-exec / async) |
| [compatibility.md](compatibility.md) | Claude Code / Codex / OpenClaw / MCP interop (both directions) |

New here? Start with **getting-started.md**, use **skills.md** for the complete
skill lifecycle, then read **research-agent-design.md** for the big picture
(and **compatibility.md** if you use Claude Code/Codex). Contributors should read
**architecture.md**, **agent-runtime-harness.md**, and
[`../CONTRIBUTING.md`](../CONTRIBUTING.md).

### Document naming convention

- `xxx.md` is the English (canonical) version.
- `xxx_cn.md` is the Chinese version, kept next to its English counterpart so
  readers can pick the language they prefer. Translations preserve code
  references (`file:line`), arXiv ids, benchmark names, and technical
  vocabulary in English; only the surrounding prose is translated.
- When a new translatable document lands, the second-language version should
  follow within the same commit (or in a paired follow-up commit) so the
  index above never lists a one-sided pair.
