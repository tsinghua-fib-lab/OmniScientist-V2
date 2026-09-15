# 智能体与科研智能体评测技术综述

> **Supplementary Material / 补充材料**
>
> **主题**：当前对通用智能体（Agent）与科研智能体（Research / Scientific Agent）的评测技术体系，覆盖结果/过程/效率/安全四维度、通用与领域基准、Harness 能力评测、Memory 能力评测。
>
> **范围**：截至 2025 年末–2026 年的公开基准、学术论文与产业实践。
>
> **适用读者**：智能体框架作者、评测工程师、AI4Science 研究者。
>
> **版本**：v1.0（2026-09-08）

---

## 摘要

智能体评测在 2025–2026 年从"跑几个 prompt 看效果"快速演化为一套独立的工程学科。本补充材料围绕四个层次展开：通用智能体评测基准、科研智能体专门评测、Harness 能力评测（执行框架本身）、Memory 能力评测（长时记忆）。材料同时给出过程/轨迹评测方法学（避免"草率成功"陷阱）、实战落地建议，以及对当前评测体系局限性的批判性观察。核心结论：评测已从单一分数演化为 **Eval Flywheel**（执行 → Trace → 评估 → Bad Case → 优化 → 回归 → 灰度发布 → 回到执行），且 harness 选择的影响在很多场景下超过模型选择。

---

## 1 评测体系整体图景

### 1.1 三层叠加的评测层级

| 层级 | 解决的问题 | 典型指标 |
|------|-----------|---------|
| **结果层（Outcome）** | 任务最终是否完成 | 任务成功率、Pass@k、Pass^k |
| **过程层（Process / Trajectory）** | 走的路径是否合理 | 工具调用 F1、轨迹相似度、PRM 步级打分 |
| **效率/安全层（Efficiency / Safety）** | 代价与风险 | 步骤数、Token、Cost、注入拦截率、越权次数 |

### 1.2 四类评测方法（相互组合使用）

1. **标准化基准**：SWE-bench、GAIA、ScienceAgentBench、BLADE 等 —— 行业横向对比
2. **自建评测集（Golden Set）**：从生产日志、专家设计、Bad Case 回流构建 —— 贴近真实业务
3. **LLM-as-Judge**：按维度 + Rubric 评分（faithfulness / relevance / adherence）—— 可扩展的语义评估
4. **人工评估**：A/B 对比、专家打分 —— 高风险终判

### 1.3 行业现状指标

- 2025 年 VCs 在 term sheet 签署前开始要求 AI Agent 公司出示 eval harness
- 65% 的企业已在生产环境运行 agent 工作流（Gartner 2025）
- 评测已升级为 Eval Flywheel：执行 → Trace → 评估 → Bad Case 分析 → 优化 → 回归 → 灰度发布

---

## 2 通用智能体评测基准

### 2.1 主要基准一览

| 基准 | 测什么 | 规模 | SOTA（2025 末–2026） | 备注 |
|------|--------|------|---------------------|------|
| **SWE-bench Verified** | 修真实 GitHub issue | 500 人工校验任务 | 80%+ | 2025-09 因污染问题被弃用，迁向 SWE-bench Pro |
| **SWE-bench Pro** | 商业仓库 + 防污染 | — | — | 2025-09 替代 Verified |
| **SWE-bench Multimodal** | 含视觉 bug report 与 UI 测试 | 517 任务 | — | 2025 扩展 |
| **GAIA** | 多步推理 + 工具 + 多模态 | 466 真实问题 | L1 40–50%，L3 远低 | 人类 90%+，最诚实的通用助手基准 |
| **GAIA2 (2025)** | 模拟手机多 app 长程交互 | 显著扩大 | — | Meta Agents Research Environments |
| **WebArena** | 真实网站导航 | 812 任务 | 60%+（CUGA 61.7%，OpenAI CUA 58.1%） | 人类 78.24% |
| **VisualWebArena** | WebArena + 视觉理解 | — | — | 2024 扩展 |
| **OSWorld** | Ubuntu 桌面 GUI Agent | 多任务 | OSWorld 2.0: 严格 20.6% / 部分 54.8% | 平均 27.25 checkpoints/任务 |
| **τ-bench / τ³-bench** | 客服场景工具调用 + 规则遵循 | 航空 / 零售 | 持续打榜 | Pass^k 衡量稳定性 |
| **AgentBench** | 8 环境（OS/DB/KG/卡牌/侧推/ALFWorld/购物/浏览） | 4k dev + 13k test | GPT-4 领先 | 老牌多环境 |
| **Terminal-Bench 2.0** | 终端原生运维工作流 | — | 82.0% | 测完整 harness 而非纯模型 |
| **WebShop** | 电商导航 | 12,087 指令 / 1.18M 商品 | — | 文本接口 |
| **AgentBoard** | 多轮分析板 | — | — | 提供 per-step 评估 |
| **MCPEval (2025)** | 基于 MCP 协议的统一评估 | — | — | Liu et al., 17 Jul 2025 |

