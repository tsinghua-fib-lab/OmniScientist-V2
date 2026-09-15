# A Survey of Evaluation Techniques for General-Purpose and Scientific Agents

> **Supplementary Material**
>
> **Topic**: the current evaluation technology stack for general-purpose agents and scientific (research) agents — covering outcome, process, efficiency, and safety dimensions; general and domain benchmarks; harness capability evaluation; and memory capability evaluation.
>
> **Scope**: public benchmarks, academic papers, and industry practice through late 2025 and into 2026.
>
> **Audience**: agent framework authors, evaluation engineers, AI4Science researchers.
>
> **Version**: v1.0 (2026-09-08)

---

## Abstract

Between 2025 and 2026, agent evaluation has rapidly evolved from "run a few prompts and look at the output" into a distinct engineering discipline. This supplement is organized in four layers: general agent evaluation benchmarks, research-agent–specific evaluation, harness-as-object evaluation, and memory capability evaluation. The material also covers process- and trajectory-based evaluation methodology (to avoid the "lucky success" trap), practical deployment guidance, and a critical view of the current evaluation ecosystem. The core conclusion: evaluation has moved from a single score to an **Eval Flywheel** (execute → trace → assess → bad-case analysis → optimize → regress → canary → back to execute), and the harness choice often matters more than the model choice.

---

## 1 The Evaluation Landscape

### 1.1 Three stacked evaluation layers

| Layer | Question it answers | Typical metrics |
|---|---|---|
| **Outcome** | Did the task finish? | task success rate, Pass@k, Pass^k |
| **Process / Trajectory** | Was the path reasonable? | tool-call F1, trajectory similarity, PRM step scoring |
| **Efficiency / Safety** | Cost and risk | steps, tokens, cost, injection-interception rate, overreach count |

### 1.2 Four evaluation methods (combined in practice)

1. **Standardized benchmarks** — SWE-bench, GAIA, ScienceAgentBench, BLADE. Industry-wide comparison.
2. **Custom Golden Sets** — built from production logs, expert design, or bad-case replay. Closer to real business.
3. **LLM-as-Judge** — dimension + rubric scoring (faithfulness / relevance / adherence). Scalable semantic evaluation.
4. **Human evaluation** — A/B comparison, expert rating. Final arbiter for high-stakes calls.

### 1.3 Industry indicators

- 65% of enterprises already run agent workflows in production (Gartner 2025).
- VCs began requiring AI agent companies to present an eval harness before signing term sheets in 2025.
- Evaluation has upgraded to an Eval Flywheel: execute → trace → assess → bad-case analysis → optimize → regress → canary → back to execute.

---

## 2 General-Purpose Agent Benchmarks

### 2.1 Major benchmarks at a glance

| Benchmark | What it measures | Scale | SOTA (late 2025–2026) | Notes |
|---|---|---|---|---|
| **SWE-bench Verified** | Fix real GitHub issues | 500 human-verified tasks | 80%+ | Deprecated 2025-09 due to contamination; moving to SWE-bench Pro |
| **SWE-bench Pro** | Commercial repos + contamination defense | — | — | Replacement for Verified as of 2025-09 |
| **SWE-bench Multimodal** | Visual bug reports + UI tests | 517 tasks | — | 2025 expansion |
| **GAIA** | Multi-step reasoning + tools + multimodality | 466 real questions | L1 40–50%, L3 much lower | Humans 90%+; the most honest general-assistant benchmark |
| **GAIA2 (2025)** | Simulated long-horizon phone multi-app interaction | Significantly expanded | — | Meta Agents Research Environments |
| **WebArena** | Realistic web navigation | 812 tasks | 60%+ (CUGA 61.7%, OpenAI CUA 58.1%) | Humans 78.24% |
| **VisualWebArena** | WebArena + visual understanding | — | — | 2024 expansion |
| **OSWorld** | Ubuntu desktop GUI agent | Multi-task | OSWorld 2.0: strict 20.6% / partial 54.8% | Avg 27.25 checkpoints/task |
| **τ-bench / τ³-bench** | Customer-service tool calls + rule following | Airline / retail | Continual leaderboards | Pass^k for stability |
| **AgentBench** | 8 environments (OS/DB/KG/card/lateral/ALFWorld/shopping/browsing) | 4k dev + 13k test | GPT-4 leading | Long-standing multi-env benchmark |
| **Terminal-Bench 2.0** | Terminal-native ops workflow | — | 82.0% | Tests the full harness, not just the model |
| **WebShop** | E-commerce navigation | 12,087 instructions / 1.18M products | — | Text interface |
| **AgentBoard** | Multi-turn analytics board | — | — | Per-step evaluation |
| **MCPEval (2025)** | Unified MCP-protocol evaluation | — | — | Liu et al., 17 Jul 2025 |

