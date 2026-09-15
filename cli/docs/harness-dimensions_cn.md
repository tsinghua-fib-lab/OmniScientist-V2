# 按维度剖析：OmniScientist agent harness 的工程实现

> 一份按九个维度组织的 OmniScientist agent harness 技术分析。每个主流 agent harness 都必须覆盖这九个职责：主循环、工具层、上下文管理、权限与 沙箱、子 agent 调度、会话与状态、可观测、Hook/拦截点、错误恢复与重试。 每一节末尾给出让本代码库与众不同的设计选择，以及支撑这些选择的 `file:line` 引用。本文档是 [agent-runtime-harness.md](agent-runtime-harness.md) （用户视角 invariants）和 [architecture.md](architecture.md)（模块图）之外的 补充——专注于"按维度展开的实现细节"。

后文把 "agent" 当作 *harness* 问题来讨论，而不是 *model* 问题。 OmniScientist 是 local-first、单进程、单 SQLite 的设计；真正的工作 集中在 model、工具和持久化记录三者之间的接缝处。当某项设计借鉴了 某个已知开源 harness（Codex、Claude Code / openclaw、HelixForge）， 代码里的注释会显式提及——下文在必要时引用这些注释。

---

## 0. 进入维度之前：贯穿全局的设计哲学

代码中反复出现三条主线，值得在最前面点明：

1. **模型不是权威（The model is not the authority）。** 每一种 wire format（工具 schema、event payload、system prompt 块）都是 手工设计的、host 代码校验的，**永远不信任**模型的返回值。 `ToolResultEnvelope` 由 `_host_seal` （`cli/src/omni/core/tool_result.py:144-168`）单向封签——一个 恶意的工具无法伪造 `succeeded` 结果；`classify_tool_error` （`cli/src/omni/core/tool_errors.py:86-127`）先看 host 控制的 字段，再读任何模型产生的字符串。
2. **可达性（reach）与暴露（exposure）是两件事。** 一个工具可以 *reachable*（按名字可被路由），但 *advertised*（在每次迭代的 `tools` 数组中不出现）。这种拆分让 `deferred` 工具变成一种 节省 token 的手段，而不是"模型不知道这个工具存在"的陷阱 （`cli/src/omni/core/react_agent.py:209-225`；见维度 2）。
3. **每条改状态的路径都有一个 host-owned 对应物。** 模型可以请求 一次写入，但 host 会在调用前快照输入、对参数 hash、持久化 `start` 事件、拿文件锁、跑策略，然后 `done` 事件会用 `admitted_arguments_hash` 校验 （`cli/src/omni/runtime/tool_gateway.py:309-475`）。模型无法 事后假装没要求过这次写入。

整个 harness 的工程目标就是：随着功能面扩张，**守住**这三条性质。 任何会侵蚀它们的特性都会被 gate 住——比如：deferred 工具不能 被策略屏蔽的同时仍是 deferred；mutating 工具在 start 事件落不 下来的情况下直接被拒；sandbox 失败是 fail-closed。

---

## 1. 主循环（agent loop / ReAct）

### 1.1 入口与控制流

`ReActLoopAgent` 在 `cli/src/omni/core/react_agent.py:323` 实现，是 bound 形式的 ReAct 风格循环。由 `orchestrator._handle_turn_impl` （`cli/src/omni/agent/orchestrator.py:1410-1437`）实例化，并接收 会话级参数（`max_iterations`、`max_tool_calls`、`max_seconds`、 `stall_timeout_s`、`soft_timeout_s`、`finalization_timeout_s`、 `finalization_attempts`、`soft_token_limit`、 `context_rollover_token_limit`、`microcompact_keep_tool_results`、 `no_progress_threshold`、`require_opening_tool`、 `owes_scientific_outputs`、`bound_skills`、`fact_feed`）。

单次迭代的步骤按顺序为：

1. `register_clock(TurnClock(...))`（`react_agent.py:452-494`）； 循环体跑在 `ExecutionControl.run` 里，这样就能轮询 durable `cancel` 行。
2. `while iteration < self._max_iterations`（`react_agent.py:564`）：
   - `take_steering` 把 `ExecutionControl.steer` 行抽出来， 以 `user` 身份插入（`react_agent.py:575-582`）。
   - 如果已到 soft-timeout 窗口，发一条 `notice kind=soft_timeout` （`react_agent.py:586-603`）。
   - 如果 `clock.expired()`，切到 `_terminate_or_synthesize(reason="timeout")` （`react_agent.py:607-618`）。
   - `normalize_tool_transcript(messages)` 修复未闭合的 `tool_call`/`tool_result` 对，避免下一轮 LLM 看到破损的 transcript（`react_agent.py:619-630`）。
   - `_maybe_microcompact` 收缩老的 tool observations （`react_agent.py:631` → `compaction.microcompact_tool_results:148-184`）。
   - `_maybe_rollover_context` 把接近溢出的窗口折叠成模型写的 JSON checkpoint （`react_agent.py:632-642` → `run_context.py:13-153`）。
   - 累计 token / cost 检查：`_usage_limit_reason` （`react_agent.py:1722-1728`），在 643-669（迭代前）和 1130-1142（迭代后）调用。
   - 如果 `require_opening_tool`，注入 `_OPENING_TOOL_DIRECTIVE` （`react_agent.py:692-699`）。
   - `chat_with_tools_stream` 或 `chat_with_tools` 跑一轮模型 （`react_agent.py:718-740`）。
   - 三条分支：纯文本终止（`react_agent.py:828-903`），`escalate_run` 工具调用变为 `AgentLoopResult(kind="escalated")` （`react_agent.py:914-939`），或派发一批工具调用 （`react_agent.py:946-1276`）。
3. 工具批：`_preflight_rejection`（未知工具 / 坏参数 / 触发熔断器） 在 `react_agent.py:1815-1877`；然后 `ToolExecutionBudget.admit` （`react_agent.py:956-960`）；然后写入 `start` 事件。如果 start 事件落库失败 **且** 工具是 mutating 的，调用以 `tool_start_not_persisted` 阻断（`react_agent.py:974-987`）。
4. `_dispatch_batch` 并发派发剩下的调用 （`react_agent.py:1760-1813`）；只读工具并行，写工具走 `_SERIAL_TOOLS` 串行（`react_agent.py:2270-2294`）。
5. 每次调用写一个 `done` 事件；`tool` 消息附加到 transcript， 供下一轮用（`react_agent.py:1060-1078`）。

`replay_safe` 工具最多重试 `_TOOL_RETRY_MAX = 1` 次 （`react_agent.py:67-68`、`1900-1901`），指数退避 （`_TOOL_RETRY_BASE_DELAY * 2**attempt`， `react_agent.py:1936-1938`）。Mutating 工具**绝不**因网络抖动 重放——start 事件已经记下了意图，重复发一次写入比失败更糟。

### 1.2 终止原因

所有终止都收口到 `cli/src/omni/core/termination.py`。循环自身的 `terminated_reason` 和 `kind` 被映射成用户可见的 `TERMINATION_LABELS` （line 105-150）、`BUDGET_EXHAUSTED_REASONS → _NEXT_ACTIONS` （"re-run with a larger X budget"）、以及最终的 `execution_outcome_status ∈ {succeeded, degraded, failed, cancelled, interrupted}`（line 77-89）。循环实际产生的具体原因：

| `terminated_reason` | 触发点 | 说明 |
|---|---|---|
| `done` | `_OPENING_TOOL_*` 满足后纯文本结束 | `react_agent.py:899-903` |
| `max_iterations` | `while iteration < self._max_iterations` 退出 | `react_agent.py:1278-1286` |
| `max_tool_calls` | `ToolExecutionBudget.admit` 无 slot；记为 `run_hard_budget_exhausted` | `react_agent.py:1019-1038`，`1165-1177` |
| `max_total_tokens` / `max_cost` | `_usage_limit_reason`；也走 `_terminate_or_synthesize` 收尾 | `react_agent.py:122, 1722-1728` |
| `no_progress` | `stalled_patterns` 重复 ≥ `no_progress_threshold`（默认 2） | `react_agent.py:1080-1108`，`1179-1190` |
| `timeout` | `TurnClock.expired()`；或 LLM `TimeoutError` 且 `clock.expired()` | `react_agent.py:607-618`，`755-779` |
| `stalled` | `IdleWatchdog` 静默超过 `stall_timeout_s`；或 LLM `TimeoutError` 且仍 `trace` | `react_agent.py:762-779`；`core/llm/idle.py:30-123` |
| `cancelled` | `ExecutionControl.request_cancel` 或 durable `cancel` 行 | `execution_control.py:113-141`；`react_agent.py:741-754, 2029-2053` |
| `interrupted` | `dispatch_cancelled` 时一个工具还在执行中 | `react_agent.py:992-1014`；`tool_result.py:36-60`（`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`） |
| `escalated` | 模型调用了 `escalate_run` | `react_agent.py:914-939` |
| `malformed_tool_calls` | `function.name` 为空；最多 `_MAX_MALFORMED_CORRECTIONS = 2` 次纠正 | `react_agent.py:845-866` |
| `required_opening_tool_missing` | 开启 `require_opening_tool` 但没产生 productive observation | `react_agent.py:867-881` |
| `output_cap_truncated` | `truncated_by_output_cap` | `react_agent.py:882-893`；在 `_synthesize_final` 里也再判一次（`react_agent.py:1422-1433`） |
| `synthesized_<reason>` | `_terminate_or_synthesize` 跑了收尾 LLM | `react_agent.py:1288-1438`；`_COMPACT_WRAP_REASONS` 触发预压缩 |

收尾 LLM 是所有"spend"原因的安全网。预算耗尽、stall、timeout 都 落在 `_COMPACT_WRAP_REASONS = _BUDGET_REASONS | {"stalled", "timeout"}`（`react_agent.py:126`），在最后一次合成前 `microcompact_tool_results(keep_last=2, max_chars=400)` （`react_agent.py:1367-1374`）。合成调用使用 `tool_choice="none"`， 所以它不能再发起新工具调用；如果 `finalization_attempts` 轮后 仍失败，`_salvage_content` 写一个 stub 结果而不是空内容。

### 1.3 反思、steer、命名压力

循环**永不**在 wire 上发 `tool_choice="required"`。原因在 `react_agent.py:150-154`：

> We never send a provider `tool_choice="required"` because not every upstream honors it (some reject it with a hard 4xx). Instead we steer with a prompt nudge and verify the call landed, mirroring openclaw's tool_choice contract; codex likewise always sends `"auto"` on the wire.

起到"反思"作用的 prompt nudge：

- `_OPENING_TOOL_DIRECTIVE` + `_OPENING_TOOL_CORRECTION` （`react_agent.py:155-162`）；一轮纠正即可 （`_MAX_OPENING_CORRECTIONS = 1`）。
- `_MALFORMED_TOOL_CALL_CORRECTION`（`react_agent.py:173-178`）； 最多 `_MAX_MALFORMED_CORRECTIONS = 2`。
- `CONTRACT_HUNT_STEER`（`react_agent.py:94-99`）—— 当尾部 `_CONTRACT_HUNT_TOOLS = {find_skill, docs_search, docs_read, glob, search_tasks, list_dir}` 出现 ≥ 2 次时触发。
- `CONTRACT_NATIVE_WRITE_STEER`（`react_agent.py:103-107`）—— 对 `find_skill` 返回 0 card 的尾部空查询；正确做法是 `write_file` 或用已经返回的 `input_schema` 调 `run_skill`。
- `CONTRACT_HUNT_CONSUME` / `CONTRACT_HUNT_STOP` （`react_agent.py:111-118`）—— 类比 Codex Stop-hook， `_replay_hunt_consume`（`react_agent.py:1678-1694`）在研究 feed 报告"债"的时候触发。
- `LOOKUP_STEER` 在 `cli/src/omni/core/scientific_progress.py:66-72` —— `LOOKUP_TOOLS = {memory_search, memory_get, search_tasks, list_recent_tasks, get_task, get_subtask, open_artifact, list_session_artifacts}` 累积，由 `lookup_pressure` 触发 （line 122-141）。
- `bound_skill_steer`（`scientific_progress.py:82-89`）加 `leftover_skill_pressure` 拦截用 `bash`/`run_compute` 写 已绑定到某个 skill 的产物（`react_agent.py:1254-1259`）。