### 2.2 关键提醒（score 解读注意事项）

- 同一基准不同 harness 下分数可差 20–40 个百分点
- 报告分数必须同时报告：harness、judge model、temperature、retry 预算、模型版本
- SWE-bench "hidden test" 实际在 repo 中可见，污染需靠 adversarial curation 抑制
- 现实 close rate（用户真实关单率）通常比 SWE-bench 分数低得多

---

## 3 科研智能体专门评测

### 3.1 主要基准对比

| 基准 | 出处 | 任务设计 | 评估指标 | 关键发现 |
|------|------|---------|---------|---------|
| **ScienceAgentBench** | ICLR'25, OSU (Chen et al.) | 44 篇顶刊论文抽取 102 任务，覆盖 4 学科 | VER（执行成功率）、SR（领域成功率）、CodeBERTScore、API Cost | 最佳 32.4% 独立 / 34.3% 加专家知识；o1 + self-debug 42.2%，但成本 10× |
| **BLADE** | EMNLP'24, UW (Gu et al.) | 12 真实研究问题；开放式数据分析 | Decision-based F1（precision + coverage@10）| GPT-4o agent 最佳 44.8 F1；模型在统计模型选择上偏弱 |
| **SciAgent** | 2025 (Li et al.) | 奥赛级推理（IMO/IMC/IPhO/CPhO）+ HLE | 奥赛官方评分 + LLM/专家双验 | IMO 2025: 36/42（>平均金牌 35.94）；IMC 2025: 100/100 |
| **MLAgentBench** | 2023 | ML 研究自动化 | 性能提升、novelty | 老牌 ML agent 基准 |
| **DS-Agent / DABench / QRData** | 2024 | 数据科学子任务 | 单答案评估 | 已被 BLADE 在"开放式 + 决策级"维度超越 |
| **Data Interpreter** | 2024 | 数据分析 | 自动评估 | — |
| **HAL (Holistic Agent Leaderboard)** | 2025 (Kapoor et al.) | 模型 × scaffold × 基准 三维矩阵 | 21,730 rollouts × 9 模型 × 9 基准 | 揭示"高 reasoning effort 反而降低准确率"等反直觉发现 |
| **ResearchBench** | 2025 | 复现 NeurIPS 论文 | 复现质量 | — |
| **Agent's Last Exam** | 2026 (Sun et al.) | 1,000+ 任务 / 55 子领域 / 13 行业 | Gate-and-score | 最难 tier 平均全过率 < 1% |

### 3.2 科研 Agent 评测的方法学差异

1. **"决策级"评测 vs "答案级"评测**：BLADE 把分析拆成"概念变量定义 → 数据变换 → 统计模型"三步分别匹配 ground truth，对部分正确赋分
2. **多模态融合评估**：SciAgent 显式评估 image + symbol + text 协同
3. **端到端 vs 子任务**：ScienceAgentBench 主张先在子任务（数据加载、统计建模、可视化）评测，再谈端到端自动化
4. **抗污染机制**：删除部分数据点 + 把监督标签替换为 dummy 值（−1）
5. **领域专家介入**：每个任务至少 9 位专家多轮校验

### 3.3 ScienceAgentBench 详细指标

- **Valid Execution Rate (VER)**：执行无错且输出命名正确的程序比例
- **Success Rate (SR)**：执行输出满足领域特定成功标准的比例
- **CodeBERTScore (CBS)**：生成代码与标注参考的相似度
- **API Cost**：运行与评分的美元成本

抗污染策略：
1. 从 test split 随机删除少量数据点
2. 监督任务 test label 替换为 dummy 值（−1）

---

## 4 Harness 能力评测

**Harness 定义**：模型外部的"操作系统"，包含工具调用、轨迹记录、错误恢复、上下文管理、安全护栏。2025 年开始被作为独立对象评测。