### 2.2 Critical reading (how to interpret scores)

- The same benchmark can swing 20–40 percentage points across harnesses.
- A reported score must also report: harness, judge model, temperature, retry budget, model version.
- SWE-bench "hidden tests" are actually visible in the repo; contamination has to be suppressed with adversarial curation.
- Real-world close rate (real users closing real tickets) is usually much lower than the SWE-bench score.

---

## 3 Scientific-Agent–Specific Evaluation

### 3.1 Major benchmarks

| Benchmark | Source | Task design | Metrics | Key findings |
|---|---|---|---|---|
| **ScienceAgentBench** | ICLR'25, OSU (Chen et al.) | 102 tasks extracted from 44 top-journal papers, 4 disciplines | VER (valid execution rate), SR (success rate), CodeBERTScore, API cost | Best 32.4% independent / 34.3% with expert knowledge; o1 + self-debug 42.2% but 10× cost |
| **BLADE** | EMNLP'24, UW (Gu et al.) | 12 real research questions; open-ended data analysis | Decision-based F1 (precision + coverage@10) | GPT-4o agent best at 44.8 F1; models weak on statistical model selection |
| **SciAgent** | 2025 (Li et al.) | Olympiad-level reasoning (IMO/IMC/IPhO/CPhO) + HLE | Official olympiad grading + LLM/expert dual validation | IMO 2025: 36/42 (> average gold 35.94); IMC 2025: 100/100 |
| **MLAgentBench** | 2023 | ML research automation | Performance gain, novelty | Long-standing ML-agent benchmark |
| **DS-Agent / DABench / QRData** | 2024 | Data-science sub-tasks | Single-answer evaluation | Superseded by BLADE on "open-ended + decision-level" |
| **Data Interpreter** | 2024 | Data analysis | Automatic evaluation | — |
| **HAL (Holistic Agent Leaderboard)** | 2025 (Kapoor et al.) | Model × scaffold × benchmark 3D matrix | 21,730 rollouts × 9 models × 9 benchmarks | Surfaces counter-intuitive findings like "high reasoning effort reduces accuracy" |
| **ResearchBench** | 2025 | Reproducing NeurIPS papers | Reproduction quality | — |
| **Agent's Last Exam** | 2026 (Sun et al.) | 1,000+ tasks / 55 sub-domains / 13 industries | Gate-and-score | Hardest tier average pass rate < 1% |

### 3.2 Methodological differences in scientific-agent evaluation

1. **Decision-level vs answer-level evaluation.** BLADE decomposes the analysis into "conceptual variable definition → data transformation → statistical model" and matches each step to ground truth, with partial credit.
2. **Multimodal-fused evaluation.** SciAgent explicitly evaluates image + symbol + text coordination.
3. **End-to-end vs sub-task.** ScienceAgentBench argues for sub-task-level evaluation (data loading, statistical modeling, visualization) before talking about end-to-end automation.
4. **Contamination defense.** Drop some data points from the test split and replace supervised labels with dummy values (-1).
5. **Domain expert involvement.** At least 9 experts per task across multiple review rounds.

### 3.3 ScienceAgentBench detailed metrics

- **Valid Execution Rate (VER)**: fraction of programs that run without error and produce correctly named outputs.
- **Success Rate (SR)**: fraction of executions that meet domain-specific success criteria.
- **CodeBERTScore (CBS)**: similarity between generated code and the reference annotation.
- **API Cost**: dollar cost of running and scoring.

Anti-contamination strategy:
1. Randomly drop a few data points from the test split.
2. Replace supervised task test labels with dummy values (-1).

---

## 4 Harness Capability Evaluation

**Harness definition**: the "operating system" around the model — tool calls, trajectory recording, error recovery, context management, safety guardrails. Starting in 2025 it is evaluated as an object in its own right.

### 4.1 Harness-Bench (arXiv:2605.27922, 2026)