Stall 检测：签名被 hash 成 `<name>:<json-dump-args-sorted-key>` （`react_agent.py:1082-1085`），这样长错误文本不会让 key 漂移。 `_hunt_window` + `_contract_hunt_pressure`（`react_agent.py:2115-2204`） 区分"为另一个交付物而 disjoint 的第二次 `find_skill`"（给 `figure` 然后 `slides` 准备）和"同一 contract 的重复查找" （这是 BUG-11，会被 steer）。

### 1.4 后台升级

`ESCALATE_RUN_TOOL_NAME = "escalate_run"`（`react_agent.py:65`） 是一个专门用途的工具。schema 见 `build_escalate_run_tool_spec`（`react_agent.py:304-320`）—— 只有 `allow_escalation=True` 时才注入到 catalog（`react_agent.py:510-511`）。 当模型调用它时，`AgentLoopResult.kind = "escalated"`，携带 `escalated_goal` / `escalated_reason`。

交接逻辑在 `cli/src/omni/agent/turn_escalate.py:12-61`：

- `maybe_escalate_run` 调 `agent.tasks.create_task(kind="escalated", depth=parent.depth+1)`。
- 如果父任务已经是 `escalated` 或 `depth ≥ 2`，拒绝递归 （一个没工具的空 turn 是这个 guard 要防的失败模式）。
- `inherit_research_ledger` 传递 Research Object Model（ROM）的 指针——claim、source、evidence、artifact——让后台 turn 在 同一份记录上接续。
- `asyncio.create_task(_run_escalated_turn, name="escalate:<id>")` 在后台跑 `agent.handle_turn(drain_tasks=True, origin="schedule")`，走 同一个 scheduler，这样 cron/IM 通道看到的是一个 durable 任务， 而不是会随 reload 消失的内存任务。

### 1.5 三层时间预算

运行时区分三个时间尺度（`react_agent.py:370-380` 加上 `_react_max_seconds` 在 `orchestrator.py:1532-1544`）：

- **Stall watchdog。** `IdleWatchdog` + `await_with_idle` （`cli/src/omni/core/llm/idle.py:30-123`）。循环的 `_on_delta` （`react_agent.py:703-706`）和 `on_activity=watchdog.tick`（`react_agent.py:723`）每次 delta 都重置。流式响应需要 wire token sink 或开启 watchdog； `use_stream`（`react_agent.py:715-717`）做选择。 `retries_on_idle` 在客户端把重连时的 `wait_stall` 设为 0 （`react_agent.py:730-734`）。
- **硬墙钟。** `TurnClock(max_seconds)` （`cli/src/omni/core/turn_clock.py:38-42`）是一个单调 deadline。`clock.expired()`（line 56-58）触发 `_terminate_or_synthesize`。`pause_enter` / `pause_exit` 引用 计数（line 65-76）让 approval-gate 等待不消耗墙钟； `pause_clocks` + `ContextVar`（line 89-118）跨 task 共享 这个 pause。
- **柔通知。** 每次迭代顶端检查 `clock.remaining() <= max_seconds - soft_timeout_s`；为真时 发一条 `notice kind=soft_timeout`（`soft_notified` 标志 防止重复），循环继续——soft timeout 不停循环，只告诉模型 时间短了。

`finalization_timeout_s = 45` 和 `finalization_attempts = 2` （`react_agent.py:380-381`）是收尾 LLM 的预算；如果两次 合成都失败，`_salvage_content` 写一个 stub，让用户得到一个 非空的终止状态。

### 1.6 取消（Cancel）

`ExecutionControl.run`（`cli/src/omni/core/execution_control.py:166-201`） 通过 `asyncio.ensure_future` 启动协程并轮询 durable 控制表 里的 `cancel` 行。`request_cancel` 是 process-local；durable 端 （`_durable_cancel`，`execution_control.py:132-136`）让 cancel 跨进程重启有效。`delivered_control_ids`（line 73-86）让上层 区分"owner 按了 stop"和"服务器重启"——后者**不该**取消一个 后台 turn。

`asyncio.CancelledError` 被收口到 `_cancelled_result` （`react_agent.py:741-754, 2029-2053`）。对已经在执行的工具， 循环用 `interrupted_tool_payload` 填充 （`react_agent.py:992-1014`），用 `tool_result.py:36-60` 的 `TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`——让模型看到"确实 发生了什么"，而不是编造一个假结果。

### 1.7 主循环刻意不做的事

- **不做 per-tool 超时。** Turn clock 和 per-LLM-call timeout 已经 包住了单次工具调用。再加一层 per-tool 墙钟能拦下的失败模式 多数已经被收尾 LLM 拦下，代价是 `cancel_persist` 还要多穿一条 取消轴。
- **不做"我是不是卡住了"的逐轮反思。** 一个 `no_progress_threshold`（默认 2）抓"重复签名"模式，hunt-consume steer 在 prompt 层发。设计赌的是：prompt 层的 steering 比 一次昂贵的"meta-prompt: 你卡住了么"更快收敛。

---

## 2. 工具层（tool registry / function calling）

### 2.1 工具面组装

`ToolSurfaceBuilder.build`（`cli/src/omni/agent/tool_surface.py:47-93`） 是 catalog 组装器。顺序有意义且有测试覆盖：

1. Builtin 工具（fs / docs / shell / compute / web / plan / research / recall / delegate）—— `build_builtin_tools` 在 `cli/src/omni/skills_runtime/builtin_tools/__init__.py:29-57`。
2. 通过 skill registry 注册的 sync skills。
3. `find_skill` / `run_skill` / `run_workflow`（meta-tools）。
4. Schedule 工具（cron 表面）。
5. MCP servers。
6. 外部 integrations。

每个 Omni-owned 工具都标记 `outcome_resolver=owned_result_outcome`（line 54-56），把 OpenAI 的 `command_result` schema 路由到内部的 `ToolCallOutcome` （`tool_result.py:194-221`）。同一个面在循环看到之前会被 `filter_tools_for_policy`（`tool_policy.py:10-22`）过滤，所以 策略拒绝**不是**运行时"unknown tool"惊吓。

### 2.2 ToolSpec 和 host-only metadata

`ToolSpec`（`react_agent.py:204-235`）是 wire / host 的拆分点：

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    replay_safe: bool = False
    exposure: Literal["direct", "deferred"] = "direct"

    def to_openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }
```

注释显式说明（`react_agent.py:209-225`）：

> `replay_safe` and `exposure` are host-only execution metadata. They are intentionally excluded from the provider-facing tool schema below: a model cannot grant replay authority.
>
> Advertising and reachability were previously the same list, so withholding a tool to save tokens also removed it, and the model was told `unknown tool 'write_file'` after doing the work it needed to save. Keeping them separate is what makes that outcome unrepresentable rather than merely unlikely: only `ToolPolicy` denial removes reach. Codex draws the same line — `build_model_visible_specs` filters advertised specs by exposure while `ToolRegistry::tool` dispatches by name without consulting it, so a deferred tool the model names anyway still executes.

运行时，`tool_specs`（advertised，`exposure == "direct"`， `react_agent.py:518-519`）和 `tools_by_name`（reach，全名， `react_agent.py:520`）独立构建。Deferred 工具首次被调用时 自动 promote 到 advertised（`react_agent.py:1055-1057, 1114-1117`）。

### 2.3 JSON-Schema 编译

`cli/src/omni/core/tool_contracts.py:252-333` 的 `prepare_json_schema` 做一次离线的 schema 编译：

1. `copy.deepcopy`（让 provider 的副本不能修改 registry 的源）。
2. 按 JSON-Schema 规范 `check_schema`。
3. `referencing.Registry(retrieve=_deny_external_schema_retrieval)` — 锁死外部 `$ref` 解析；任何 skill 都不能在校验时从网络拉 schema。
4. `crawl` 预解析所有 `$ref`、`$dynamicRef`、`$recursiveRef` （`tool_contracts.py:410-464`）。
5. 失败返回 `PreparedJSONSchema(validator=None, definition_errors=...)`，fail-closed。

`ProviderInputCompiler.compile_entry`（`tool_contracts.py:47-71`） 做"声明 schema → resolver 填关键字段 → required/type/enum 检查 → 严格 unknown 检查"；`_resolve_declared_fields` （`tool_contracts.py:73-104`）让根级 `field_resolver` 自动注入 时间、附件等环境字段。`admit_provider_arguments` （`tool_contracts.py:572-600`）对 plan 已经看过的参数再校一次， 所以 LLM 不能把被 planner 拒掉的字段偷偷塞进运行时。

### 2.4 校验链（按顺序）

模型发起的工具调用按以下顺序过闸门；任意一层拒绝就停：

1. **Preflight**（`react_agent.py:1815-1877`）：名字必须在 `tools_by_name`（reach）里，否则 `error_code="unknown_tool"`。 关键：未知工具**不会**让循环从 `tools_by_name` 里把它 删掉——exposure 和 reach 是解耦的。
2. **Circuit breaker**：per-tool 计数器 `self._circuit[_circuit_key(tc)]` （`react_agent.py:138, 2081-2095`）；meta-tool 的 key 用 `_META_TOOL_SUBJECT_ARGS`（`react_agent.py:138-142`—— `run_skill` 按 `skill.name` 取 key，不是按 `run_skill` 本身）。超 `_CIRCUIT_BREAKER_MAX = 5` 触发 `tool_circuit_open` 拒收。这正是"一个坏 skill 不让 路由器拖垮整个 catalog"的护栏。
3. **Argument parse**：区分 `arguments_invalid` 与 `arguments_truncated`。后者是输出 token cap 截断，会得到 一个不同的 steer（"用更小的参数"），因为模型可以自纠正 （`react_agent.py:1848-1855`）。
4. **Tool policy**：`ToolPolicyGuard`（`tool_policy.py:98-156`） 做 `authorization_rejection`（allowed/blocked 列表）和 `budget_rejection`（`max_tool_calls`、`per_tool_limits`）。 拒绝的调用**不消耗预算**——注释写得很直白："A refused call executes nothing, so it must cost nothing" （`tool_policy.py:142`）。
5. **Contract / input schema**： `tool_contracts.admit_provider_arguments` 把模型 JSON 投影到 skill 的 `input_schema`。
6. **Approval gate**：`orchestrator._build_tools` （`orchestrator.py:1360-1372`）把 tools 交给 `ToolGateway(task_id, tools=..., approval_gate=...)`。 `approval_tools_for_plan` 与 `SENSITIVE_TOOLS` （`core/approval.py:91-95`）列出 `bash/write_file/edit_file/apply_patch/run_compute/log_run` 等。
7. **Mutating 守卫**：`_emit_event("start", ...)` 返回 `False` 时以 `tool_start_not_persisted` 阻断 （`react_agent.py:974-987`）。
8. **Result adapter**：`owned_result_outcome` / `fs_result_outcome` / `recall_result_outcome` 解析 Omni / FS / Recall schema（`tool_result.py:194-270`）。 `HostToolRejection(_host_seal)` 在 outcome 上单向封签 （`tool_result.py:144-168`）。
9. **错误分类**：`classify_tool_error` （`tool_errors.py:86-127`）产出 5 种稳定码： `INVALID_ARGS` / `RETRYABLE_IO` / `SKILL_FAILED_PARTIAL` / `UNPAYABLE` / `FATAL_TURN`。`short_skill_observation` （`tool_errors.py:130-168`）附上 `_remediation_hint`，让 下次尝试不再是"再猜一次"——VLM 503 拿"retry"提示，而不是 "VLM 未配置"（`tool_errors.py:312-335`）。

### 2.5 Mutating 和 replay_safe

`tool_is_mutating`（`cli/src/omni/runtime/execution_policy.py:204-211`） 枚举三组 mutating 族：

- `_FILESYSTEM_MUTATIONS = {write_file, edit_file, apply_patch}`
- `_EXECUTION_TOOLS = {bash, run_compute}`
- `_STORE_MUTATIONS = {add_evidence, build_research_artifact, cite_source, log_run, record_claim, record_hypothesis, record_run, remember, run_skill, run_workflow, search_literature, submit_task}`

start 事件持久化规则（上面）一致适用于三族。

`replay_safe` 是 skill manifest 自己的字段 （`replay_safe=sk.replay_safe`，`agent/subagents.py:207`）； ReAct invoker 只有在工具可以被安全重放时才会 attach `owned_result_outcome`。Mutating 工具永远不会被标 `replay_safe`，所以重试预算只会花在可安全重发的读侧调用上。

### 2.6 Exposure vs reach — 为什么重要

`cli/src/omni/core/tool_exposure.py:50-71` 定义 `DEFERRED_TOOLS`， 一组 18 个家族工具，占 50-tool surface ~35% 的 schema token， 但只占 4.2% 的调用。`apply_default_exposure`（line 74-84）在 catalog 末尾 mutate `tool.spec.exposure = "deferred"`，但**保留** 在列表里——所以 system prompt 的 catalog （`render_tool_catalog` 在 `core/system_prompt.py:184-199`） 把名字以"schemas omitted"列出，`find_skill` 可以把完整 schema 拉回来。

Reach 端不变。`tools_by_name` 在 `react_agent.py:520` 从完整 surface 构建；deferred 工具正常派发。模型若直接点名了一个 deferred 工具，调用照样执行（只是下次迭代开始付完整 schema 的 token 钱——因为循环会 promote 它）。

`spawn_subagents` 是唯一的例外（`tool_exposure.py:17-25`）： 虽然 schema 大，但它是"discretionary"的——没有它模型不会 想到要 delegate——所以一直留在 advertised 集合里。

### 2.7 重试、降级、优雅退化

- **网络级重试** 按异常名匹配（`_RETRYABLE` 在 `react_agent.py:143-148`）加 HTTP 状态 429 / 5xx。重试预算 `1 if replay_safe else 0`，指数退避优先用 `Retry-After` header。
- **Per-tool circuit breaker**（`_CIRCUIT_BREAKER_MAX = 5`， `react_agent.py:69`）在某个 `(tool, args)` 对上跳闸。Meta-tool 的 key 通过 `_META_TOOL_SUBJECT_ARGS` 重写，所以一个 坏 skill 名不会毒化其他 skill。
- **Unexecuted-call streak**（`_MAX_UNEXECUTED_CALL_STREAK = 5`， `react_agent.py:70-76, 1265-1276`）是给"被拒"调用 （`unknown_tool`、`tool_arguments_invalid`、 `tool_arguments_truncated`、`tool_policy_rejected`、 `tool_contract_violation`、`tool_approval_required`） 设的对称护栏。连续 5 个，循环以 `no_progress` 终止——模型 正在幻觉工具或参数，不应该再烧预算去教它别这样。
- **Provider HTTP 5xx/429** 归类 `RETRYABLE_IO`；unpayable 条件（`vlm_unavailable`、`node_unavailable`、 `pptx_unavailable`）拆出来作为 `UNPAYABLE`，不触发空重试 （`tool_errors.py:115-119`）；`_looks_config_unpayable` （`tool_errors.py:263-281`）按签名判定，所以瞬时 503 不会被误判为"VLM 没配"。
- **Context overflow** 三层回退：模型写 JSON checkpoint → `evidence_checkpoint`（host-owned、确定性） → `microcompact_tool_results`（Codex 风格的老观测头尾裁剪； 见维度 3）。
- **Slot fallback**（`slot_routing.allow_slot_fallback`， `skills_runtime/slot_routing.py:71-89`）：当 `livefigure` 准入 失败时，`scientific-figure` 允许接管 `artifact.figure` slot。 可编辑 / 已命名的 slot 永不 fallback；名字对产物身份是 权威的。
- **单 skill 失败 fallthrough**：`SkillTaskRunner.run` （`capability_runners.py:97-252`）返回 `handled=False, terminated_reason="single_skill_failed"`， 让 ReAct 循环把 turn 收干净 （`capability_runners.py:211-228`）；路由失败不会被误 当作 verdict。
- **`needs_input` 收集**：`run_skill` 在 `WorkflowNeedsInput` 时返回 `{"status":"needs_input",...}` （`tool_surface.py:402-410`）；循环检测到这个 outcome 就调 `_compose_terminal_needs_input`，让模型用用户语言重写 问题（`react_agent.py:1144-1163, 1440-1514`）。
- **Unknown tool fallback**：preflight 拒绝 payload 列出按名字 排序的可用工具（`react_agent.py:1822-1828`），并 bump unexecuted-streak 计数；模型下一轮可自纠正。

### 2.8 工具层刻意不做的事

- **不做远程 schema 校验。** 一个 skill 若过不了 `prepare_json_schema`，注册时就被拒。运行时不会试着 在飞行中修复 schema。
- **不做影子 registry。** 一个工具要么在 `tools_by_name` 里（reachable），要么不在。没有"注册了但被配置禁用" 这个概念——那是 `ToolPolicy` 的事，策略作用在已知列表上。
- **不做模型控制的 replay。** `replay_safe` 是 manifest 字段， 不是模型提供的提示。模型可以请同一个工具再调一次，但循环 不会替它重发 mutating 工具。

---

## 3. 上下文管理（context manager）

### 3.1 System prompt 组装

`build_system_prompt`（`cli/src/omni/core/system_prompt.py:261-323`） 是六段式，其中**三段**根据 turn catalog **条件切换**：

1. `role`（identity）+ 可选 `persona_overlay`，由 `persona_stoma.load_turn_persona_overlay` 提供 （`turn_prompt.py:51`）。
2. `render_tool_catalog(tools)`（`system_prompt.py:170-199`）—— 只列名字；完整 schema 在 wire 上。Deferred 工具以 "Also available, with schemas omitted" 列出。
3. `render_tool_guidance(tools)`（`system_prompt.py:24-55`）—— `[Tool use]` 规则。`has_docs` 标志（line 53）按本 turn catalog 有没有 `docs_search` 来切换"通过 docs 重新发现"的措辞。
4. `render_planning(tools)`（`system_prompt.py:129-145`）—— 只有 `update_plan` 在 catalog 时才输出。Codex 风格的 plan 工具。
5. `render_local_environment(tools, working_dir)` （`system_prompt.py:202-258`）—— 只有 `bash` / `write_file` / `apply_patch` 存在时才输出。
6. `render_self_knowledge(tools)`（`system_prompt.py:102-127`）—— **最**条件化的段：有 `docs_search`，prompt 让模型用 docs grounding；没有，prompt 切到"凭通用知识回答，标记未验证 部分"。`render_behavior(tools)`（line 84-88）对 `write_file` 做同样的切换（longform / shortform 行为）。

六段之后是：

- `project_memory` 来自 `load_curated_memory` （`memory/files.py`）—— `MEMORY.md`、`AGENTS.md` 等。权威的， 放在 recalled memory **之前**，保证 operator 写的胜过 模型学的。
- `memory_block` 由 `assemble_react_system_prompt` （`cli/src/omni/agent/turn_prompt.py:20-84`）：`clarification`、 `react_context_block`、`assumption_block`、`unpayable`、 `bound_skill`、`context_summary`、`referenced`、`thread_brief`、 `research_brief`、`compiled_memory`、`skill_catalog`。
- `recent_activity` —— 最近 6 个 principal 范围的 task （`agent/recent_activity.py:43-100`）。
- `repo_history`。
- `[Session context]`：cwd、OS、`OMNI_OUTPUT_DIR`、`TMPDIR`、 lab notebook 摘要（`memory/notebook.py:11-26`）。

"按 catalog 切换"的模式与 Codex 同源：prompt 叙事随本 turn 模型**能做什么**而变，而不是随某个其他 turn 模型**能做什么**变。

### 3.2 历史装载

`messages = [system, *history_filter, user]` 在 `react_agent.py:522-532`。`history_filter` 只保留 `{role, content, name, tool_call_id, tool_calls}` 五 key—— 不注入元数据。模型看到的是对话，不是 run 框架。

### 3.3 两阶段截断

运行时区分两种"压缩"事件，名字都是 "compaction"，但干的是 不同的活：

#### 3.3.1 每轮的 microcompact

`_maybe_microcompact`（`react_agent.py:1516-1535`）调 `compaction.microcompact_tool_results(messages, keep_last=N, max_chars=M)`（`compaction.py:148-184`）。策略是老的 `role="tool"` 消息头尾裁，**两条**特例：

- `_keep_failed_observation`（line 187-194）跳过 `status ∈ {failed, error, rejected, timed_out}`，所以失败 不会被重新压成"一切正常"。
- `_preserved_research_tokens`（line 197-203）保护 `[S#]`、 `source_id=`、`task_id=`、`artifact://X` 锚点，让模型 仍能 cite。

这一对是 `tool_call ↔ tool_result` 在压缩后仍配对的保证： `tool_call` 不动，只动那条长 result 字符串。

#### 3.3.2 跨轮的 rollover

`RunContextWindow`（`cli/src/omni/core/run_context.py:13-153`） 是硬上限层。`should_rollover`（line 34-45）在 wire 序列化 token ≥ `limit` **且**历史里有 tool 消息时触发。压力估算 （`run_context.py:21-32`）是 `json.dumps` + `estimate_tokens`—— 所以计的是 *序列化* 大小，message 对象通常比 wire 形态 小。

`continue_with`（line 47-122）二分搜索把窗口填到阈值。模型 被要求写 JSON checkpoint（`react_agent.py:1555-1603`）时， schema 由 `parse_rollover_checkpoint` 解析、 `format_rollover_checkpoint` 渲染（同 line）。如果模型写不出 有效 checkpoint，运行时回退到 `evidence_checkpoint` （`run_context.py:156-179`）：由 trace 派生的、host-owned 的确定性 ledger，上限 16 000 字符。回退存在的原因是 **唯一比坏 checkpoint 更糟的就是没有 checkpoint**。

### 3.4 单观测硬顶

`observation_max_chars` 默认 8 000（`react_agent.py:351`）。 溢出走 `compact_observation(value, max_chars, spill_dir=observation_spill_path)` （`cli/src/omni/core/observation.py`，在 `react_agent.py:1654-1661` 调用）：正文写到 `~/.omni/spill/...`，模型看到的是一个头尾预览 + 内嵌的 恢复路径。模型若需要全文可以用 `read_file` 读回。

`truncation.formatted_truncate_text` （`cli/src/omni/core/truncation.py:51-83`）保留头 1/3 + 尾 2/3， 带 `original token count` 和 `Total output lines` 警告。 `command_output_window`（`tool_result.py:410-426`）对进程 输出用同策略。

### 3.5 Token 估算

`compaction.py:117-145` 支持两种估算器：

- `tiktoken` `cl100k_base`（provider-token 精度），装了的时候用。
- 一个 3 字节桶（`ascii-word 5.0`、`ascii-punct 1.5`、 `non-ascii 1.9`），没装的时候用。注释明说每个数都校准到 **略多算**，所以压缩阈值不会偷偷放过超限请求。

### 3.6 跨 turn 会话压缩

`SessionCompactor`（`cli/src/omni/agent/session_compactor.py`） 是 durable 层。触发器 `maybe_compact` 在 `session_compactor.py:57-71`：

```python
if sum(estimate_tokens(row.content) for row in rows) > session_compact_token_budget():
    ...
```

阈值是 *token* 预算，不是消息数。注释 （`session_compactor.py:27-29`）点名：Codex 风格，"成本到了 才压缩，条数凑齐不压"。

流程（`compact` 在 line 73-184）：

1. `MemoryService.extract_session(older_msgs, principal, on_llm_call)` 把 durable facts flush 到 M3/M4 （`service.py:689-750`）；只 seed "真"的 user 消息， 过滤 `kind=error/partial` 与 `terminated_reason ∈ _DEGRADED_TERMINATED` （`_is_low_value_assistant_turn`，line 107-123）。
2. `summarize_messages`（`compaction.py:289-337`）：有真 provider 就让它产 ≤ 8 个 bullet，保留 research goal、决策、 `artifact://X`、task id。Output cap 截断时回退到 `_heuristic_summary`（line 368-396）：抽 user asks、最近的 assistant turns、任何 `artifact://` / `task_id` 引用。
3. 写一条 `compaction` 消息到 store，把被覆盖的 rows 标 `compacted`——对 prompt 隐藏，但仍保留供回放。
4. `bridge_budget`（line 172-174）把新 bridge 折进剩余预算； 多次 fold 时旧 bridge 折进新 summary，但首窗口保留 （line 104-115）。

`_COMPACT_THRESHOLD = 30`（`session_compactor.py:29`）**不是** 触发器；它是给 `/context` 报告的提示，让用户看到"再 N 轮就 会压缩"。

### 3.7 Turn-end 记忆

`TurnMemory`（`cli/src/omni/agent/turn_memory.py`）每 8 轮跑 一次（`_CONSOLIDATE_EVERY` 在 line 32，line 102-107 调用）。 session 结束时，交互式 channel 把维护任务 enqueue （`enqueue_session_maintenance`，line 141-153）；CLI 同步 `end_session`。维护任务跑 `decay_and_dedup` + `rebuild_user_profile` + `compact_memory_file` + `refresh_global_summary`（line 279-302）。跨进程安全由 `global_memory_lock`（`memory/locks.py`）保证。

`redact_secrets`（`memory/sanitize.py:29-36`）在任何东西落 `MEMORY.md` 或 `memory_entries` 之前跑，这样模型回复里 "碰巧"露面的 API key 不会进存储。

### 3.8 五层记忆

`cli/src/omni/memory/service.py:140-154` 定义这些层：

| 层 | 名字 | 范围 | 跨会话？ |
|---|---|---|---|
| M1 | SESSION | 原始对话 | **否**（line 149-154——显式排除） |
| M2 | TASK | 任务结果 | 任务范围 |
| M3 | EPISODIC | 会话摘要 | 是 |
| M4 | SEMANTIC | durable 事实 | 是 |
| M5 | ARTIFACT | artifact 引用 | 是 |

M1 的排除是 memory 模块最重要的规则。没有它，"用户说 他们要加一节"会在两周后被当作 durable 事实被 recall 回来。 有了它，对话 row 留在了产生它的对话里。

### 3.9 Principal 隔离

`PRINCIPAL_OWNER = "local"`（`memory/service.py:170-201`）是 CLI / MCP / 共享 baseline。`channel_identity=owner` 时所有 IM peer 共享 owner memory。`per_peer`（默认）时每个 IM 身份 = `<channel>:<external_key>`，各自一个 principal。 `_principal_visible`（line 204-212）保证一个 peer 看不到 另一个的 memory。

store router（`service.py:281-307`）按 scope 写：

- `scope == "user"` / `memory_type == "user_profile"` / `layer == EPISODIC` → global（跨 workspace identity）
- 其它 → workspace

`open_global_store`（line 39-61）是 global handle，独立于 per-workspace DB。

### 3.10 Recall（混合检索）

`cli/src/omni/memory/service.py:453-558`：

- **Bounded candidate。** 硬顶 `memory.recall_candidate_limit` 默认 200（line 481-487）。 这个 cap 是为了化解 `limit` 注入攻击。
- **Scope 过滤。** Pin / `(session, session_id)` / `(task, {subtask_id, session_task_ids})` / 跨会话 `_cross_session_layers(requested)`（M3/M4/M5）/ 显式 `scope` 列表。`principal ∈ {self, OWNER}` 被强制。
- **Score。** `_score` = recency + importance + pin + cosine （有 embedding 的话）。向量端在 `cli/src/omni/memory/vectors.py:23-31`（纯 Python 路径）和 `vectors.py:56-65`（opt-in `sqlite-vec` `vec0` KNN）。两边 产出相同的 `similarity_scores`（line 112-135），所以切换 不会改变 ranking。
- **Graph spread。** `MemoryGraph.spread(seeds)` （`service.py:560-602`）：从 top hit 出发走 1–2 hop， 提升邻居分，把提升后的邻居拉回候选集。每 store walk 后用 max-boost reduce 合并。
- **Render。** `build_recall_block`（`service.py:663-687`） 格式化为 `[layer/type·scope]📌stale - summary[:240]`， 按 char 预算裁。

### 3.11 Lab notebook 和 library

`memory/notebook.py` 写一份人类可读的 `## stamp — title #tag\nbody` block 到 `<workspace>/NOTEBOOK.md`。 git 友好；充当 system prompt 能引用的"summary view"（在 `[Session context]` 末尾的 `Lab notebook summary` 行）。 `read_recent(max_chars=800/1500)` 是 loader。

`memory/library.py:1-100` 是 `library.jsonl` 引文表，按 arxiv_id / DOI / 归一化 title 去重；它是 M5 artifact 的 人类可读对位，给 `omni cite export` 出 BibTeX / JSON / CSV。

### 3.12 陈旧化与衰减

`memory/policy.py` 定义半衰期：

- `NON_DECAYING_TYPES = {preference, user_preference, user_profile, decision, methodology, constraint}` —— 永不衰减。
- `finding = 45d`、`dead_end = 365d`、`idea_evolution = 180d`、 `episode = 30d`、`note = 60d`、`user_note = 120d`。
- `is_stale` 给 recalled entry 打 `stale` 标；pinned 永不衰减。
- `decayed_importance` 地板 0.1（line 77-88）保证 keyword recall 不会让一条事实掉到不可见。

### 3.13 上下文管理刻意不做的事

- **不做细粒度的 per-tool-call token 记账。** 关心累计花费 多过关心单次花费。循环按总量 bound，不按单步天花板。
- **不自动重写 system prompt。** Prompt 每轮都从 live catalog 重建；没有缓存的"user prompt"会跟现实漂移。代价是重算， 收益是没有"为什么模型看不到新工具"这类 bug。
- **不做 memory 软删除。** `compacted` row 对 prompt 隐藏， 但留在磁盘上供回放。重 promote 是 `replay_hunt_consume` 用来喂 research ledger 的机制。

---

## 4. 权限与沙箱（sandbox / permissions）

### 4.1 单一网关

每个工具调用——来自模型、来自 skill、来自 subagent——都过 `ToolGateway.invoke_operation` （`cli/src/omni/runtime/tool_gateway.py:164`）。流水线统一 （`tool_gateway.py:290-510`）：

1. 快照 `admitted_arguments_hash = copy.deepcopy(arguments)`。
2. 用工具的 schema 校验输入。
3. 授权（policy + approval + preauthorizer）。
4. 预算检查（`ToolPolicyGuard`）。
5. 持久化 `start` 事件。
6. 如果是 mutating，持久化必须成功——否则 `policy_violation("start_event_not_persisted")` 拒收。
7. 执行。
8. 用工具的 result schema 校验输出。
9. 持久化 `done` 事件；事后 hash 校验用**同一个** `admitted_arguments_hash`，所以 start 和 done 之间换参数 会被拒。

这就是"模型无法事后假装没要求写入"这条性质的机械形态。

### 4.2 Mutating 与 replay_safe（重述）

见维度 2 §2.5。要点：三组显式 mutating 集合 （`_FILESYSTEM_MUTATIONS`、`_EXECUTION_TOOLS`、 `_STORE_MUTATIONS`），`replay_safe` 是 manifest 字段， mutating 工具在网络抖动时绝不重放。

### 4.3 敏感路径

`cli/src/omni/core/sensitive_paths.py:22-58` 是单一权威。 四类，每类不同的强制：

- **`SENSITIVE_GLOBS`** —— case-insensitive 文件名匹配： `secrets.toml`、`.env`、`.env.*`、`*.key`、`*.pem`、`*.pfx`、 `*.p12`、`id_rsa`、`id_rsa.*`、`id_ed25519`、`.netrc`、 `.pgpass`、`*.credentials`、`credentials.json`、`*.secret`、 `*_secret`。
- **`SENSITIVE_DIRS`** —— `.ssh`、`.gnupg`、`.aws`、`.gpg`。
- **`VCS_PROTECTED_DIRS`** —— `.git`、`.hg`、`.svn`。**写这里 就是代码执行。**
- **`STATE_PROTECTED_DIRS`** —— `.omni`、`.agents`、`.codex`。 是 omni 自己的控制面。

`is_write_protected_path`（`sensitive_paths.py:84-92`）是消费者。 注释精确：没有任何 `output_roots` 配置能放松 VCS 保护——无论 workspace 在哪，`.git` 永远被拒。

`is_sensitive_target`（`sensitive_paths.py:116-131`）同时查 **名**和符号链接解析后的**目标**，关掉"先写一个 symlink， 再透过它读敏感文件"的 TOCTOU bypass。

`bash` 在 `cli/src/omni/skills_runtime/control_store_guard.py:31-48` 拿到等价保护：`command_writes_frozen_control_store` 抽 quoted 和 bare 路径，对 mutating 动词正则 （`touch/rm/rmdir/mv/cp/mkdir/chmod/chown/tee/...`）+ 解释器 （`python/sqlite3/ipython`）+ 重定向做匹配，返回会被碰的 frozen-control-store 路径。bash 工具返回结构化拒绝。

### 4.4 Approval gate

`ApprovalGate`（`cli/src/omni/core/approval.py:321-803`）是 单一同意层。它 wrap ReAct invoker（line 379-383），对安全 工具 pass-through，对敏感工具拦。

决策顺序（`approval.py:399-582`）：

1. `policy == "never"` → 返回 `None`，不查（自治模式）。
2. `classify_tool_call` 对照 `SENSITIVE_TOOLS = {bash, write_file, edit_file, apply_patch, run_compute}`（line 58）；`force_sensitive` manifest flag 让非内置工具 opt-in。
3. `SessionApprovalStore.match` —— exact grant / rule / task-bash （见 §4.5）。匹配就自动放行并发 `approval.auto` （line 421-437）。
4. 持久 `security.approval_allowlist`（支持 `*`、`name`、 `name:prefix`；line 264-277, 438-442）。自动放行。
5. `_write_stays_inside` —— 写路径在 workspace 根下、 `policy != "always"`、非敏感。自动放行 （line 444-446, 692-732）。
6. `_reports_without_changing` —— `command_is_known_safe` （只读动词如 `git log`）。自动放行 （line 448-450）。
7. `_on_request_allows_exec` —— `policy == "on-request"` + 非破坏性写 + 沙箱写使能 + `bash`/`run_compute`。自动放行 （line 452-454, 639-652）。
8. `_workspace_auto_exec` —— `workspace_auto=True` + 非 IM 渠道 + 沙箱可写 + `bash`/`run_compute`。自动放行 （line 456-458, 625-637）。
9. `_preauthorizer` —— 来自 schedule/granted tools。自动放行 （line 468-475）。
10. 没匹配：调 `approver`（UI 弹窗）。**无 approver = fail closed。** 返回 `_no_approver_reason` 带可执行建议 （"rerun from terminal" / "add to allowlist" / "set require_approval=false"）——不抛异常 （line 280-301, 477-480）。

弹窗 UI 在 `_with_choices`（`approval.py:584-615`）：默认 `Approve once`；加 `Approve '<argv-prefix>' for this session` （仅 bash，且 metadata 提议的 argv-prefix 过 `_supported_rule_prefix`）；对 task-bash 合规工具加 `Approve this turn's workspace`。永远有 `Deny`。

`pause_clocks()` 通过 `approval.py:49` 接进来，所以 approver 的人类等待不消耗 turn 墙钟。

### 4.5 Approval grant 形态

`cli/src/omni/core/approval_rules.py` 定义三种持久化形态：

- **`ExactApprovalGrant`**（line 108-120）—— 把命令**逐字**绑 到一个 `ApprovalContext`（cwd / workspace / channel / sandbox）。对 `bash` 比较的是完整 script，**包括**引号、 转义、`$`——所以 `/bin/sh -c "..."` 不会因 normalize 静悄悄扩展过短匹配（line 88-92）。
- **`SessionApprovalRule`**（line 138-193）—— 经过校验的 argv prefix。`_supported_rule_prefix`（line 318-399）只接受 review 过的族：`npm run <script>`、`uv run <...>`、 `pytest ...`（拒 `--basetemp`）、`ruff check/format`、 `cargo {bench/build/check/clippy/doc/fmt/metadata/run/test}`、 `git <safe verb>`（拒 `-c/--config-env/--exec-path/--git-dir/ --namespace/--work-tree`）、 `omni task {list/ls/show/status/watch}`。
- **`TaskBashApprovalGrant`**（line 402-431）—— 一 turn 的 workspace 信任。**显式不能**让写离开 workspace。**IM 渠道 （wechat/feishu/dingtalk）永不继承**（line 423-425）。

`SessionApprovalStore`（line 434-504）是 turn-scoped 内存 store；三组 grant 各一组；`prompt_lock: asyncio.Lock` 保证 同时只有一个 modal 对话（line 442, 486-510）。

### 4.6 沙箱

`cli/src/omni/skills_runtime/sandbox.py:1-415` 负责 OS 级 隔离：

- `_sandbox_works` 启动期跑四个后端探针 （`sandbox-exec -p "(version 1)(allow default)" /usr/bin/true` 等，line 47-61）；结果缓存。
- `detect_sandbox` 优先 Darwin `sandbox-exec`，然后 Linux `bwrap`，然后 Linux `firejail`（line 64-71）。
- `resolve_sandbox("auto")` → 自动检测；`off`/`none`/`` → 关； 显式 `sandbox-exec`/`bwrap`/`firejail` 不可用 → `SandboxUnavailableError`（fail-closed；line 74-89）。
- **回退**：auto 下找不到任何后端 → 空前缀（直跑），但 `_warn_unsandboxed_once`（line 255-277）发一次性 WARNING： "WITHOUT kernel confinement — only the coarse denylist applies"。

各后端的 profile：

- **Darwin seatbelt**（`_seatbelt_profile`，line 221-248）： `(allow default) (deny file-write*) (allow file-write* (subpath <each-root>...) (literal /dev/null /dev/stdout /dev/stderr))`。 每个写根被 `_seatbelt_write_atom`（line 200-218）包成 `(require-all (subpath <root>) (require-not (regex #"^<root>/<protected-name>(/.*)?$")))`，所以写 `<workspace>/.git/...` 也会被拒。
- **bwrap**（`_bwrap_prefix`，line 371-405）： `--ro-bind / /`、`--dev-bind /dev /dev`、`--proc /proc`， 每写根 `--bind root root`，scratch `--bind <persist_tmp> /tmp`。 `_safe_persist_tmp`（line 355-368）拒绝把 scratch 路径 解析进 `.omni`（"exec scratch opens Omni control state"）。
- **firejail**（line 320-330）：`--whitelist=<root>` 加对 metadata 路径 `--read-only=<denied>`。

`_metadata_deny_paths`（line 150-178）在用户源根下枚举 `.git / .omni / .agents / .codex` 全部进 deny 集合。 `PROTECTED_METADATA_NAMES`（line 125-138）是显式允许覆盖， 当用户真的 grant 了写 `.git`（罕见；主要是 worktree isolation 那例）。

`WRITE_PROTECTED_DIRS = VCS_PROTECTED_DIRS | STATE_PROTECTED_DIRS` （`sensitive_paths.py:63`）双重检查： `approval._write_stays_inside` 用 `is_write_protected_path` **且** fs tool handler 自己再过一遍。两个读，要求答案一致。

### 4.7 Container 与 worktree 隔离

`cli/src/omni/runtime/isolation.py`：

- `container` 要求 `docker_image`（line 35-47）。
- `worktree` 要求 `.git` 存在，在 `project_dir/worktrees/` 下创分支（line 60-92）。

被 `subagents`（维度 5）和需要 hermetic 执行的 `compute` skill 使用。

### 4.8 Node 渲染器 setup

`cli/src/omni/skills_runtime/runtime_setup.py:142-199` 只在 三个 owner-controlled 入口跑 `npm ci --omit=dev`：`omni init`、 `omni update`、`omni skills setup`。turn 里的 engine 只读 cache；包管理器永远不被 turn 调。开头注释明说："Task execution is deliberately side-effect free"。

### 4.9 权限层刻意不做的事

- **不做 capability token。** 没有"给模型一份 per-tool 的 capability token，host 再校验"这个机制。gate 是在 host 内 的同步检查。模型不能在事后重放这个检查。
- **不通过 system prompt 留后门。** System prompt 可以**描述** 工具；不能 grant 或 revoke 工具。Surface builder 在 prompt 渲染之后才跑。
- **不做"信任这个 session"的二元开关。** 批准是 grant 形态 （exact / argv prefix / task-bash），不是二元的。用户可以 grant `npm run test` 而不 grant `rm -rf`。

---

## 5. 子 agent 调度（routing / delegation）

### 5.1 没有固定的 "explore / worker / verifier" 三件套

`cli/src/omni/agent/subagents.py:60-74` 的 `SubagentSpec` 是一个 free-text `role` 字段。**Reviewer 不是 subagent 类** ——它是 `run_subagent` 里的 LLM-as-judge 循环 （`subagents.py:588-649`），由 `cfg.reviewer_enabled` 和 `reviewer_min_score=0.5` 控制。Domain packs （`cli/src/omni/data/domain_packs/*.toml`）列角色**建议** （"ml-method-reviewer"、"biomedical-evidence-reviewer"、 "evidence-auditor" 等），但这些都是模型写到 `role` 字段的 字符串，不是类型 tag。

这是对"三命名 subagent"模式的刻意偏离：harness 应当是 role-agnostic 的，由模型意图塑形。

### 5.2 两套 dispatch 表面

模型看到两组 delegation 工具，都在 `cli/src/omni/skills_runtime/builtin_tools/delegate.py`：

**Blocking batch**（默认）：`spawn_subagents` —— 收 `subtasks: [{goal, role, context, tools, model, compute_profile, isolation}]`，调 `run_subagents` （`delegate.py:181-217`）；并发上限 `cfg.concurrency` （`subagents.py:698-704`）。

**Async fire-and-collect**（`async_enabled=True`）： `spawn_subagent` / `wait_subagent` / `list_subagents` / `interrupt_subagent` / `message_subagent` / `followup_subagent` （`delegate.py:68-169, 255-340`）。

`SubagentControl.spawn`（`cli/src/omni/agent/subagent_control.py:79-97`） 用 `asyncio.create_task(self._run_one(live, seed))` 后台跑。 `wait` 等其中一个 live handle 发 `done_event`。`wait(None)` 等"任意"，但已收集过的 subagent 不再 signal。`interrupt` 调 `live.control.request_cancel()`。`message` 调 `live.control.push_steer(text)`。`followup` 给一个 finished 的建新 specialist，把旧 summary 当 context （`subagent_control.py:79-288`）。

Orchestrator 在 turn 起点 （`subagents.async_enabled=True`）建 `ctx.subagent_control = SubagentControl(ctx, cfg=self.settings.subagents, depth=0)` （`orchestrator.py:1338-1341`），turn 末 `await ctx.subagent_control.aclose(grace_s=2.0)` （`orchestrator.py:1474-1476`）。

### 5.3 没有"注册 subagent" 的 API

没有用户面的"注册 subagent" API。定制只能两种方式：

- **`spawn_subagents` 调用时**直接传 `tools=[...]` allowlist。 默认是 `builtin + research`，剥掉 `_MUTATION_TOOLS = {write_file, edit_file, apply_patch, bash, run_compute}`（`subagents.py:56, 215-223`）。Spec 里可以 `+` 回 mutating 工具。
- **Domain pack `[[specialists]]` 块**：定义 `role + description + tools` 元数据；在 registry 里仍按普通 skill 处理，不是单独的 subagent registry。

`SkillRegistry.register(entry: SkillEntry)` （`cli/src/omni/skills_runtime/registry.py:274-395`）是唯一 入口。系统 skill 单独索引 （`_index_system_skills`，line 362-376）但还是 skill。

`container` 隔离锁住 surface 为 `builtin + research + run_compute`（`subagents.py:217-218`）：一个 `worktree` 隔离的 spec 不会意外跑进 host Python engine。

### 5.4 上下文传递与隔离

`_child_context`（`subagents.py:124-142`）构建每个 child 的 context：

- `task_id = f"{ctx.task_id}::sub-{uuid4().hex[:8]}"`—— 每个 event 和 artifact 都能归属。
- `subagent_control=None` —— subagent 不能开自己的 `SubagentControl`（depth 限制；见 §5.5）。
- `subagent_depth = depth + 1` —— 驱动 `build_delegation_tools` 的 depth gate。
- `file_uris` 拷一份（inbox 引用），但 *transcript* **不** 继承。

System prompt 是 `_specialist_system` （`subagents.py:226-233`）：

```
You are the coordinator's {role} subagent. Complete only the assigned
subtask and return a self-contained final answer. The coordinator
sees only the final answer, so it must be complete, directly usable,
and evidence-grounded.
```

"协调器只看 final answer" 由 `_summary_of` 截到 6 000 字符强制 （`subagents.py:251-255`）。成本按 child task 记， `component="subagent.initial" / "subagent.revision.{n}" / "reviewer.subagent.{n}"`（`subagents.py:381-431`）。Research ledger 通过 `merge_research_ledger(parent_id, child_row)` 合回父（`subagents.py:666`）。

默认 tool surface 是 `_specialist_tools` （`subagents.py:145-223`）：`builtin + research + PYTHON_ENGINE / CLI_EXEC skill`（带 `provider_authority` 校验，line 188-200）， 去掉 mutating 工具。Spec 给的 `tools` 列表被当作 allowlist。

### 5.5 嵌套、cancel、timeout

**嵌套。** `build_delegation_tools` （`cli/src/omni/skills_runtime/builtin_tools/delegate.py:177-179`） 在 `depth >= cfg.max_depth` 时不返回 `spawn_subagents`。 默认 `max_depth=2`（settings:287）意思是"协调器 → specialist 一层深"。`SubagentControl._depth` 透传给 child （`subagent_control.py:69-97`）。

**Cancel** 五条路径：

1. **用户**：`ExecutionControl.request_cancel`（REPL / TUI / IM interrupt）。
2. **Turn 末**：`SubagentControl.aclose(grace_s=2.0)` （`subagent_control.py:291-307`）：等 `grace_s`；然后 `control.request_cancel()`；再等 1s；最后 `task.cancel()`。
3. **单 subagent**：`SubagentControl.interrupt(nickname)` 调 `live.control.request_cancel()`（`subagent_control.py:235-242`）。
4. **循环内部**：`run_subagent._run_once` 透传 `execution_control`，所以子循环能看到 cancel。
5. **硬 cancel**：`asyncio.CancelledError` 在 `subagents.py:566-573` 抓住；child task 标 "cancelled" 并 re-raise。

**Timeout。** `wait_subagent` 默认 `cfg.wait_default_s=30.0`， 被 `cfg.max_seconds=90.0` 截顶（"never wait longer than a specialist could possibly run"， `subagent_control.py:183-187`）。Specialist 自己的 `ReActLoopAgent` 构造时用 `max_seconds=cfg.max_seconds, max_iterations=cfg.max_iterations, max_tool_calls=cfg.max_tool_calls, stall_timeout_s=...` （`subagents.py:532-552`）。工具预算由 `subagent_tool_budget = ToolExecutionBudget(cfg.max_tool_calls)` 跨循环共享（`subagents.py:532-538, 605-642`）。 `usage_budget_exhausted(result)` 跳出 revise 循环 （line 592, 631）。

**资源锁。** Specialists 继承父的 `resource_locks` （`tool_gateway.py:101` 读 `getattr(ctx, "resource_locks", None)`）；`ToolResourceLockPool` （`cli/src/omni/runtime/execution_policy.py:48-154`）按 key （`fs:<path>` / `exec:<scope>` / `store:<scope>`）排序加锁 避免死锁；mutating fs 操作走 `fcntl` / `msvcrt` 文件锁 （line 90-201）。

### 5.6 Reviewer gate

Specialist 循环跑完后，如果 `cfg.reviewer_enabled and status in (ok, partial)`，调 `review_output(llm, goal, output)`。低于 `reviewer_min_score=0.5`，gate 追加 `[Review feedback] {notes}` 作为 user_msg 再跑一轮；最多 `reviewer_max_revises=1`（`subagents.py:588-647`）。 Reviewer 是一个 LLM 调用，不是独立 agent——一次额外的循环 质量检查，成本大约等于多一轮。

### 5.7 子 agent 调度刻意不做的事

- **不做嵌套 subagent 图。** `max_depth=2` 是上限。Harness 把它当够用：父是协调器，子是 specialist，就这样。
- **不做跨 specialist 的共享可变状态。** 每个 subagent 有 自己的 `task_id`、自己的 resource lock pool 引用、自己的 预算。唯一共享资源是数据库和父 task 的 event 流。
- **不做模型控制的 subagent 生命周期。** Subagent 只能被 `spawn_subagents` / `spawn_subagent`（模型）启动、 `interrupt_subagent`（模型）打断、或 `aclose`（host）收割。 父不能深入 child 的循环。

---

## 6. 会话与状态（session / state）

### 6.1 生命周期与 turn 边界

一个 turn 的入口是 `orchestrator.run_turn` （`cli/src/omni/agent/orchestrator.py:978`）。整个 turn 跑在 `async with persist_scope(self.db)` 里，所以取消在 DB 层是 原子的——见 `cli/src/omni/runtime/cancel_persist.py:107-117` 的 persist scope。簿记是"turn 写入一起 commit 或一起回滚"。

`SessionLifecycleService`（`cli/src/omni/agent/session_lifecycle.py:181-386`） 守护 session 级操作：

- `delete_many(session_ids)` 在一个事务里跑 `_delete_workspace` （line 239-378）。先 `_begin_task_delete_snapshot` 占写 （line 248-254）；查 active 子节点、`_task_descendant_closure` 找跨 session 后代（line 280-291）；列 blockers （schedules、open action_checkpoints、active compute_jobs、 pending schedule proposals；line 102-178）；没 blocker 就 `stage_task_deletion(force=True)` + flush + 删 `ConversationMessageORM` / `SessionFocusORM` / `SessionORM`， 最后 commit（line 366-378）。
- 失败模式：`code ∈ {concurrent_write, ambiguous, not_found, conflict, busy}`（line 258-264, 293-302, 330-335, 348-352）。
- 机器级 `control_db` 也 init，跨 workspace 的 schedule proposals 拿到写预约（line 210-222）。

### 6.2 Conversation store

`cli/src/omni/agent/conversation_store.py:76-449`：

- `ensure_session(channel, external_key, reuse_latest)`： IM 同 key 复用；CLI 默认新开（line 87-110）。
- `history(session_id, limit=12)` 给 ReAct prompt； `extraction_history(limit=40)` 给 memory / compaction。
- `touch_session` / `set_session_title` / `delete_session` / `get_session` / `resolve_session` 是标准五件。
- `principal_of(channel, external_key)` 返回 `"<channel>:<external_key>"` 或 `"local"`；按 channel 缓存 （line 112-143）。

Schema（`cli/src/omni/storage/models.py:63-95`）：

- `SessionORM`：`id, project, channel, external_key, title, status, forked_from, created_at, updated_at`（`forked_from` 支持 session fork）。
- `ConversationMessageORM`：`session_id, role, content, content_type, name, tool_call_id, metadata, created_at`。

### 6.3 Task controller

`TaskController`（`cli/src/omni/agent/task_controller.py:17-199`）：

- `create_turn_task`：建 task + 触发 `on_task_ack` （line 23-48）。
- `finish_turn`：append `assistant.message` 事件；如果 children 已提交，`refresh_from_executions` 等 child terminal 再 settle。`terminal_status` 推导：
  - `kind == "error"` → `failed`
  - `kind == "needs_input"` → `needs_input`
  - 其它 `succeeded`，有 `turn_degradation_warnings` 时降级 为 `degraded`。
- `apply_settlement` 把 orchestrator 自报 status 与 durable record 对齐（line 159-199）。注释里点原则："settle from the turn's own end once children are terminal"。

### 6.4 持久化

`Database`（`cli/src/omni/storage/db.py:70-200`）是单个 SQLAlchemy async engine 跑 `sqlite+aiosqlite`。Pragmas： `synchronous=NORMAL, busy_timeout=1000ms, foreign_keys=ON` （line 91-100）。WAL 模式（docstring line 4-6）让 daemon 和 短命令并发读。`ApplicationId=0x4F4D4E33 ("OMN3")` 标当前 store shape（line 53）；`user_version=1` 保留（line 52）。

ORM 表（`cli/src/omni/storage/models.py`，27 张表）：

- 会话 / 任务：`SessionORM, ConversationMessageORM, TaskORM, WorkflowRunORM, WorkflowStepORM, SubtaskORM, WorkflowCheckpointORM, TaskEventORM, TaskControlORM`
- 投递：`OutboundDeliveryORM, ComputeJobORM, SessionFocusORM`
- 记忆：`MemoryEntryORM, MemoryEdgeORM`
- 产物 / 研究：`ArtifactORM, SourceORM, ChunkORM, CitationORM, HypothesisORM, ClaimORM, EvidenceORM, RunORM`
- 调度：`ScheduleORM, ScheduleActionProposalORM, ActionCheckpointORM`
- 索引：`TaskIndexORM`

`TaskORM`（`models.py:105-184`）的 lineage 让 retry 安全：

- `parent_task_id`（FK 自指，`ondelete="CASCADE"`）
- `origin_workflow_run_id / step_id`
- `schedule_id`
- `retry_of_task_id / root_task_id / attempt`（retry lineage）
- `input_snapshot_json`（不可变 turn input）
- `approved_tools: list` —— per-task grant，materialize 时 设置（例：scheduled run 在 `scheduler.py:493-498` 调 `recorder.grant_tools(task.id, grants, ...)`）
- `current_authority_fingerprint` + `approval_authority_fingerprint` —— plan + catalog + contract
  + pre-grant 的内容 hash（`models.py:167-172`）

### 6.5 Cancel-safe 写入

`cli/src/omni/runtime/cancel_persist.py` 解 Py3.11+ 的一个 怪事：`Task.cancel()` 能递归取消子 waiter，所以父 cancel 会杀掉本来要写 `react.finished` 的 writer。答案是 `run_uncancelled(work)` —— 在 sibling task 跑 work 并 wait（line 151-180+）；`pause_cancellation` / `resume_cancellation(held)` 让 approval-gate 等待不被当 cancel（line 120-148）；`persist_lock` 在每 loop 每 DB 可重入（两个 workspace 不互锁；line 26-96）。

### 6.6 Scheduler 与 cron

`cli/src/omni/runtime/scheduler.py:187-591` 是手写的 5-field cron 解析（`parse_cron` / `cron_matches` / `next_cron_fire`），全套 cron 特性：`*`、`*/n`、`a-b`、 `a-b/n`、`a,b`；DST 与 day-of-month / day-of-week OR 规则 按标准 cron（line 95-186）。

`Scheduler.run_due(now, limit)`（line 375-462）：

- 一个事务：`SELECT enabled AND next_due_at <= now`。
- 打 `_Fire` 占写；推进 `next_due_at = _compute_next(...)`；bump `run_count`；禁 `once`。
- **SQLite 串行化写**保证两个 ticker 不会双发。

`_materialise_owning_task`（line 464-500）每个 fire 建一个 `TaskORM`，`schedule_id` 指回。`grant_tools` 注入 `approved_tools`；preauthorizer 把它传给 `ApprovalGate`， OS 沙箱 + 写根还继续收口。

触发类型：`interval` / `cron` / `once`（`ScheduleORM.kind`， `models.py:744`）。长 outage 期间漏发的 fire **不补**—— outage 不会演变成 stampede（line 386）。

`cli/src/omni/scheduling/service.py:73-552` 是 schedule 创建面：

- 本地 CLI 直接创建（`omni schedule add` 不弹窗）。
- IM 渠道创建需要本地批准：写一条 `ScheduleActionProposalORM` （`payload_digest = sha256(payload)`、`idempotency_key` 拦重发）；`omni schedule approve <id>` 用**存储的** payload 重跑（从不从 prose 重新派生； `models.py:766-820`）。
- 24h `_PROPOSAL_TTL` 让旧 proposal 过期；near-term `_NEAR_TERM_APPROVAL`（15 min）自动拒批时间窗口已经过去 了大半的 proposal（`service.py:53-62`）。

`cli/src/omni/scheduling/temporal.py` 解析 8 段中英时间 表达（`resolve_temporal`）：`resolve_cron` / `resolve_interval` / `resolve_once` / `resolve_zone` 把 "明天 6 点"、"every 2h"、"cron 0 18 * * *" 翻成 trigger 参数。 `ZoneInfo` aware-DST。

### 6.7 Daemon 与 web

- `omni serve` 用 `os.replace`（原子）写 `serve.pid`，带 startup metadata（version、argv、channels、workers）， 让 `omni update` 能用同样参数重启 （`cli/src/omni/runtime/daemon.py:30-67`）。30s `HEARTBEAT_STALE_SECONDS` 是 liveness 窗口（line 27）。
- `omni web` 写 `web.pid` （`cli/src/omni/runtime/web_service.py:23-82`）；无 heartbeat 窗口（"a healthy UI older than 30s is still live"）；`pid_alive` 死了就自动清 pidfile （line 86-100）。`STOP_GRACE_S=3.0 / STOP_KILL_S=2.0` 是 两段停机。

### 6.8 Update 收敛

`cli/src/omni/runtime/update_state.py`：

- `InstallationFingerprint`（line 38-54）hash 掉 sanitized PEP 610 `direct_url.json`（去凭证）。
- `STATE_FILE=update-state.json` schema v2；venv-out `omni update` 用它判断"包换了，home 要不要重收敛"。

### 6.9 Resume 与 cancel

**Resume**：

- 同 session 新 turn → `ConversationStore.ensure_session(channel, external_key)` 按 `external_key` 复用。
- `TaskController.create_turn_task` 透传 `file_uris`，让 `task retry` 能复现输入（line 35-41）。
- `TaskORM.input_snapshot_json` 不可变；retry lineage 在 `retry_of_task_id / root_task_id / attempt` （`models.py:130-137`）。
- `WorkflowStepORM.step_key` 跨 retry 稳定；`execution_ids` 是各 attempt 的 subtask id 列表 （`models.py:228-261`）。
- `recent_activity_digest` （`cli/src/omni/agent/recent_activity.py:43-100`）给 planner 渲染最近 6 个 principal 范围的 task——"重新生成 上次那张图"不用再问一遍。

**Cancel**：

- `TaskController.finish_turn` 检测 `terminal_status ∈ {cancelled, interrupted}` → 先 `settle_open_children_for_cancel(task_id)` 关 children， 再 `ensure_event` 强落 `react.finished`（Windows busy 也 能保证 event 进去；`task_controller.py:91-107`）。
- 顶层 `ExecutionControl.request_cancel` 由 orchestrator 持； 每个 subagent 在 `_child_context` 里 `subagent_control=None` 但有真 `execution_control` （`subagent_control.py:84-92`）。
- `cancel_persist.pause_cancellation` / `ignore_cancellation` 让 `react.finished` 的写能跑完 （见 §6.5）。
- `SubagentControl.aclose(grace_s=2.0)` 是 turn 末的收割 （见 §5.5）。
- IM 渠道的 child 永远不让 task-bash grant 继承 （`TaskBashApprovalGrant.matches`，`approval_rules.py:423`）。

### 6.10 会话层刻意不做的事

- **不做跨设备同步。** Harness 是 local-first。AGENTS.md 显式说：SQLite + filesystem only；没有 MySQL、Redis、 MinIO、Chroma。Control store 是 home-level （`control.sqlite3`），但仍是单机本地。
- **没有"session 升级为 long-running task"的边界。** Task 和 session 是分开的概念。一个长期工作的单元是一个 `Task`（`kind ∈ {turn, subagent, maintenance, escalated}`）+ 一个 `Schedule`（如果需要 cron）； session 是聊天线，不是 job。
- **不做 per-session 工具策略。** 策略是 per-task， materialize 时记入 `TaskORM.approved_tools`。一个 session 想 grant 工具，靠"建一个有 grant 的 task"——不是 "模型在中途开口要"。

---

## 7. 可观测（observability）

### 7.1 Trace 与步骤记录

可观测的核心是 `task_events`，一张 append-only 表 （`cli/src/omni/storage/models.py:343-381`）；每行有 `seq`、`event_type`、`status`、`lifecycle_status`、 `result_success`、`name`、`tool_name`、`skill_name`、 `input_json`、`output_json`、`error`、`summary`、 `duration_ms`。事件名遵循 `<component>.<verb>` 约定。 Hook 成功 / 失败本身也是事件 （`cli/src/omni/runtime/hooks.py:217-227`）：

```python
await self._tasks.append_event(
    task_id,
    event_type="hook.done" if status == "succeeded" else "hook.failed",
    status=status, name=event,
    input_json={"event": event, "command": _command_label(command)},
    output_json=output, error=error, duration_ms=duration_ms,
    summary=f"hook {event} {status}",
)
```

`TaskRecorder.append_event` 包了 `retry_while_busy`，所以 SQLite 的"database is locked"是瞬时的。

一张独立的 `control.sqlite3`（机器全局）装 `TaskIndex`—— 跨 workspace 的 task metadata——`omni task --all` 不必扫 每个 workspace。`TaskRecorder` dual-write （`cli/src/omni/runtime/task_index.py:144-168`）。

### 7.2 Token 与 cost 跟踪

`cli/src/omni/agent/cost.py:25-50` 是一张 USD-per-1M 价目 （`gpt-4o / deepseek-v4 / claude-sonnet-4` 等）。`rate_for` 按带日期别名的最长子串匹配。`estimate_cost` 优先读 provider 的 `usage` 块；否则按 `len(text) // 4` 估算 （`cost.py:104-133`）。最终事件是 `cost.usage`：

```python
await tasks.append_event(
    task_id,
    event_type="cost.usage",
    status="succeeded",
    name=component,
    output_json={**estimate.to_dict(), "currency": currency, "component": component},
    summary=f"{component}: cost ~{estimate.cost_usd:.4f} {currency} · tokens {estimate.total_tokens}",
)
```

`record_cost_event` 吞掉异常（`cost.py:172-173`），保证 计量失败不阻塞 turn。`react_usage_limits` 把 `settings.cost` 翻成 ReAct 的 `max_total_tokens / max_cost_usd / warn_*`；`usage_budget_exhausted` 看 `result.terminated_reason` 或 `usage_budget.enforced` （`cost.py:303-342`）。`task show` 端调 `summarize_cost_events` 把已加载事件汇总，不再多查 DB （`cost.py:228-271`）。

### 7.3 日志

`cli/src/omni/runtime/logging_config.py:1-6` 一开篇就是 硬规则：

> Never call `basicConfig`, never replace caller handlers.

`OmniLogFormatter`（line 173-194）把记录格式化成单行 JSON-ish：`ts level component=<c> logger=<n> pid=<p> event=<e> message=<json>`。Token redact 跑 `redact_secrets` + `_UNSAFE_TOKEN_RE`。

`_PrivateRotatingFileHandler` （`logging_config.py:197-207, 40-44`）是文件 sink： 10 MB × 10 文件，POSIX 立即 `os.fchmod(0o600)`。 `ProcessLogging`（`logging_config.py:284-371`）是 enter/exit 的可逆 attach：进入记 logger 旧 level 并调低 `httpx / httpcore / uvicorn.access`；退出恢复。它只卸 自己创建的 handler——caller handler 保留。 `UvicornShutdownFilter`（`logging_config.py:100-122`） 把 ASGI graceful-shutdown 时的 `CancelledError` 噪声从 stderr 抹掉，但文件日志仍保留真相。

### 7.4 调试入口

- **CLI**：`omni status` / `omni task show` / `omni task list --all`。
- **TUI footer**：`turn_outcome.classify_turn_outcome` + `header_state`（`cli/src/omni/runtime/turn_outcome.py:80-87`）， `exec_exit_code` 决定进程 exit code。
- **跨 workspace**：`cli/src/omni/runtime/aggregate.py` 的 `_indexed_task_page` 读 `control.sqlite3` 的 `TaskIndex`；空时跑 `reconcile_index`。 `list_schedules_all_workspaces` 迭代 `iter_catalog_workspaces`（line 166-205）。
- **最近活动**：`cli/src/omni/agent/recent_activity.py:43-99` 把过去 60 条 terminal task 渲染成 `Recent activity` 块 喂给 planner，让 follow-up turn 不再问"上次那张图"。
- **Lab notebook**：`<workspace>/NOTEBOOK.md` （`memory/notebook.py:11-26`）是 git 友好的人类可读 trace， 与 DB 的 `task_events` 并行。

### 7.5 可观测刻意不做的事

- **不做 Prometheus / OpenTelemetry exporter。** 不接 metrics scrape，不接 distributed-trace 协议。DB 和 log 文件就是接口。外部 dashboard（如果有）读同一个 SQLite 或 JSONL。
- **不做 web dashboard。** Web SPA 是 workspace 视图，不是 metrics 视图。它展示 task 和 artifact，不展示图表。
- **不做自动告警。** 长 stall 表现为 task 上的 `terminated_reason="stalled"`；不会主动通知谁。

---

## 8. Hook 与拦截点

### 8.1 LLM event hook

`cli/src/omni/core/llm/client.py:36-46` 用 `ContextVar` 做 单 slot 注入，让同一进程服务两个对话时不串：

```python
_llm_event_hook: ContextVar[...] = ContextVar("omni_llm_event_hook", default=None)

def bind_llm_event_hook(hook): return _llm_event_hook.set(hook)
def reset_llm_event_hook(token): _llm_event_hook.reset(token)
```

`emit_llm_notice` 承载 retry 通知（`Reconnecting n/5`）； `_emit_delta` 是 token 流 sink（同步/异步都行）。循环在 迭代开始 `bind_llm_event_hook(on_tool_event)` （`react_agent.py:700`），迭代结束 `reset_llm_event_hook` ——同一个 callback 既收 token delta 也收 notice。

### 8.2 工具前后插桩

`invoke_tool_with_hooks`（`cli/src/omni/runtime/hooks.py:367-509`） 是所有路径必经的拦截器：

```python
if hooks is not None:
    decision = await hooks.emit("pre_tool", task_id=task_id,
        payload={"tool_name": tool_name, "arguments": ..., "family": family},
        deny_capable=True)
    if not decision.allowed:
        raise HookDeniedError(decision.reason)
...
try: ... except Exception as exc:
    if hooks is not None:
        await hooks.emit("post_tool", task_id=task_id,
            payload={..., "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    raise
```

`pre_tool` 之后插一个 `ExecutionPolicyFrame` （`hooks.py:417-442`）—— 哈希（`sha256` of canonical args） 决定"哪一次具体调用"被允许；同进程内拷到子任务的 `ContextVar`，子任务通过 `asyncio.current_task() is frame.owner_task` 拒绝跨任务 复用授权——"task A 的 grant、task B 来执行"的攻击被拒。

### 8.3 自定义 hook 注册

`settings.hooks` 是一个配置对象，命令按事件名分桶，`*` 通配 所有事件：

```python
commands = [
    *self._cfg.commands.get("*", []),
    *self._cfg.commands.get(event, []),
]
```

命令运行器（`hooks.py:240-277`）严格：

- 只 `asyncio.create_subprocess_exec`——不调 shell。
- `stdin` 是 redacted JSON envelope。
- `stdout` 必须是单个 JSON object。
- `max_output_bytes` 默认 1024。
- `timeout_s` 严格守护。
- payload 走 `_redact`——`api_key / secret / password / token / credential / authorization` 全部变 `[REDACTED]` （`hooks.py:280-289`）。
- `action: "deny"` 才是真 deny；`failure_policy == "fail"` 也视作 deny（`hooks.py:236-237`）。
- **优先级**：通配命令先跑，专用事件后跑；第一个 deny 短路。

### 8.4 失败隔离

- Hook 自身异常被 `except Exception` 抓住，记 warning， 主循环继续（`hooks.py:208-213`）。
- Subprocess 跑在独立进程组 （`processes.process_group_options` + `stop_process_tree`，`cli/src/omni/runtime/processes.py:13-63`）， 取消 hook 会沿组 ID 杀掉整棵树。
- `is_cancelled` 重抛 `asyncio.CancelledError`，不吞。

### 8.5 其它拦截器

- **`defend_observation`**（`cli/src/omni/core/injection.py:46-79`） 在 observation 进 transcript 前跑。`mode="flag"` 加 banner； `mode="strip"` 把 11 类操纵话术替换成 `[suspected injected instruction neutralized]`。这是 sensitive-paths 检查后的第二道防线；第一道是"模型 写不进 .env"，第二道是"模型不能通过改写 prompt 的话术 骗自己写 .env"。
- **`execution_ownership`**（`cli/src/omni/runtime/execution_ownership.py`） 给每个 `SubtaskORM` 钉一个 PID。`pid_alive` 是 POSIX 上 `os.kill(pid, 0)`，Windows 上 `OpenProcess` + `GetExitCodeProcess`。进程死了它的 subtask 才能 settle ——活进程永远不被偷。
- **`ExecutionControl`**（`cli/src/omni/core/execution_control.py:43-201`） 拆 `cancel_requested`（process-local）和 `durable_cancel` （DB 背书）。`request_cancel` 不写控制行；`poll` 只在 看到 cancel 行才 `durable_cancel=True`。这让 `omni update` 重启**不**取消一个长跑 turn。
- **MCP client**（`cli/src/omni/compat/mcp_client.py:23-56`）： session-per-call（stdio 或 SSE）；server 不可达就 `logger.warning` 跳过——一个坏 MCP 不能黑掉整工具面。

### 8.6 Hook 层刻意不做的事

- **不做"model 后、tool 前"的任意代码 hook。** 唯一拦截 点是运行时定义的那些。想加更多，owner 可在 `settings.hooks` 配自己的命令列表；不能在进程里 patch 循环。
- **不做远程加载的 hook 代码。** Hook 是命令名 + argv； harness spawn 它。没有进程内 plugin loader。

---

## 9. 错误恢复与重试

### 9.1 错误分类

`cli/src/omni/core/tool_errors.py:13-25` 定义 5 个 host-owned 错误类。**在 wire 上稳定**——模型策略代码可以 直接 branch：

```python
INVALID_ARGS         # unknown_tool / tool_arguments_invalid / tool_contract_violation / tool_policy_rejected
RETRYABLE_IO         # tool_circuit_open / tool_timeout / 429 / 503
SKILL_FAILED_PARTIAL # failed but artifacts survived
UNPAYABLE            # unpayable / vlm_unavailable / node_unavailable / pptx_unavailable
FATAL_TURN           # sandbox_escape / storage_corrupt
```

`classify_tool_error`（`tool_errors.py:86-127`）先看 `result.error_class`，再看 status / error_code，最后回退 到对结果文本的子串匹配。`short_skill_observation` （`tool_errors.py:130-168`）附 `_remediation_hint`，让 下次尝试不再是"再猜"——VLM 503 拿"retry"提示，不是 "VLM 未配置"（`tool_errors.py:312-335`）。

LLM 侧独立分类 （`cli/src/omni/core/llm/errors.py:69-139`）：

- `400 + tool_call` → `transcript_invalid`（不重试；同 transcript 同错误）。
- `429` / `5xx` → retryable，fallback allowed。
- `401` / `403` → `authentication`。
- `output_cap_truncated` 单独一支 （`OUTPUT_CAP_TRUNCATED_REASON`）；是 provider 截断信号， 不是失败。

### 9.2 重试策略

- **LLM 客户端**：`RetryPolicy(max_retries=2, base_delay=0.5, max_delay=8.0, jitter=0.1)`（`client.py:99-155`）；优先 `Retry-After` header（`client.py:117-144`）；否则指数 退避 + 对称 jitter。
- **ReAct 工具调用**：`_TOOL_RETRY_MAX = 1`，仅在 `replay_safe` 工具上（`react_agent.py:67-68, 1900-1901`）； 退避 `_TOOL_RETRY_BASE_DELAY * (2 ** attempt)` （`react_agent.py:1936-1938`）。
- **后台 skill**： `cli/src/omni/runtime/subtask_retry.py:27-40` 的 `_TRANSIENT_SIGNS` 匹配 `timeout/429/503/connection reset/...`；在 `recovery_policy="auto_retry_transient"`, `is_transient_error` 触发；`record_auto_retry` 把 `original_error + recovery_attempt` 写 `SubtaskORM`，发 `subtask.retry`，按 `settings.tasks.retry_backoff_s * attempt` 退避（`subtask_retry.py:51-104`）。
- **Workflow envelope timeout**： `cli/src/omni/runtime/skill_timeout.py:23-36` 的 `skill_exception_status`：workflow_envelope 总是 `failed`；单 skill budget/stall 仅在有 durable output 时为 `degraded`，否则 `failed` 以让 SINGLE_SKILL 还能 fallthrough。

### 9.3 熔断器

`cli/src/omni/core/react_agent.py:69-76, 135-148, 1837-1894, 1990-1995` 跑两套独立计数器：

```python
_CIRCUIT_BREAKER_MAX = 5
_MAX_UNEXECUTED_CALL_STREAK = 5
```

- `self._circuit[_circuit_key(tc)]` —— **被执行过**的失败。 给定 `(tool, args)` 对超 5 短路为 `tool_circuit_open`。 Meta-tool key 走 `_META_TOOL_SUBJECT_ARGS`，所以一个 坏 `run_skill` 不会毒化其他 skill。
- `self._unexecuted_calls[code]` —— **未执行**的拒绝 （`unknown_tool`、`tool_arguments_invalid`、 `tool_arguments_truncated`、`tool_contract_violation`、 `tool_policy_rejected`、`tool_approval_required`）。超 5 循环以 `no_progress` 终止。

成功调用同时清两套：`self._circuit.pop(circuit_key, None)`
+ `self._unexecuted_calls.clear()`
（`react_agent.py:1981-1984`）。

### 9.4 升级路径

- **`escalate_run` 工具**（`react_agent.py:65, 304-320`）： 注册为 meta-tool；只有 `allow_escalation=True` 时注入。 模型调用 → `agent.turn_escalate.maybe_escalate_run` （`turn_escalate.py:12-61`）。
- **`maybe_escalate_run`** 建 `kind="escalated"` child task， 通过 `tasks.inherit_research_ledger` 继承父 ROM， `asyncio.create_task` 后台跑 `handle_turn`。拒递归 （`depth ≥ 2` 或父已 escalated）。
- **Plan ladder**（`cli/src/omni/agent/plan_recovery.py:67-118`）： 五级。Safety → 硬停。Reference-marker `needs_input` → ReAct 回退查"哪个引用物"。单字段缺 → `needs_input` 问。 其它 → `build_react_recovery_plan`（rung 4，把 findings 喂给 capable assistant，仍 tool-policy bound）。
- **Fallthrough**（`cli/src/omni/agent/plan_fallthrough.py:30-42`）： 单 skill 失败后，`policy_after_failed_route` 把 `allowed_tools / max_tool_calls / max_iterations` 清回 `None`（deny list 保留）； `history_with_failed_attempt` 把失败注入 history 防止 模型原地重试。

### 9.5 Resume 与 checkpoint

- **Durable checkpoint**： `cli/src/omni/runtime/schedule_checkpoint_resume.py:107-200` 的 `resolve_schedule_checkpoint` 做 CAS、 `required_decider` 校验、`pick:/repair_next_day:` 前缀 解析、`run_now/cancel/other_time` 关键字路由。
- **Recovery coordinator**： `cli/src/omni/runtime/task_recovery.py:160+` 的 `TaskRecoveryCoordinator` 区分三种语义：
  - `retry`：不可变 `input_snapshot` 下的新 attempt。
  - `resume`：复用 ROM 重开 ReAct，或接续 workflow checkpoint。
  - `requeue`：把 standalone skill execution 重新入队。
- **Lost-executor reconcile**： `cli/src/omni/runtime/execution_ownership.py:49-173` 的 `execution_owner_lost` 检查 `pid_alive + stale_after_s`。 `reconcile_lost_executors` 按状态分：pending cancel → `cancelled`；其它 `interrupted`；workflow-bound 可 requeue 走 CAS `_cas_requeue_workflow`；否则 `_cas_finish_standalone` 用 `owner_pid` 做乐观锁 （`execution_ownership.py:204-260`）。
- **Cancel-persist**：见 §6.5。
- **Stream-stall watchdog**： `cli/src/omni/core/llm/idle.py:30-123` 的 `IdleWatchdog` 记最后活动时间；`await_with_idle` 同时看墙钟和静默； `provider_http_timeout` 把 httpx `read` 拆到 idle window， 让长流不被短超时一刀切。`StreamIdleTimeout` 被 `RetryingLLMClient` 接住重连。
- **Periodic housekeeping**： `cli/src/omni/runtime/housekeeping.py:30-57` 的 `run_housekeeping` 先 reconcile lost owner，再按 `tasks.retention_days` 清 `failed/cancelled/interrupted` 任务 （`succeeded/degraded` 是 provenance，保留）；artifacts 文件永远不动。
- **Wrap-up terminate**：见 §1.2 —— 每个 spend 原因触发 一次 `tool_choice="none"` 合成调用，前面 `microcompact_tool_results(keep_last=2, max_chars=400)`， 这样合成能读 transcript。

### 9.6 错误恢复刻意不做的事

- **不做跨 turn 边界的自动 replanning。** Turn 结束，下个 turn 全新开始。跨 turn 恢复走 durable record（workflow checkpoints、recovery plans）和模型自己，不是循环。
- **顶层不做异常 squash。** 每个异常都 typed + classified； 唯一"吞掉"是失败模式已知且定义明确的地方（cost event 写、hook 执行）。

---

## 10. 跨维度观察

### 10.1 这个 harness 做了、其他 harness 经常不做的事

- **Reach 和 exposure 是两轴。** 大多数 harness 把"模型 能调这个"和"模型知道这个存在"混在一起。OmniScientist 拆开它们，让 token 优化不会破坏 reach （`react_agent.py:209-225`）。
- **Host-owned 封签结果的 admit 控制。** `HostToolRejection(_host_seal)` 模式 （`tool_result.py:144-168`）形同密码学签名：host 是唯一 能 mint"这个工具调用成功"verdict 的实体，封签是单向的 ——工具无法伪造成功。
- **Sandbox fail-closed。** 显式要 `sandbox-exec` / `bwrap` / `firejail` 但拿不到就抛 `SandboxUnavailableError`。Fallback 是"无 sandbox + WARNING"——不是"假装我们有 sandbox"（见 §4.6）。
- **Cancel 是 durable 的。** Process-local cancel 与 DB-backed cancel 是两件事；harness 能重启不丢 cancel， 重启不会意外取消长跑 turn （`execution_control.py:43-201`）。
- **Approvals 是 grant 形态的，不是二元的。** Exact / argv prefix / task-bash——三种形态让用户能说"是 `npm run test`"而不说"是 `rm -rf`" （`approval_rules.py:108-431`）。
- **五层 memory 显式排除 M1 跨会话。** 长跑 agent harness 最大的 bug 是"模型把对话里随口说的话 recall 成 durable 知识"。M1 的排除就是修复 （`service.py:149-154`）。
- **Sandbox profile 默认 deny VCS，且不可被放松。** 即便 配了 `output_roots`，`.git` 也被拒 （`sensitive_paths.py:84-92`）。注释就是设计规则： "no output_roots can relax VCS"。

### 10.2 这个 harness 不做的事（以及为什么可能是刻意的）

- **不做跨设备同步。** 产品是 local-first，AGENTS.md 明说。 跨设备同步就意味着 server，违反 local-first 前提。
- **不做 metrics 的 web dashboard。** Web UI 是 workspace 视图，不是图表视图。Operator 读 SQLite 或 JSONL。
- **不做 Prometheus / OpenTelemetry。** 同样原因。想要 metrics，tail log 或查 DB。
- **不做"注册 subagent" API。** 设计把 subagent 当 ephemeral、model-spawned。Specialization 走 `role` 字符
  + `tools` allowlist，不走类型 tag。Depth gate 因此简单。
- **不做细粒度逐轮"你卡住了吗"反思。** Harness 用一个 `no_progress_threshold` 和 prompt 层的 hunt-consume steer；刻意不加昂贵的 meta-prompt 轮次。
- **不做发给模型的细粒度 capability token。** Harness 不给 模型一份 token 再由 host 校验；gate 是进程内同步检查。 模型无法事后重放这个检查。

### 10.3 关注点的"一屏阅读清单"

| 关注点 | 文件 | 行 |
|---|---|---|
| 主循环 | `cli/src/omni/core/react_agent.py` | 全文 |
| 终止 vocabulary | `cli/src/omni/core/termination.py` | 245 |
| 工具面 | `cli/src/omni/agent/tool_surface.py` | 47-93 |
| JSON-Schema 编译 | `cli/src/omni/core/tool_contracts.py` | 252-333, 410-464 |
| Tool gateway | `cli/src/omni/runtime/tool_gateway.py` | 164, 290-510 |
| Approval | `cli/src/omni/core/approval.py` | 321-803 |
| Approval rules | `cli/src/omni/core/approval_rules.py` | 108-504 |
| Sandbox | `cli/src/omni/skills_runtime/sandbox.py` | 47-405 |
| Subagents | `cli/src/omni/agent/subagents.py` | 60-707 |
| Subagent control | `cli/src/omni/agent/subagent_control.py` | 79-307 |
| Compaction | `cli/src/omni/memory/compaction.py` | 117-396 |
| Memory service | `cli/src/omni/memory/service.py` | 39-750 |
| Sensitive paths | `cli/src/omni/core/sensitive_paths.py` | 22-131 |
| Session lifecycle | `cli/src/omni/agent/session_lifecycle.py` | 181-386 |
| Conversation store | `cli/src/omni/agent/conversation_store.py` | 76-449 |
| Scheduler | `cli/src/omni/runtime/scheduler.py` | 95-500 |
| Schedule service | `cli/src/omni/scheduling/service.py` | 73-552 |
| Cost | `cli/src/omni/agent/cost.py` | 25-342 |
| Hooks | `cli/src/omni/runtime/hooks.py` | 217-509 |
| Tool errors | `cli/src/omni/core/tool_errors.py` | 13-335 |
| Task recovery | `cli/src/omni/runtime/task_recovery.py` | 160+ |
| Idle watchdog | `cli/src/omni/core/llm/idle.py` | 30-123 |
| Turn clock | `cli/src/omni/core/turn_clock.py` | 38-118 |
| Execution control | `cli/src/omni/core/execution_control.py` | 43-201 |
| Cancel-persist | `cli/src/omni/runtime/cancel_persist.py` | 26-198 |
| Logging | `cli/src/omni/runtime/logging_config.py` | 1-371 |
| Storage | `cli/src/omni/storage/db.py`, `cli/src/omni/storage/models.py` | 70-200, 全文 |

---

## 附录 A — 从代码里抽出的设计原则

这些是注释和结构反复重申的原则——是上面每个决定的"为什么"。

1. **模型不是权威。** Wire 格式 host-owned 且封签；模型的 返回值是数据，不是 verdict。
2. **Reach 和 exposure 是分开的。** 把工具从 per-iter catalog 拿掉是 token 决定；移除 reach 是策略决定。混了会让 token 优化破坏 reach。
3. **每条改状态的路径都有 host-owned 对应物。** Host 快照、hash、持久化、锁、跑策略，事后检查用**同一个** hash。
4. **Cancel 是 durable，不是 process-local。** 重启不能 取消长跑 turn；cancel 必须能跨重启。
5. **Approvals 是 grant 形态，不是二元的。** 用户说"X 是"
靠 grant X，不是 toggle 标志。
6. **安全边界默认 fail-closed。** Sandbox 后端拿不到， harness 抛；fallback 是显式的"无 sandbox + warning"。
7. **Memory 是分层的，M1 永不跨会话。** 对话里的随口话 留在产生它的对话里。
8. **Harness 是一小套命名良好的 primitives，不是框架。** Reach、exposure、replay_safe、mutating、hunt window、 contract hunt、lookup pressure、leftover skill pressure ——这些是名字。读代码你能预测下一个设计决定，因为 词汇一致。
9. **Harness 会引用它的同行。** 注释在 design choice 与 Codex、Claude Code / openclaw、HelixForge 共享时点名 对照。这是代码可对已知参考设计审计的基础。