### 4.1 Harness-Bench（arXiv:2605.27922, 2026）

第一个把 harness 本身作为被测对象的公开基准。

**设计要点**：
- **106 任务 × 6 可配置 harness × 8 后端模型 = 5,088 trajectories**（另加 106 Codex 轨迹，共 5,194）
- 任务设计四原则：realism / solvability / oracle-checkability / integrity
- 评分采用乘性公式：
  ```
  TaskScore_i = Security_i × Completion_i × (Robustness_i + ToolUse_i + Consistency_i) / 3
  ```
- Security 是 0/1 门控，违反安全即归零
- Process 维度由统一 LLM judge（claude-sonnet-4.6）按 trace-based rubric 评估

**关键发现**：
- 同一组任务同一批模型下，harness 间总分开差 **23.8 个百分点**（NanoBot 76.2 vs OpenClaw 52.4）
- NanoBot 获最高可配置 harness 分数但 token 消耗低于 4 个对手 —— 长轨迹 ≠ 好结果
- Codex 80.4（最高）但属模型绑定编码 agent，单独报告
- 所有 harness 在 security 门控上得 100%

### 4.2 Harness 评测的"三层测试法"

| 层级 | 覆盖范围 | 方法 |
|------|---------|------|
| **Layer 1: 确定性单元层** | 30–40% 行为 | 工具 schema、参数解析、状态机 → 标准单测 |
| **Layer 2: LLM-as-Judge 软质量层** | 语义、风格、faithfulness | 0.0–1.0 评分；多次运行取均值；0.5–0.8 视为"软失败" |
| **Layer 3: 端到端轨迹层** | step efficiency、error recovery、循环检测 | 20% 测试用例专门覆盖故障路径 |

### 4.3 元评测（Meta-Reward）—— 给 Judge 加 Harness

**问题**：LLM Judge 本身也需要 harness 调优，否则是 underspecified reward model。

**Meta-Reward 方法**（Canvas, 2026）：
- 固定 judge 模型参数，优化 evaluator harness 周边
- 可优化项：trace view、policy context、rubric、decision process、scoring logic
- 在 τ³-bench airline 上，给定 Haiku 4.5 judge，调优 harness 把 held-out agreement 从 **52.8% 提到 78.2%**，best-of-N 选优提升 +30.2 点
- Plan-RewardBench 上：60.5% → 72.4%（+11.9 点）

这印证了 HAL 发现的"高 reasoning effort 反而降低准确率" —— 评测端的冗余思考也是噪声。

### 4.4 实证案例：Harness 评测的紧迫性

- Braintrust 2026 报告：只评估最终输出的 agent 比轨迹级评估多过 20–40% 用例
- 某支持工单 agent 切到 outcome-based scoring 后通过率从 92% 跌至 41%，但生产事故显著下降
- 经典陷阱：grading the narration —— agent 能写出漂亮的"成功叙述"，但 tool call 实际失败

---

## 5 Memory 能力评测

2024 之前 "agent memory = 把对话塞进 context window"，2026 已成熟为独立基准赛道。

### 5.1 三大标准基准

| 基准 | 出处 | 规模 | 测什么 | 为什么难 |
|------|------|------|--------|---------|
| **LoCoMo** | ACL'24 (Maharana et al.) | 1,540 问 / 300 turn / 35 会话 / ~9,000 token | single-hop、multi-hop、temporal、open-domain | 跨天/跨周 35 session |
| **LongMemEval** | ICLR'25 (Wu et al.) | 500 问 / ~115K token 历史 | single-session（user/assistant/preference）、knowledge update、temporal、multi-session | 知识更新与多 session 推理是难点 |
| **BEAM** | ICLR'26 (Tavakoli et al.) | 100 会话 / 2,000 问 / 1M–10M token | 10 类：preference/instruction/info-extraction/knowledge-update/multi-session/summarization/temporal/event-ordering/abstention/contradiction | 不能用堆 context window 解决 |
| **MemoryArena** | 2026 | agentic 任务中的 memory 使用 | 任务完成度提升 | 在路线图中 |

### 5.2 核心指标五维

| 指标 | 含义 |
|------|------|
| **BLEU** | 与 ground truth 的 token 级相似度 |
| **F1** | 响应 token 的 precision / recall |
| **LLM score** | LLM judge 二元正确性判定 |
| **Token consumption** | 每 query 总 token |
| **Latency** | 检索 + 响应 wall-clock 时间 |