The first public benchmark that takes the harness itself as the unit under test.

**Design**:
- **106 tasks × 6 configurable harnesses × 8 backend models = 5,088 trajectories** (plus 106 Codex trajectories, 5,194 total).
- Task-design four principles: realism / solvability / oracle-checkability / integrity.
- Multiplicative scoring formula:
  ```
  TaskScore_i = Security_i × Completion_i × (Robustness_i + ToolUse_i + Consistency_i) / 3
  ```
- Security is a 0/1 gate; a security violation zeroes the score.
- Process dimensions are scored by a unified LLM judge (claude-sonnet-4.6) against a trace-based rubric.

**Key findings**:
- Same tasks, same models: total scores differ by **23.8 percentage points** between harnesses (NanoBot 76.2 vs OpenClaw 52.4).
- NanoBot has the highest configurable-harness score while spending fewer tokens than 4 competitors — long trajectories ≠ good results.
- Codex 80.4 (highest overall) is a model-bound coding agent, so reported separately.
- Every harness scored 100% on the security gate.

### 4.2 The three-layer harness test method

| Layer | Coverage | Method |
|---|---|---|
| **Layer 1: Deterministic unit** | 30–40% of behavior | Tool schema, argument parsing, state machines → standard unit tests |
| **Layer 2: LLM-as-Judge soft quality** | Semantics, style, faithfulness | 0.0–1.0 score; average across multiple runs; 0.5–0.8 is "soft failure" |
| **Layer 3: End-to-end trajectory** | Step efficiency, error recovery, loop detection | 20% of test cases dedicated to failure paths |

### 4.3 Meta-evaluation (Meta-Reward) — adding a harness to the judge

**Problem**: an LLM judge itself needs harness tuning, otherwise it is an underspecified reward model.

**Meta-Reward method** (Canvas, 2026):
- Fix the judge model parameters; optimize the surrounding evaluator harness.
- Optimizable: trace view, policy context, rubric, decision process, scoring logic.
- On τ³-bench airline, given a Haiku 4.5 judge, tuning the harness raised held-out agreement from **52.8% to 78.2%**; best-of-N selection adds another +30.2 points.
- On Plan-RewardBench: 60.5% → 72.4% (+11.9 points).

This confirms HAL's finding that "high reasoning effort reduces accuracy" — redundant thinking on the evaluation side is also noise.

### 4.4 Empirical case: why harness evaluation matters now

- Braintrust 2026 report: agents evaluated on final output only pass 20–40% more cases than trajectory-level evaluation.
- A support-ticket agent that switched to outcome-based scoring dropped from 92% pass rate to 41%, but production incidents dropped significantly.
- Classic trap: grading the narration — the agent writes a beautiful "success narrative" while the tool call actually failed.

---

## 5 Memory Capability Evaluation

Before 2024 "agent memory = shove the conversation into the context window"; by 2026 it is its own benchmark track.

### 5.1 Three standard benchmarks

| Benchmark | Source | Scale | What it measures | Why it's hard |
|---|---|---|---|---|
| **LoCoMo** | ACL'24 (Maharana et al.) | 1,540 questions / 300 turns / 35 sessions / ~9,000 tokens | single-hop, multi-hop, temporal, open-domain | 35 sessions spanning days and weeks |
| **LongMemEval** | ICLR'25 (Wu et al.) | 500 questions / ~115K-token history | single-session (user/assistant/preference), knowledge update, temporal, multi-session | Knowledge updates and multi-session reasoning are the hard parts |
| **BEAM** | ICLR'26 (Tavakoli et al.) | 100 sessions / 2,000 questions / 1M–10M tokens | 10 categories: preference / instruction / info-extraction / knowledge-update / multi-session / summarization / temporal / event-ordering / abstention / contradiction | Cannot be solved by throwing context at it |
| **MemoryArena** | 2026 | Memory use in agentic tasks | Task-completion uplift | On the roadmap |

### 5.2 The five core metric dimensions

| Metric | Meaning |
|---|---|
| **BLEU** | Token-level similarity to ground truth |
| **F1** | Precision / recall over response tokens |
| **LLM score** | LLM-judge binary correctness |
| **Token consumption** | Total tokens per query |
| **Latency** | Retrieval + response wall-clock time |

### 5.3 Current SOTA comparison

