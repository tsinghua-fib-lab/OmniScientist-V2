# 请求生命周期：从用户消息到持久化记录

> 一份对单个用户请求在 harness 中行进的纵向视图。 [harness-architecture.md](harness-architecture.md) / [harness-architecture_cn.md](harness-architecture_cn.md) 里的 九维度分析是 *横截面*——每个职责静止时什么样。本文档 是 *轨迹*——一个请求、六个阶段、跨过每个边界的是什么、 harness 在失败下怎么保持系统一致。
>
> 想要已建成的模块图，请看 [architecture.md](architecture.md)。想要用户视角的 运行时 invariants，请看 [agent-runtime-harness.md](agent-runtime-harness.md)。想要 逐行的实现，请看 [harness-dimensions.md](harness-dimensions.md) / [harness-dimensions_cn.md](harness-dimensions_cn.md)。

## 1 为什么需要生命周期视图

真实的用户请求不是到达某个单一函数。它到达 CLI 解析、IM webhook、或者 REPL 命令。它被归一化成 session，挂到 principal 上，被赋予一个 `Task`。它被 plan、validate、execute、 observe、settle、render、persist。同一个请求可能派生 subagent、调度后台工作、触发人类批准、扛过一次服务器 重启。

九维度分析是必要的，但不充分。它告诉你每个职责*是*什么； 不告诉你什么能扛住 crash、什么跨过进程边界、cancel hook 在哪。这些是*纵向*属性——只在时间上下文里有意义。

本文档沿时间跟着一个用户请求从到达走到持久化记录，分 六个阶段。每个阶段有明确的 inputs、outputs、所在的边界、 harness 处理的失败模式。最后是一张表，把每个阶段映射回 九个职责，让任一文档的读者能跳到另一个。

## 2 六个阶段

```
   Phase 1         Phase 2         Phase 3         Phase 4         Phase 5         Phase 6
   Intake          Plan            Execute         Settle          Render          Persist
   接入             规划             执行             结算            渲染            持久化
   ──────          ────            ───────         ──────          ──────          ──────
   channel ──▶  IntentPlan  ──▶  ReAct loop  ──▶  terminal   ──▶  TurnPresent  ──▶ committed
   principal        capability      tool dispatch    status          channel         + global
   session          validation      subagent         children         notification    updates
   user msg         needs_input     observability    settle_*
                                    escalate_run
                                    hooks
                                    circuit break
```

图是简化版。实践中阶段是 re-entrant 的——`Plan` 可以 产出 `needs_input` 回环到 `Intake`；`Execute` 可以 `escalate_run` 出一个新的后台任务，再走一遍六个阶段； `Settle` 可以失败并重执行；`Persist` 可以输掉 race 回滚。上面的箭头是 happy path。

### 2.1 Phase 1 — 接入（Intake）

**Inputs。** 一个用户消息，来自某个入口 channel （`omni chat`、REPL、IM 平台：Feishu / WeChat / DingTalk、 或程序化 API 调用）。消息是一个字符串加上 channel 特定的 envelope（sender id、thread id、attachments、time）。

**边界。** Channel 特定的 adapter 把 envelope 转成归一化的 `Session` 请求。这是系统*外部*和*内部*之间的边界。

**Harness 在这里做什么。**