### 5.3 当前 SOTA 对比

| 系统 | LoCoMo | LongMemEval | BEAM-1M | BEAM-10M |
|------|--------|-------------|---------|----------|
| Mem0（新算法，2026） | 92.5 | 94.4 | 64.1 | 48.6 |
| Zep | 94.7 | 90.2 | — | — |
| ByteRover | — | 92.8 (LongMemEval-S) | — | — |
| MemoryLake | 94.03 | — | — | — |
| Dakera | 88.2 | — | — | — |
| Mem0（旧算法） | 71.4 | 67.8 | — | — |
| LlamaIndex | 54.8 | 59.0 | — | — |
| LangChain | 51.9 | 59.0 | — | — |
| **LLM baseline（无 memory）** | 50.4 | 57.6 | — | — |
| Mem0 OSS | 0.0 | 32.4 | — | — |

**注**：同一系统在不同 harness 下 LoCoMo 可从 38% 飙到 92%；分数不可直接比较。

### 5.4 LongMemEval 类别细分（Mem0 报告）

| 类别 | Mem0 分数 |
|------|-----------|
| Single-session (user) | 98.6 |
| Single-session (assistant) | 98.2 |
| Knowledge update | 93.6 |
| Multi-session | 88.0 |

Single-session 已接近饱和；knowledge update 与 multi-session 仍有显著 headroom。

### 5.5 BEAM 类别细分（Mem0 报告）

| 类别 | 1M | 10M |
|------|-----|------|
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

10M 级别下 temporal_reasoning 与 event_ordering 是全行业开放问题。

### 5.6 开放问题（mem0 2026 报告）

- 跨 session 身份（cross-session identity）
- 时序抽象规模化（temporal abstraction at scale）
- 记忆陈旧化（memory staleness）

### 5.7 重要警告

- LoCoMo 答案键有 ~6.4% 错误
- 默认 GPT-4o-mini judge FPR 约 63%
- "LLM baseline > 多数专用 memory 系统"：堆 context window 在很多场景胜过压缩/蒸馏

### 5.8 实测方法论（推荐顺序）

1. **先测 Context Completeness**（COMPLETE/PARTIAL/INSUFFICIENT）—— 检索层独立于 LLM
2. **再测 Answer Correctness** —— 端到端
3. **再测 Latency + Token** —— 生产门槛
4. **测试集设计**：3–5 目标交互 → 扩到 10+ 变体 → 多 session 散布 → 包含时序变化用例
5. **加背景数据与噪声**：把相关事实埋在更大图与 JSON/业务数据中，模拟真实检索条件

### 5.9 Context Window 实际有效率

- NVIDIA RULER 基准：有效 context 约为标称 window 的 50–65%
- Chroma "context rot" 研究：测试 18 个前沿模型，全部在长度增长时退化
- Lost-in-the-middle 效应：相关事实位于窗口中段时准确率下降 > 30%

---

## 6 过程 / 轨迹评测：避免"叙述陷阱"

科研 agent 最易出现的失败模式：**结果对但路径错**（"草率成功" / lucky success）。

### 6.1 Milestone / Checkpoint 评分方法

| 基准 | 方法 | 优势 |
|------|------|------|
| **OSWorld 2.0** | 每任务 ~27.25 checkpoints | 严格二值 20.6% / 部分 54.8% 暴露信息丢失 |
| **WindowsWorld (2026)** | 181 任务 × 5 子目标均值 | 78% 跨多 app |
| **Agent's Last Exam** | Gate-and-score（先过门槛再加权 rubric） | 全过 < 1% 时仍能定位失败点 |
| **ClawTrack** | Outcome + Process 双 grader | 安全门控是乘性而非加性 |

### 6.2 ClawTrack 评分公式

**Task Score**：
```
s_task = s_safe · (α·s_comp + β·s_rob)
```
其中 α=0.80，β=0.20，s_safe 是 0/1 门控。

**Process Score**（per turn）：
```
s_proc^(t) = g_t · (w_e·e_t + w_i·i_t + w_v·v_t)
```
- g_t = goal alignment（门控，不对齐则该 turn 归零）
- e_t = efficiency，w_e = 0.40
- i_t = information utilization，w_i = 0.40
- v_t = result verification，w_v = 0.20