| System | LoCoMo | LongMemEval | BEAM-1M | BEAM-10M |
|---|---|---|---|---|
| Mem0 (new algorithm, 2026) | 92.5 | 94.4 | 64.1 | 48.6 |
| Zep | 94.7 | 90.2 | — | — |
| ByteRover | — | 92.8 (LongMemEval-S) | — | — |
| MemoryLake | 94.03 | — | — | — |
| Dakera | 88.2 | — | — | — |
| Mem0 (old algorithm) | 71.4 | 67.8 | — | — |
| LlamaIndex | 54.8 | 59.0 | — | — |
| LangChain | 51.9 | 59.0 | — | — |
| **LLM baseline (no memory)** | 50.4 | 57.6 | — | — |
| Mem0 OSS | 0.0 | 32.4 | — | — |

**Note**: the same system can score anywhere from 38% to 92% on LoCoMo depending on the harness. Scores are not directly comparable.

### 5.4 LongMemEval category breakdown (Mem0 report)

| Category | Mem0 score |
|---|---|
| Single-session (user) | 98.6 |
| Single-session (assistant) | 98.2 |
| Knowledge update | 93.6 |
| Multi-session | 88.0 |

Single-session is near saturation; knowledge update and multi-session have significant headroom.

### 5.5 BEAM category breakdown (Mem0 report)

| Category | 1M | 10M |
|---|---|---|
| preference_following | 88.3 | 90.4 |
| instruction_following | 85.2 | 82.5 |
| information_extraction | 70.0 | 56.3 |
| knowledge_update | 65.0 | 75.0 |
| multi_session_reasoning | 65.2 | 26.1 |
| summarization | 63.5 | 46.9 |
| temporal_reasoning | 61.8 | 16.3 |
| event_ordering | 53.6 | 20.2 |
| abstention | 52.5 | 40.0 |
| contradiction_resolution | 35.7 | 32.5 |

At the 10M scale, `temporal_reasoning` and `event_ordering` are open industry-wide problems.

### 5.6 Open problems (mem0 2026 report)

- Cross-session identity
- Temporal abstraction at scale
- Memory staleness

### 5.7 Important caveats

- LoCoMo answer keys have ~6.4% errors.
- The default GPT-4o-mini judge has a ~63% false-positive rate.
- "LLM baseline beats most dedicated memory systems": just stuffing the context window beats compression / distillation in many scenarios.

### 5.8 Recommended measurement methodology

1. **First measure Context Completeness** (COMPLETE / PARTIAL / INSUFFICIENT) — the retrieval layer is independent of the LLM.
2. **Then measure Answer Correctness** — end-to-end.
3. **Then measure Latency + Token** — production thresholds.
4. **Test-set design**: 3–5 target interactions → expand to 10+ variants → spread across multiple sessions → include temporal variation cases.
5. **Add background data and noise**: bury relevant facts in a larger graph or JSON/business data to simulate real retrieval conditions.

### 5.9 Real effective context window

- NVIDIA RULER: effective context is 50–65% of the nominal window.
- Chroma "context rot" study: 18 frontier models, all degrade as length grows.
- Lost-in-the-middle effect: accuracy drops > 30% when relevant facts sit in the middle of the window.

---

## 6 Process / Trajectory Evaluation: Avoiding the "Narration Trap"

The most common scientific-agent failure mode: **the answer is right but the path is wrong** ("lucky success" / "shallow success").

### 6.1 Milestone / Checkpoint scoring

| Benchmark | Method | Strength |
|---|---|---|
| **OSWorld 2.0** | ~27.25 checkpoints per task | Strict binary 20.6% / partial 54.8% exposes information loss |
| **WindowsWorld (2026)** | 181 tasks × avg 5 sub-goals | 78% span multiple apps |
| **Agent's Last Exam** | Gate-and-score (pass a gate, then weighted rubric) | When full pass < 1%, can still locate failure points |
| **ClawTrack** | Outcome + Process dual grader | Safety is multiplicative, not additive |

### 6.2 ClawTrack scoring formulas

**Task score**:
```
s_task = s_safe · (α·s_comp + β·s_rob)
```
where α=0.80, β=0.20, and s_safe is a 0/1 gate.

**Process score** (per turn):
```
s_proc^(t) = g_t · (w_e·e_t + w_i·i_t + w_v·v_t)
```
- g_t = goal alignment (gate; if misaligned the turn is zeroed)
- e_t = efficiency, w_e = 0.40
- i_t = information utilization, w_i = 0.40
- v_t = result verification, w_v = 0.20