- **Principal 解析。** 每个 channel 映射到一个 `principal`：CLI 是 `local`，IM 是 `<channel>:<external_key>`。Memory 隔离按这个 key。
- **Session 连续性。** 同 key 的 IM 消息复用最近一个 active session；CLI 默认开新 session，除非命名了 一个。Session id 成为所有后续状态的 durable key。
- **Project 上下文。** 解析 active workspace （CLI 用 CWD、最近一次在该目录的 `omni` 调用、 或 `~/.omni/projects/` 下的命名 project）。Workspace 是 harness 的 `output_root` 和沙箱可写子路径列表的 边界。
- **首次 prompt 组装。** System prompt 由 [harness-architecture_cn.md §2.3](harness-architecture_cn.md#23-上下文管理) 描述的六段构建——identity、tool catalog、planning rules、environment、self-knowledge、behavior——三段 条件切换的部分反映本 turn 在 scope 内的工具。

**Outputs。** 一个 `Session`、一个 `Principal`、一个 `Workspace`、一个初始 system prompt、以及 durable 记录里 一行 `Task`。

**失败模式。**

- Channel 不可达（IM webhook 挂了）。Harness 把消息 排队并返回 5xx；不启动 `Task`。
- 消息格式无效。Harness 向 channel 返回结构化错误； 不创建 `Task`。
- Workspace 找不到。Harness 在 `~/.omni/workspaces/` 下按 CWD 创建；失败模式是*用户*不在 VCS 根，harness 容忍这种情况。

### 2.2 Phase 2 — 规划（Plan）

**Inputs。** 用户消息、session 上下文、project 上下文、 workspace 装好的 skills、可用工具的 catalog。

**边界。** "我知道用户想要什么"和"我有一个具体计划去做" 之间的边界。输出是一个已被 validate 的 `IntentPlan`， 有显式拒绝的候选、显式 turn 主张必须留下哪些 events。

**Harness 在这里做什么。**

- **能力选择。** Harness 决定需要哪些 skills 和 tools，哪些被故意拒绝（以及为什么）。Plan 是 *自描述*的；读者能看见考虑过什么、为什么没选。
- **模式选择。** 交互模式是 `auto`（跑 plan）、 `plan`（持久化 plan 并停在 `awaiting_approval`）、 或 `review`（只读面、强制输出 review）。模式来自 channel（CLI flag、REPL 命令、IM）或 user profile。
- **Policy 绑定。** Tool policy 绑到 plan（allow list / block list / per-tool 预算）。Plan 拥有自己的 policy；policy 不能在 execute 时被放宽。
- **Validation。** Plan 被 validate 安全性（禁止动作、 不安全 scope）、前置条件（需要的 skill 装了、需要的 secret 在）、幂等性。Safety finding 是唯一的硬停。
- **恢复路径。** Plan 不完整时，harness 有三种有序 响应：`needs_input`（用户必须澄清）、`bounded ReAct handoff`（plan 够好；让 model 补 gap）、 `awaiting_approval`（持久化 plan，停）。

**Outputs。** 一个 validate 过的 `IntentPlan` 持久化到 durable 记录，带一个 `TaskORM` 行，包含 plan、拒绝的 候选、执行模式、tool policy、turn 主张必须留下的 events。 如果模式是 `plan`，task 处于 `awaiting_approval`；用户 必须 `omni task approve <task-id>` 才能推进。

**失败模式。**

- **Safety finding。** 硬停。Plan 不被持久化；用户 被告知什么错、该做什么。
- **能力缺失。** Plan 要么优雅降级（省掉受影响 步骤），要么带着具体 gap 返回 `needs_input`。
- **Plan 过大。** Harness 给 plan 大小加 cap；过大的 plan 被拆成多个 task，每个有自己的 plan 和 approve 边界。

### 2.3 Phase 3 — 执行（Execute）

**Inputs。** Validate 过的 `IntentPlan`、workspace、tool surface（从 skill registry、schedule registry、MCP servers、 外部 integrations 构建）、model binding（provider、model、 harness 侧 knobs）、system prompt。

**边界。** "我有 plan"和"我已经取得进展"之间的边界。 这个阶段的输出是一串 `TaskEventORM` 行，记下每次工具 调用、每个 cost event、每次 hook firing、每个 subagent spawn、每个 model turn。

**Harness 在这里做什么。**

- **主循环。** 有界 ReAct 循环，三层时间尺度（stall watchdog、wall-clock deadline、soft notice），prompt 级反思（命名的 steer 字符串），14 种不同的终止 原因。循环驱动整个阶段。
- **Tool dispatch。** 每个工具调用都通过 `ToolGateway` 这个单一 choke point。Gateway 强制 policy、对敏感 调用跑 approval、持久化 `start` 事件、执行、持久化 `done` 事件。Mutating 调用要求 start 事件落地；mutating 调用不在瞬时失败时重试。
- **Subagent dispatch。** Model 可以用 `spawn_subagents` （blocking batch）或 `spawn_subagent` / `wait_subagent` / `interrupt_subagent`（async fire-and-collect）派生 specialists。父循环在 blocking spawn 时暂停，return 时继续。Async spawn 在 `SubagentControl` 里追踪， 在 turn 末收割。
- **升级。** Model 可以调 `escalate_run` 把对话交给 一个 durable 后台任务。后台任务继承父的 ROM （Research Object Model）并通过同一 scheduler 跑， 让 IM / cron channel 看到一个 durable task 而不是 内存中的延续。
- **Hooks。** Pre-tool 和 post-tool hooks 在每次调用 时触发。Owner-controlled 命令，子进程隔离，redacted JSON envelope，严格 timeout。Hook 决定是 `allow` / `deny`；失败被记，主循环继续。
- **熔断。** 两套独立计数器：executed-failure （5 在 `(tool, args)` 上跳闸，meta-tool 按 *subject argument* 做 key）和 unexecuted-refusal（5 触发 `no_progress`）。
- **可观测。** 每步 append 一个 `task_event` 行。Cost 按每次调用记为 `cost.usage` 事件。Hook 成功 / 失败 本身是事件。Transcript 在 `messages` 数组里为下一次 LLM 调用组装。

**Outputs。** 一串 `TaskEventORM` 行（tool calls、cost events、hook events、subagent events、model turns）、 task 上挂的 tool trace、更新的 research ledger（sources、 claims、evidence、artifacts）、以及描述终止的 `AgentLoopResult`。

**失败模式。**

- **Tool error。** 归入五个 host-owned 错误类之一。 一些重试（网络），一些不（mutating、unknown tool、 policy-rejected）。分类是 host-owned；model 看到 remediation hint，不是自由文本错误。
- **Model 幻觉出工具名。** Preflight 拒绝 bump unexecuted-refusal 计数器。连续 5 个以 `no_progress` 终止循环。
- **预算耗尽。** Wall clock、step count、cost、或 context window。循环跑一次 `tool_choice="none"` 的 wrap-up 调用然后终止。失败的 wrap-up 用 salvage stub 替换。
- **Stall 或 timeout。** 同 wrap-up 路径；model 拿到 一次最后机会落一个 final answer。
- **Cancel。** Process-local cancel 或 durable cancel （后者跨重启）。In-flight 工具发 `interrupted_tool_payload`。 Task settle 为 `cancelled` 或 `interrupted`。
- **升级。** Model 调了 `escalate_run`。循环以 `kind="escalated"` 终止；后台 task 被创建并在 scheduler 里追踪。当前 turn 的 events 被封存； 后台 task 用新 event 流重新开始。

### 2.4 Phase 4 — 结算（Settle）

**Inputs。** Phase 3 的 `AgentLoopResult`、research ledger、 subagent 结果、schedule checkpoint（如果有）、以及到 目前为止的 durable task 记录。

**边界。** "我跑了 plan"和"我知道答案是什么"之间的 边界。输出是 task 的一个 `terminal_status`，从 result 和 durable state 确定性派生。

**Harness 在这里做什么。**

- **Child 调和。** 如果派生了 subagent，harness 等 child tasks 到达 terminal state 再 settle 父。父 在 child 还在飞的时候不能 `succeeded`。
- **Terminal status 派生。** Status 从 `AgentLoopResult.kind` 派生：
  - `kind="text"` 无 degradation warnings → `succeeded`
  - `kind="text"` 有 degradation warnings → `degraded`
  - `kind="needs_input"` → `needs_input`
  - `kind="error"` → `failed`
  - `kind="escalated"` → 父 task 是后台 task； settle 延后。
  - Cancelled / interrupted → `cancelled` / `interrupted`
- **Artifact transactions。** 如果这次 run 产出了 artifacts（figures、slides、reports、papers）， 每个记为一个 durable `ArtifactORM` 行，带 content hash、provenance、和追溯到 task 的 event trail。
- **Research ledger merge。** 引用的 sources、做出的 claims、收集的 evidence、产出的 artifacts 都被 fold 进 durable 记录，并通过 recall 对未来 tasks 可见。

**Outputs。** `TaskORM` 行上的 `terminal_status`； 已 settle 的 subagent 结果；更新的 research ledger； artifacts 列表。

**失败模式。**

- **Child 永不终止。** Child 有 deadline 和 heartbeat； 如果 deadline 超过，child 被调和为 `interrupted`， 父被 settle 为 `degraded` 并带一条 warning，说 child 没完成。
- **Settlement 冲突。** Orchestrator 自报的 status 和 durable record 可能不一致。Durable record 赢（代码 注释："settle from the turn's own end once children are terminal"）。

### 2.5 Phase 5 — 渲染（Render）

**Inputs。** 带 `terminal_status` 的 `Task`、 `AgentLoopResult.content`、artifacts 列表、citations 列表、起源 channel。

**边界。** "我有答案"和"用户看见答案"之间的边界。 输出是 channel 特定的 `TurnPresentation`。

**Harness 在这里做什么。**

- **Channel 特定渲染。** CLI 拿 TUI 或纯文本输出； REPL 拿交互式输出；IM 拿格式化的消息（带 artifact 附件和 citation 格式）。渲染是 channel 和 result 的函数，不是配置。
- **Notification。** 如果 task 产出了 artifacts 或到 达了值得注意的状态，harness 可能向 channel （或 channel 离线时向 inbox）推一个 notification。 Notification 是*五选一*：`InboxNotifier`（默认， JSONL 在 `~/.omni/inbox/`）或任何 owner 注册的 channel。
- **Citation 渲染。** Citations 按 channel 惯例 格式化（Markdown 链接、纯文本、IM 友好的 inline）。

**Outputs。** 写到 channel 的 `TurnPresentation`； 可选的、写到 inbox 的 `TaskNotification`。

**失败模式。**

- **Render 错误。** Harness 记错；用户看到一个 fallback "答案在你的工作目录"消息，task 仍然 settled。
- **Channel 离线。** Presentation 入队；用户在 channel 恢复时看到。这是少数系统主动变成 "store and forward" 队列的路径之一。

### 2.6 Phase 6 — 持久化（Persist）

**Inputs。** Phases 1–5 的一切，加上任何 session 级 的维护（memory 合并、notebook 更新、schedule 推进）。

**边界。** "turn 结束"和"系统准备好下一个 turn"之间 的边界。输出是 turn 的 durable 记录加上任何跨 session 的状态更新。

**Harness 在这里做什么。**

- **原子提交。** Turn 的所有写入要么一起 commit， 要么一起回滚。`cancel_persist.py` 里的 `persist_scope` context manager 是机制；它让 parent-cancel-during-write 安全。
- **Session memory 抽取。** 实质的 user 消息被 `MemoryService.extract_session` flush 到 M3 （episodic）和 M4（semantic）。Failures、纯 retrieval 的 turns、degraded turns 被过滤。
- **Notebook append。** Lab notebook append 一段 人类可读 block 总结这次 turn。Notebook 是 git 友好的，充当 system prompt 能引用的 "summary view"。
- **Schedule 推进。** 如果 task 是 schedule 触发的， schedule 的 `next_due_at` 被推进、`run_count` bump。推进跟 commit 的其余部分在同一个事务里。
- **Memory maintenance（后台）。** 每 8 turn session memory 被合并。Session 末 maintenance enqueue（交互式 channel）或同步跑（CLI）。 Maintenance 跑 `decay_and_dedup`、 `rebuild_user_profile`、`compact_memory_file`、 `refresh_global_summary`，带 `global_memory_lock` 跨进程安全。

**Outputs。** 已 commit 的 turn，更新的 memory 层， 推进的 schedule，append 的 notebook，enqueue 的 maintenance（或它的结果，如果同步跑）。

**失败模式。**

- **Commit 失败。** 整个 turn 回滚；用户看到错误， 系统处于干净状态。
- **Memory 抽取失败。** 抽取错误被记但不阻塞 commit；turn 不带抽取的 memory 也是 durable 的 （M1 行还在；recall 降级但没断）。
- **Notebook append 失败。** 同上——记、不阻塞。

## 3 什么跨过阶段边界

状态在不断运动，跨过每个边界能活下来的是什么，是理解 持久性的关键。

| 边界 | 跨过的 | 被重建的 |
|---|---|---|
| **进入 Intake** | channel envelope | （无） |
| **Intake → Plan** | Session、Principal、Workspace、首次 prompt | （无——durable） |
| **Plan → Execute** | IntentPlan、Task 行、mode、policy、model binding | （无——durable） |
| **Execute → Settle** | `AgentLoopResult`、tool trace、subagent 结果、research ledger、event 流 | in-memory messages 数组 |
| **Settle → Render** | `Task.terminal_status`、`result.content`、artifacts、citations | 中间 tool outputs |
| **Render → Persist** | `TurnPresentation`、`TaskNotification`（如果有） | channel 连接 |
| **Persist → 下一次 Intake** | 已 commit 的 `Task` 行、已更新的 memory、推进的 schedule | 整个 `Task` 是 durable 的；只有 `messages` 数组被重建 |

两个事实让系统可控。第一，`Task` 行是规范的 handoff： 它是每个阶段读写的那一个东西，并且对下一个阶段可见之前 已经是 durable 的。第二，in-memory 状态（`messages` 数组、 tool surface、model binding）在每次重启时*被重建*—— 系统被设计成可以从 durable 记录回放。

## 4 九个职责在每个阶段里怎么表现

这张表是本文和 [harness-architecture_cn.md](harness-architecture_cn.md) 之间的导航。一个文档的读者能在这里找到另一文档里的相关 阶段。

| 职责 | Intake | Plan | Execute | Settle | Render | Persist |
|---|---|---|---|---|---|---|
| **1. 主循环** | — | — | 驱动整个阶段 | — | — | — |
| **2. 工具层** | schema 组装 | 能力选择 | 通过 `ToolGateway` 派发 | 结果调和 | citation 渲染 | — |
| **3. 上下文管理** | 首次 prompt 组装 | plan recap | system prompt；context 压缩 | — | — | memory 抽取；notebook append |
| **4. 权限 / 沙箱** | principal 解析 | policy 绑定 | approval gate；OS sandbox | — | — | — |
| **5. 子 agent 调度** | — | subagent plan 条目 | spawn / wait / interrupt | child 调和 | render 里的 subagent 摘要 | event 流里的 subagent events |
| **6. Session / state** | session + principal + workspace | Task 行创建 | event 流、lineage | terminal status | — | 原子 commit、schedule 推进 |
| **7. 可观测** | session open event | plan persisted event | 每步、cost event、hook event | settle event | render event | commit event |
| **8. Hooks** | — | — | 每次调用的 pre-tool / post-tool | — | — | — |
| **9. 错误恢复** | channel 错误处理 | safety finding；needs_input | 分类；circuit；escalate | settlement 冲突；missing child | render fallback | commit 回滚 |

空白格意味着该职责在该阶段没有具体工作。"—"意味着 "职责的性质 active 但没有具体工作发生"（比如，权限在 Render 期间 active，意思是不是 renderer 不能调 mutating 工具，但因为没有 tool call，所以没有权限 check 触发）。

## 5 本文不是什么

这是生命周期视图。它是 [harness-architecture_cn.md](harness-architecture_cn.md) 作为横截面覆盖的同一组九个职责的纵向切片。它刻意停在 file:line 之上、policy 之下。想要已建成的模块图，请看 [architecture.md](architecture.md)。想要用户视角的 invariants， 请看 [agent-runtime-harness.md](agent-runtime-harness.md)。 想要逐行的实现分析，请看 [harness-dimensions.md](harness-dimensions.md) / [harness-dimensions_cn.md](harness-dimensions_cn.md)。