### 6.3 工具调用层评测指标

- **Tool F1**：与参考 multiset 比对的 precision / recall / F1
- **Step Efficiency** = optimal_steps / actual_steps（capped 1.0）
- **Tool Call Accuracy**：参数级
- **Recovery Count**：错误恢复次数

### 6.4 Process Reward Model (PRM)

- 每步单独打分（Lightman et al., 2023 起）
- 是 RL 后训练的核心信号
- Milestone-aware 训练方法：BEACON、MiRA、ADMIRE、GiGPO
  - 用 repeat state / 显式分解 / 学习出的 milestone 给长程 rollout 提供 dense credit
  - GiGPO 从共享 anchor state 抽取 step-level comparison group

---

## 7 实战落地建议（4 阶段路径）

### 7.1 Week 1–2 — Foundation

- 5–10 个 priority intent + 每个的成功定义
- 开启 trace（plan / tools / timings / cost / errors）
- 起步 50–200 条 Golden Set
- 夜间跑 evaluator + 简易 scorecard

### 7.2 Week 3–4 — Automation

- CI/CD gate：safety / task success 回归即阻塞
- 按 version / env / date / intent 的 dashboard
- 高风险 session 启动 HITL 评审

### 7.3 Month 2 — Learning

- 按 delta 优化 prompt / policy，慎用 fine-tune
- 高风险工具前加 policy check
- 失败循环、延迟尖峰、成本漂移告警
- A/B 或 shadow 实验

### 7.4 Month 3+ — Scale

- 自动化 scenario 生成（adversarial + counterfactual）
- 跨 session 评测（memory 维度）
- 红队演练 / 治理

### 7.5 评分器分工（关键工程原则）

| 类型 | 适用场景 | 优点 | 风险 |
|------|---------|------|------|
| **代码/规则 scorer** | 工具 schema、状态变更、禁用动作 | 稳定、便宜、可复现 | 覆盖不了复杂语义 |
| **LLM-as-Judge** | 解释质量、策略妥当性 | 可扩展，接近专家判断 | 有偏差、需校准 |
| **Human scorer** | 业务口径未固化 / 高风险终判 / 校准 | 最接近业务共识 | 成本高、规模小 |

**核心原则**：
- 规则看"硬条件" —— 能写成代码的由规则主判
- LLM 看"软语义" —— 解释质量、策略妥当性由 LLM-as-Judge 补充
- 人工看"终局" —— 业务标准确认、冲突处理、高风险终判

**LLM-as-Judge 工程要求**：
- 明确评分标准：每个分档有可执行标准
- 输出 Reason：方便定位问题和 Bad Case 聚类
- Few-shot 示例：包含边界样本和判定逻辑
- 周期性校准：与人工复核一致率达 ~85% 后进入日常自动化
- 偏差治理：verbosity bias、position bias、多模型对抗打分
- Judge 模型与被评 Agent 不同源

---

## 8 怀疑论视角：评测的局限

### 8.1 仿真环境的迁移性

- WebShop、ALFWorld 等仿真环境接近 LLM 预训练分布
- 在这些环境上的成绩不可迁移到未见过的真实 UI
- WebShop 80% 的 agent 可能在 novel e-commerce site 上完全失败

### 8.2 SWE-bench 的"戏剧性"与警示

- 2024 初 ~2% → 2025 末 ~60%+ 的进展曲线是真实的，是 program synthesis 历史最快之一
- 但同时：
  - **Gameable**：hidden test 实际在 repo 可见；访问 test file 的 agent 表现人为偏高
  - **Not the same as deployed close rate**：Cognition / Sourcegraph / Aider 部署后真实 close rate 显著低于 benchmark
  - **Insensitive to solution quality**：通过测试但引入回归的修复得分与干净修复相同

### 8.3 GAIA 的诚实信号

- 是第一个 leading LLM agent 在贴近真实助手工作的任务上不接近人类的基准
- 教训：用户在意的很多任务，honest human baseline 超出当前 agent 能力

### 8.4 三类 Agent 错误分类

| 错误类型 | 描述 | 修复工具 |
|---------|------|---------|
| **Specification errors** | 用户请求模糊，agent 静默承诺错误解释 | 强制 agent 先产出解释与澄清问题（SpeakRL, Acikgoz et al. 2025 可提升 ~20pp）|
| **Tool-call errors** | 参数类型不匹配 / 语义错误 | Schema validation、retry with feedback、constrained decoding |
| **Reasoning errors** | 推理链无效 | Reflexion 是结构化重采样方法，不是 oracle；系统性错误仍传播 |