### 6.3 Tool-call-level evaluation metrics

- **Tool F1**: precision / recall / F1 against a reference multiset.
- **Step Efficiency** = optimal_steps / actual_steps (capped 1.0).
- **Tool Call Accuracy**: argument-level.
- **Recovery Count**: number of error recoveries.

### 6.4 Process Reward Model (PRM)

- Step-by-step scoring (Lightman et al., 2023 onward).
- Core signal for RL post-training.
- Milestone-aware training: BEACON, MiRA, ADMIRE, GiGPO
  - Use repeat states / explicit decomposition / learned milestones to give long-horizon rollouts dense credit.
  - GiGPO extracts step-level comparison groups from shared anchor states.

---

## 7 Practical Deployment: A Four-Phase Path

### 7.1 Week 1–2 — Foundation

- 5–10 priority intents + a success definition for each.
- Turn on tracing (plan / tools / timings / cost / errors).
- Start with a 50–200-row Golden Set.
- Run the evaluator + a simple scorecard overnight.

### 7.2 Week 3–4 — Automation

- CI/CD gate: safety / task-success regressions block deploys.
- Dashboard by version / env / date / intent.
- HITL review on high-risk sessions.

### 7.3 Month 2 — Learning

- Optimize prompt / policy by delta; use fine-tuning cautiously.
- Add policy checks before high-risk tools.
- Alerts for failure loops, latency spikes, cost drift.
- A/B or shadow experiments.

### 7.4 Month 3+ — Scale

- Automated scenario generation (adversarial + counterfactual).
- Cross-session evaluation (memory dimension).
- Red-team drills / governance.

### 7.5 Scorer division of labor (key engineering principle)

| Type | Best for | Strengths | Risks |
|---|---|---|---|
| **Code / rule scorer** | Tool schema, state changes, forbidden actions | Stable, cheap, reproducible | Can't cover complex semantics |
| **LLM-as-Judge** | Explanation quality, policy adequacy | Scalable, close to expert judgment | Biased, needs calibration |
| **Human scorer** | Unstable business criteria / high-stakes final calls / calibration | Closest to business consensus | Expensive, doesn't scale |

**Core principles**:
- Rules check "hard conditions" — anything expressible in code is primarily judged by rules.
- LLMs check "soft semantics" — explanation quality and policy adequacy are supplemented by LLM-as-Judge.
- Humans check "the final call" — business-standard confirmation, conflict resolution, high-stakes arbitration.

**Engineering requirements for LLM-as-Judge**:
- Explicit scoring criteria: each tier has executable standards.
- Output a Reason: easier bad-case clustering and root-causing.
- Few-shot examples: include boundary samples and decision logic.
- Periodic calibration: reach ~85% agreement with human review before daily automation.
- Bias mitigation: verbosity, position, self-style; use multiple adversarial models.
- Judge model and evaluated agent must be from different sources.

---

## 8 A Skeptical View: Limits of Evaluation

### 8.1 Transferability of simulated environments

- WebShop, ALFWorld etc. sit close to the LLM pre-training distribution.
- Scores on these environments don't transfer to unseen real UIs.
- A WebShop 80% agent may completely fail on a novel e-commerce site.

### 8.2 SWE-bench's drama and warnings

- The 2024-early ~2% → 2025-late ~60%+ trajectory is real, and is one of the fastest in program-synthesis history.
- But:
  - **Gameable**: "hidden tests" are visible in the repo; agents that read the test file are artificially inflated.
  - **Not the same as deployed close rate**: Cognition / Sourcegraph / Aider real-world close rates are significantly below their benchmark numbers.
  - **Insensitive to solution quality**: a fix that introduces regressions scores the same as a clean fix.

### 8.3 GAIA's honest signal

- The first benchmark where leading LLM agents on assistant-realistic tasks are not close to humans.
- Lesson: for many tasks users care about, the honest human baseline is beyond current agent capability.

### 8.4 Three classes of agent error

| Error type | Description | Fix |
|---|---|---|
| **Specification errors** | User request is ambiguous; agent silently commits to a wrong interpretation | Force the agent to first produce an explanation and clarification questions (SpeakRL, Acikgoz et al. 2025 can lift this by ~20pp) |
| **Tool-call errors** | Argument type mismatch / semantic error | Schema validation, retry-with-feedback, constrained decoding |
| **Reasoning errors** | Invalid reasoning chain | Reflexion is a structured re-sampling method, not an oracle; systematic errors still propagate |