### 8.5 部署基准差异

- benchmark-to-deployment gap 是 agent reliability 的一类事实，不是小瑕疵
- 即使加 schema validation，5–15% 的 tool call 在生产中含语义错误

---

## 9 评测方法学的工程化补充

### 9.1 Trajectory vs Outcome 两种评分

- **Outcome grading**：问最终环境状态是否正确。客观但粗糙
- **Transcript grading**：问路径是否合理。能识别 lucky pass 或 wasteful path
- 例：修复失败测试，agent A 改函数测试绿了；agent B 注释掉测试也绿了。Outcome 都过，Transcript 区分
- 实务：Outcome 用于规模化 pass/fail，Transcript 用于解释失败

### 9.2 Agent Eval 四组件（Anthropic 框架）

1. **Task**：单条测试，有明确输入与成功标准
2. **Trial**：一次任务尝试
3. **Grader**：评分逻辑
4. **Evaluation Harness**：端到端运行基础设施

### 9.3 Trace 标准化

最小 trace 记录（推荐字段）：
- `session_id`
- `turn`
- `user_intent`
- `agent_plan`
- `chosen_tools`
- `tool_calls`（name, args, latency, success）
- `memory_reads/writes`
- `response_summary`
- `costs`
- `timings`
- `errors`

### 9.4 Eval 数据集构建方法

1. **生产日志采样**：保留真实用户输入；人工标注期望路径
2. **专家设计**：覆盖正常 / 异常 / 边界 / 安全
3. **LLM 辅助生成**：批量生成 + 人工审核
4. **Bad Case 回流**：线上失败自动进入评测集

最佳实践：四种方法组合使用。

### 9.5 LLM-as-Judge 校准方法

- 与人工一致率 ≥ 85% 才进入日常自动化
- Judge 偏差治理：
  - Verbosity bias（冗长偏见）
  - Position bias（位置偏见）
  - 自家模型偏好（self-style bias）
- 多模型对抗打分
- Judge 模型与被评 Agent 不同源

### 9.6 回归测试触发条件

- LLM 模型版本更新
- System Prompt 修改
- 工具 / Skill 的接口或行为变化
- 记忆库内容变化（新增/删除了历史记忆）

### 9.7 成熟度模型（参考 CMMI 思想）

| 级别 | 特征 | 适用 |
|------|------|------|
| **L0 原型** | 能跑就行，无工程化 | 内部实验、概念验证 |
| **L1 可用** | 基本错误处理、日志、版本管理 | 小范围内部使用 |
| **L2 可靠** | 自动化评测、回归测试、监控、灰度、长期记忆、任务规划 | 面向用户的生产环境 |
| **L3 可优化** | 反馈驱动持续改进、Bad Case 分析、A/B 测试、多 Agent 协作 | 规模化运营 |
| **L4 可规模化** | 标准化 Skill 生态、统一 Agent 平台、MCP 协议、跨团队复用 | 企业级 AI 平台 |

---

## 10 一句话总结

- **趋势**：评测从"打分"演化为"工程学科"，3 个分数搭配（task success + cost + safety）才够看，golden set 写到 CI gate 才算生产化
- **科研 agent 特殊点**："多解 + 过程对才算对"，BLADE / ScienceAgentBench / SciAgent 是必看
- **Harness**：现在被作为独立对象评测，Harness-Bench 给出 23.8 分的 harness 间差距 —— 选 harness 比选模型影响更大
- **Memory**：已成熟为独立基准赛道（LoCoMo / LongMemEval / BEAM），但分数不可比，要看 harness + judge + seed 一起
- **警惕**：SWE-bench 高分不一定预测生产可用；"草率成功"需轨迹级评分拦截；评测端本身的冗余思考也是噪声

---

## 参考文献

### 通用评测

1. Jimenez, C.E. et al. (2024). "SWE-bench: Can Language Models Resolve Real-World GitHub Issues?" arXiv:2310.06770.
2. Mialon, G. et al. (2023). "GAIA: A Benchmark for General AI Assistants." arXiv:2311.12983.
3. Zhou, S. et al. (2024). "WebArena: A Realistic Web Environment for Building Autonomous Agents." arXiv:2307.13854.
4. Liu, X. et al. (2024). "AgentBench: Evaluating LLMs as Agents." ICLR 2024.
5. Yao, S. et al. (2022). "ReAct: Synergizing Reasoning and Acting in Language Models." ICLR 2023.
6. Shinn, N. et al. (2023). "Reflexion: Language Agents with Verbal Reinforcement Learning." NeurIPS 2023.

### 科研 Agent 评测

7. Chen, Z. et al. (2025). "ScienceAgentBench: Toward Rigorous Assessment of Language Agents for Data-Driven Scientific Discovery." ICLR 2025. https://github.com/OSU-NLP-Group/ScienceAgentBench
8. Gu, K. et al. (2024). "BLADE: Benchmarking Language Model Agents for Data-Driven Science." EMNLP 2024 Findings. https://blade-bench.github.io/
9. Li et al. (2025). "SciAgent: A Unified Multi-Agent System for Generalistic Scientific Reasoning." arXiv:2511.08151.
10. Huang, Q. et al. (2024). "MLAgentBench: Evaluating Language Agents on Machine Learning Experimentation." arXiv:2310.03302.
11. Guo, S. et al. (2024). "DS-Agent: Automated Data Science by Empowering Large Language Models with Case-Based Reasoning." ICML 2024.
12. Hu, X. et al. (2024). "InfiAgent-DABench: Evaluating Agents on Data Analysis Tasks." ICML 2024.
13. Kapoor, S. et al. (2025). "Holistic Agent Leaderboard (HAL): The Missing Infrastructure for AI Agent Evaluation." arXiv:2510.11977.
14. Sun et al. (2026). "Agent's Last Exam."

### Harness 与元评测

15. (2026). "Harness-Bench: Measuring Harness Effects across Models in Realistic Agent Workflows." arXiv:2605.27922.
16. Sleiman et al. (2026). "Meta-Reward: Reward Modeling as Harness Optimization." Canvas Research. https://www.canvas.inc/research/reward-models
17. (2024). "Evaluation-Driven Development of LLM Agents: A Process Model and Reference Architecture." Xia et al.
18. Anthropic. "Building Effective Agents." & "Agent Evals Guide."

### Memory 评测

19. Maharana, A. et al. (2024). "Evaluating Very Long-Term Conversational Memory of LLM Agents." ACL 2024. arXiv:2402.17753.
20. Wu, D. et al. (2025). "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory." ICLR 2025. arXiv:2410.10813.
21. Tavakoli, M. et al. (2026). "BEAM: Benchmarking Agent Memory at Scale." ICLR 2026.
22. Mem0 (2025). "Mem0: A Memory Layer for AI Agents." ECAI 2025. arXiv:2504.19413.
23. (2025). "State of AI Agent Memory 2026: Benchmarks, Architectures & Production Gaps." mem0.ai.
24. Sumers, T. et al. (2024). "CoALA: Cognitive Architectures for Language Agents."

### 过程 / 轨迹评测

25. Lightman, H. et al. (2023). "Let's Verify Step by Step." arXiv:2305.20050.
26. (2026). "Milestone-Based Evaluation and Training for Long-Horizon AI Agents." Snorkel AI.
27. (2025). "ClawTrack: Towards Trace-Level Evaluation and Improvement of Real-World Autonomous Agents." arXiv:2607.28037.
28. OSWorld 2.0. https://osworld.dev/
29. WindowsWorld (2026). arXiv:2604.27776.
30. (2025). "GiGPO: Grouped-in-Group Policy Optimization." (state-based step-level comparison groups)

### 评测框架与综述

31. Mohammadi, H. et al. (2025). "Evaluation and Benchmarking of LLM Agents: A Survey."
32. Yehudai, A. et al. (2025). "Evolutionary Perspectives on the Evaluation of LLM-Based AI Agents: A Comprehensive Survey."
33. (2025). "Survey on Evaluation of LLM-based Agents." ownyourai.com.
34. (2024). "AgentBoard: An Analytical Evaluation Board of Multi-turn LLM Agents."
35. (2025). "MCPEval: Automatic MCP-based Deep Evaluation for AI Agent Models." Liu et al., 17 Jul 2025.
36. (2024). "TestAgent: A Framework for Domain-Adaptive Evaluation of LLMs via Dynamic Benchmark Construction and Exploratory Interaction."

### 怀疑论视角