### 8.5 Deployment vs benchmark

- The benchmark-to-deployment gap is a class of agent-reliability fact, not a small blemish.
- Even with schema validation, 5–15% of tool calls in production contain semantic errors.

---

## 9 Engineering Supplement to Evaluation Methodology

### 9.1 Trajectory vs outcome grading

- **Outcome grading**: is the final environment state correct? Objective but coarse.
- **Transcript grading**: is the path reasonable? Can identify lucky passes or wasteful paths.
- Example: fixing a failing test — agent A changes the function and the test goes green; agent B comments out the test and it also goes green. Outcome passes for both; transcript separates them.
- Practice: outcome for large-scale pass/fail, transcript to explain failures.

### 9.2 The four components of agent eval (Anthropic framework)

1. **Task**: a single test, with explicit input and success criteria.
2. **Trial**: one attempt at the task.
3. **Grader**: the scoring logic.
4. **Evaluation Harness**: the end-to-end runtime infrastructure.

### 9.3 Trace standardization

Minimum recommended fields for a trace:
- `session_id`
- `turn`
- `user_intent`
- `agent_plan`
- `chosen_tools`
- `tool_calls` (name, args, latency, success)
- `memory_reads / writes`
- `response_summary`
- `costs`
- `timings`
- `errors`

### 9.4 Building an eval dataset

1. **Production log sampling**: keep real user inputs; have humans label the expected path.
2. **Expert design**: cover normal / abnormal / boundary / safety.
3. **LLM-assisted generation**: batch-generate, then human review.
4. **Bad-case replay**: production failures auto-enter the eval set.

Best practice: combine all four.

### 9.5 Calibrating an LLM-as-Judge

- ≥ 85% agreement with humans before daily automation.
- Judge bias mitigation:
  - Verbosity bias
  - Position bias
  - Self-style bias
- Multi-model adversarial scoring.
- Judge model and evaluated agent must be from different sources.

### 9.6 Regression test triggers

- LLM model version update.
- System prompt change.
- Tool / skill interface or behavior change.
- Memory store change (entries added or removed).

### 9.7 Maturity model (CMMI-inspired)

| Level | Characteristic | Use |
|---|---|---|
| **L0 Prototype** | Runs, no engineering rigor | Internal experiments, POCs |
| **L1 Usable** | Basic error handling, logging, version control | Small-scale internal use |
| **L2 Reliable** | Automated evaluation, regression testing, monitoring, canary, long-term memory, task planning | User-facing production |
| **L3 Optimizable** | Feedback-driven continuous improvement, bad-case analysis, A/B testing, multi-agent collaboration | Scaled operations |
| **L4 Scalable** | Standardized skill ecosystem, unified agent platform, MCP protocol, cross-team reuse | Enterprise AI platform |

---

## 10 One-Sentence Summary

- **Trend**: evaluation has evolved from "scoring" to an "engineering discipline"; a 3-score combo (task success + cost + safety) is the minimum, and a golden set wired into a CI gate is what makes production "real".
- **Scientific agents' specialness**: "many solutions + only correct processes count" — BLADE / ScienceAgentBench / SciAgent are required reading.
- **Harness**: now evaluated as an object in its own right. Harness-Bench shows a 23.8-point gap between harnesses — choosing a harness often matters more than choosing a model.
- **Memory**: a mature standalone benchmark track (LoCoMo / LongMemEval / BEAM), but scores are not comparable — you must read the harness + judge + seed together.
- **Caution**: high SWE-bench scores don't predict production usability; "lucky success" needs trajectory-level scoring to catch; redundant thinking on the evaluation side is also noise.

---

## References

### General evaluation

1. Jimenez, C.E. et al. (2024). "SWE-bench: Can Language Models Resolve Real-World GitHub Issues?" arXiv:2310.06770.
2. Mialon, G. et al. (2023). "GAIA: A Benchmark for General AI Assistants." arXiv:2311.12983.
3. Zhou, S. et al. (2024). "WebArena: A Realistic Web Environment for Building Autonomous Agents." arXiv:2307.13854.
4. Liu, X. et al. (2024). "AgentBench: Evaluating LLMs as Agents." ICLR 2024.
5. Yao, S. et al. (2022). "ReAct: Synergizing Reasoning and Acting in Language Models." ICLR 2023.
6. Shinn, N. et al. (2023). "Reflexion: Language Agents with Verbal Reinforcement Learning." NeurIPS 2023.