37. iohanngrig. (2025). "LLM agents as decision systems: a skeptic's guide." iohanngrig.github.io/research-notes/05-llm-agents.
38. Skygena (2025). "Year in review — agent evaluation is the discipline that finally grew up."

### 安全 / 治理

39. OWASP. (2025). "Top 10 for Agentic Applications 2026" (ASI01–ASI10).
40. OWASP. (2024). "LLM Top 10" (2025 版).
41. 新加坡 IMDA. (2026). "Model AI Governance Framework for Agentic AI." Version 1.5.
42. 五眼网络安全机构. (2026). "Careful Adoption of Agentic AI Services."

### 行业报告

43. Gartner (2025). 40% of enterprise applications will integrate task-specific AI agents by end-2026.
44. McKinsey (2025). "State of AI Global Survey."
45. Braintrust (2026). Series B $80M at $800M valuation, built on production-trace-to-regression-test pipeline.
46. Penfield Labs (2026). "LOCOMO audit." dev.to/penfieldlabs.

---

## 附录 A：完整基准清单（按用途分类）

### A.1 编码 / 软件工程
- SWE-bench / SWE-bench Verified / SWE-bench Pro / SWE-bench Multimodal / SWE-rebench (21,000+)
- HumanEval / MBPP（基础编码能力）
- RepoBench / CrossCodeEval（仓库级）

### A.2 通用助手
- GAIA / GAIA2 / AgentBench / AgentBoard / WebArena / VisualWebArena / WebShop / Mind2Web

### A.3 桌面 / GUI
- OSWorld / OSWorld 2.0 / WindowsWorld / ScreenAgent

### A.4 工具调用 / 函数调用
- τ-bench / τ³-bench / ToolBench / API-Bank / MCPEval

### A.5 科研 / 数据科学
- ScienceAgentBench / BLADE / SciAgent / MLAgentBench / DS-Agent / DABench / QRData / Data Interpreter / ResearchBench

### A.6 推理 / 数学 / 知识
- MATH / GSM8K / AIME / IMO-Bench / HLE (Humanity's Last Exam) / FrontierMath / TheoremQA

### A.7 多模态
- MMMU / MathVista / ChartQA / DocVQA / BLINK

### A.8 对齐 / 安全
- MACHIAVELLI / DialogGuard / ALI-Agent / HarmBench / JailbreakBench

### A.9 多 Agent
- MultiAgentBench / ChatEval / ChatDev / MetaGPT-Eval

### A.10 Memory
- LoCoMo / LongMemEval / BEAM / MemoryArena

### A.11 Harness / 框架评测
- Harness-Bench / HAL（Holistic Agent Leaderboard）/ AgentBoard

---

## 附录 B：关键数字速查

| 项 | 数值 |
|----|------|
| GAIA 人类水平 | 90%+（L1/L2/L3） |
| GAIA 顶级 agent | L1 40–50%，L3 远低 |
| SWE-bench Verified SOTA | 80%+（vendor 报告） |
| WebArena 顶级 agent | 60%+（CUGA 61.7%） |
| WebArena 人类 | 78.24% |
| OSWorld 2.0 严格 SOTA | 20.6% |
| OSWorld 2.0 部分 SOTA | 54.8% |
| Terminal-Bench 2.0 SOTA | 82.0% |
| ScienceAgentBench 最佳 | 32.4% 独立 / 42.2% self-debug |
| BLADE 最佳 F1 | 44.8% (GPT-4o agent) |
| SciAgent IMO 2025 | 36/42（>人类金牌 35.94） |
| Harness 差距（Harness-Bench） | 23.8 pp（NanoBot vs OpenClaw） |
| Meta-Reward τ³ 提升 | 52.8% → 78.2%（+25.4） |
| Meta-Reward Plan-RewardBench 提升 | 60.5% → 72.4%（+11.9） |
| 上下文有效率 | 50–65%（RULER） |
| Lost-in-middle 准确率损失 | > 30% |
| LLM baseline（无 memory）LoCoMo | 50.4% |
| LoCoMo 答案键错误率 | ~6.4% |
| GPT-4o-mini judge FPR | 63% |
| 企业 agent 部署率 | 65%（Gartner 2025） |
| 主动规模化企业 | 23%（McKinsey 2025） |
| Braintrust Series B | $80M at $800M（2026-02） |

---

*本文档为 OmniScientist 项目补充材料。如需引用，请参考各基准原始论文。*