### Scientific-agent evaluation

7. Chen, Z. et al. (2025). "ScienceAgentBench: Toward Rigorous Assessment of Language Agents for Data-Driven Scientific Discovery." ICLR 2025. https://github.com/OSU-NLP-Group/ScienceAgentBench
8. Gu, K. et al. (2024). "BLADE: Benchmarking Language Model Agents for Data-Driven Science." EMNLP 2024 Findings. https://blade-bench.github.io/
9. Li et al. (2025). "SciAgent: A Unified Multi-Agent System for Generalistic Scientific Reasoning." arXiv:2511.08151.
10. Huang, Q. et al. (2024). "MLAgentBench: Evaluating Language Agents on Machine Learning Experimentation." arXiv:2310.03302.
11. Guo, S. et al. (2024). "DS-Agent: Automated Data Science by Empowering Large Language Models with Case-Based Reasoning." ICML 2024.
12. Hu, X. et al. (2024). "InfiAgent-DABench: Evaluating Agents on Data Analysis Tasks." ICML 2024.
13. Kapoor, S. et al. (2025). "Holistic Agent Leaderboard (HAL): The Missing Infrastructure for AI Agent Evaluation." arXiv:2510.11977.
14. Sun et al. (2026). "Agent's Last Exam."

### Harness and meta-evaluation

15. (2026). "Harness-Bench: Measuring Harness Effects across Models in Realistic Agent Workflows." arXiv:2605.27922.
16. Sleiman et al. (2026). "Meta-Reward: Reward Modeling as Harness Optimization." Canvas Research. https://www.canvas.inc/research/reward-models
17. (2024). "Evaluation-Driven Development of LLM Agents: A Process Model and Reference Architecture." Xia et al.
18. Anthropic. "Building Effective Agents." & "Agent Evals Guide."

### Memory evaluation

19. Maharana, A. et al. (2024). "Evaluating Very Long-Term Conversational Memory of LLM Agents." ACL 2024. arXiv:2402.17753.
20. Wu, D. et al. (2025). "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory." ICLR 2025. arXiv:2410.10813.
21. Tavakoli, M. et al. (2026). "BEAM: Benchmarking Agent Memory at Scale." ICLR 2026.
22. Mem0 (2025). "Mem0: A Memory Layer for AI Agents." ECAI 2025. arXiv:2504.19413.
23. (2025). "State of AI Agent Memory 2026: Benchmarks, Architectures & Production Gaps." mem0.ai.
24. Sumers, T. et al. (2024). "CoALA: Cognitive Architectures for Language Agents."

### Process / trajectory evaluation

25. Lightman, H. et al. (2023). "Let's Verify Step by Step." arXiv:2305.20050.
26. (2026). "Milestone-Based Evaluation and Training for Long-Horizon AI Agents." Snorkel AI.
27. (2025). "ClawTrack: Towards Trace-Level Evaluation and Improvement of Real-World Autonomous Agents." arXiv:2607.28037.
28. OSWorld 2.0. https://osworld.dev/
29. WindowsWorld (2026). arXiv:2604.27776.
30. (2025). "GiGPO: Grouped-in-Group Policy Optimization." (state-based step-level comparison groups)

### Evaluation frameworks and surveys

31. Mohammadi, H. et al. (2025). "Evaluation and Benchmarking of LLM Agents: A Survey."
32. Yehudai, A. et al. (2025). "Evolutionary Perspectives on the Evaluation of LLM-Based AI Agents: A Comprehensive Survey."
33. (2025). "Survey on Evaluation of LLM-based Agents." ownyourai.com.
34. (2024). "AgentBoard: An Analytical Evaluation Board of Multi-turn LLM Agents."
35. (2025). "MCPEval: Automatic MCP-based Deep Evaluation for AI Agent Models." Liu et al., 17 Jul 2025.
36. (2024). "TestAgent: A Framework for Domain-Adaptive Evaluation of LLMs via Dynamic Benchmark Construction and Exploratory Interaction."

### Skeptical view

37. iohanngrig. (2025). "LLM agents as decision systems: a skeptic's guide." iohanngrig.github.io/research-notes/05-llm-agents.
38. Skygena (2025). "Year in review — agent evaluation is the discipline that finally grew up."

### Safety / governance

39. OWASP. (2025). "Top 10 for Agentic Applications 2026" (ASI01–ASI10).
40. OWASP. (2024). "LLM Top 10" (2025 edition).
41. Singapore IMDA. (2026). "Model AI Governance Framework for Agentic AI." Version 1.5.
42. Five Eyes cybersecurity agencies. (2026). "Careful Adoption of Agentic AI Services."

### Industry reports

43. Gartner (2025). 40% of enterprise applications will integrate task-specific AI agents by end-2026.
44. McKinsey (2025). "State of AI Global Survey."
45. Braintrust (2026). Series B $80M at $800M valuation, built on a production-trace-to-regression-test pipeline.
46. Penfield Labs (2026). "LOCOMO audit." dev.to/penfieldlabs.

---

## Appendix A: Complete Benchmark Index (by use case)

### A.1 Coding / software engineering
- SWE-bench / SWE-bench Verified / SWE-bench Pro / SWE-bench Multimodal / SWE-rebench (21,000+)
- HumanEval / MBPP (basic coding ability)
- RepoBench / CrossCodeEval (repository-level)

### A.2 General assistants
- GAIA / GAIA2 / AgentBench / AgentBoard / WebArena / VisualWebArena / WebShop / Mind2Web

### A.3 Desktop / GUI
- OSWorld / OSWorld 2.0 / WindowsWorld / ScreenAgent

### A.4 Tool calling / function calling
- τ-bench / τ³-bench / ToolBench / API-Bank / MCPEval

### A.5 Scientific / data science
- ScienceAgentBench / BLADE / SciAgent / MLAgentBench / DS-Agent / DABench / QRData / Data Interpreter / ResearchBench

### A.6 Reasoning / math / knowledge
- MATH / GSM8K / AIME / IMO-Bench / HLE (Humanity's Last Exam) / FrontierMath / TheoremQA

### A.7 Multimodal
- MMMU / MathVista / ChartQA / DocVQA / BLINK

### A.8 Alignment / safety
- MACHIAVELLI / DialogGuard / ALI-Agent / HarmBench / JailbreakBench

### A.9 Multi-agent
- MultiAgentBench / ChatEval / ChatDev / MetaGPT-Eval

### A.10 Memory
- LoCoMo / LongMemEval / BEAM / MemoryArena

### A.11 Harness / framework evaluation
- Harness-Bench / HAL (Holistic Agent Leaderboard) / AgentBoard

---

## Appendix B: Key Numbers Quick Reference

| Item | Value |
|---|---|
| GAIA human level | 90%+ (L1/L2/L3) |
| GAIA top agent | L1 40–50%, L3 much lower |
| SWE-bench Verified SOTA | 80%+ (vendor-reported) |
| WebArena top agent | 60%+ (CUGA 61.7%) |
| WebArena human | 78.24% |
| OSWorld 2.0 strict SOTA | 20.6% |
| OSWorld 2.0 partial SOTA | 54.8% |
| Terminal-Bench 2.0 SOTA | 82.0% |
| ScienceAgentBench best | 32.4% independent / 42.2% self-debug |
| BLADE best F1 | 44.8% (GPT-4o agent) |
| SciAgent IMO 2025 | 36/42 (> human gold 35.94) |
| Harness gap (Harness-Bench) | 23.8 pp (NanoBot vs OpenClaw) |
| Meta-Reward τ³ lift | 52.8% → 78.2% (+25.4) |
| Meta-Reward Plan-RewardBench lift | 60.5% → 72.4% (+11.9) |
| Effective context ratio | 50–65% (RULER) |
| Lost-in-middle accuracy loss | > 30% |
| LLM baseline (no memory) LoCoMo | 50.4% |
| LoCoMo answer-key error rate | ~6.4% |
| GPT-4o-mini judge FPR | 63% |
| Enterprise agent deployment rate | 65% (Gartner 2025) |
| Enterprises actively scaling | 23% (McKinsey 2025) |
| Braintrust Series B | $80M at $800M (2026-02) |

---

*This document is supplementary material for the OmniScientist project. For citations, please refer to the original benchmark papers.*
