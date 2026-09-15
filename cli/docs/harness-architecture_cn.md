# Harness 架构：九个职责

> 一份从宏观视角看 OmniScientist agent harness 的文档。本文 解释 *为什么* 这个 harness 长这样，以及在每个"主流 agent harness 都必须覆盖"的九个职责内，harness 做了什么设计选择。 本文刻意停在 file:line 之上。
>
> 想要逐行的实现分析，请看 [harness-dimensions.md](harness-dimensions.md)（英文）/ [harness-dimensions_cn.md](harness-dimensions_cn.md)。想要用户视角 的 invariants 和已建成的模块图，请看 [agent-runtime-harness.md](agent-runtime-harness.md) 和 [architecture.md](architecture.md)。

## 1 为什么 "agent" 其实是 harness

任何成熟的 agent 系统里，真正有意思的工作不在 model。Model 是数据——它把一个 prompt 变成下一个 prompt 加上少量具名 工具调用。围绕这一切——什么时候停、模型能用什么工具、模型 能记什么、模型被允许删什么、子 agent 怎么派活、crash 后什么 能恢复——是 **harness**。Harness 是 model 运行于其中的那 个小型操作系统。

这已经是几大参考设计（Codex、Claude Code / openclaw、 HelixForge）的共识看法。OmniScientist 跟这些参考一样，把 绝大部分复杂度预算花在 harness 设计上，而不是 prompt 工程。

"harness" 这个设计空间可以拆成九个职责，任何严肃实现 都得覆盖。把它们命名出来是第一步；在每个职责内做站得住脚 的选择，是剩下的工作。

| # | 职责 | 它回答的问题 |
|---|---|---|
| 1 | **主循环** | Model 什么时候拿到一轮？什么时候停？什么时候升级？ |
| 2 | **工具层** | 能力怎么注册成可调用的接口？怎么告诉 model？Model 能拿它们做什么？ |
| 3 | **上下文管理** | 什么放进 model 的 context window？满了扔什么？跨 turn 持久什么？ |
| 4 | **权限与沙箱** | Model 允许碰什么？什么必须请示？物理上不可能做什么？ |
| 5 | **子 agent 调度** | 父 model 什么时候把活儿派给 specialist？Specialist 看见什么？返回什么？ |
| 6 | **会话与状态** | 什么是 session？什么是 task？crash 后什么还在？什么能恢复？ |
| 7 | **可观测** | 人怎么看 model 干了什么？trace 放在哪？怎么算成本？ |
| 8 | **Hook 与拦截点** | Owner 怎么在不改循环的前提下插入策略？Hook 能看见什么、能拦什么？ |
| 9 | **错误恢复与重试** | 工具调用失败怎么办？Model 幻觉怎么办？预算耗尽怎么办？ |

每个现代 agent harness——Claude Code、Codex、openclaw、 HelixForge、OmniScientist——都覆盖这九项。它们的不同在于 *怎么* 覆盖。本文余下部分按顺序走每一个，呈现设计空间， 并解释 OmniScientist 做了哪个选择。

只要读一屏，跳到 §3（跨切面属性）。要看把九个串起来的数据 流，跳到 §4。

---

## 2 九个职责

### 2.1 主循环

**问题。** Model 是个函数：`messages → next_message`。一个 有用的 agent 是 *进程*：它得迭代（调工具、读结果、再调一个）， 得约束自己（按时间、按步数、按 cost），得知道什么时候停。

**设计空间。** 三个结构选择主导一切：

1. **有界 vs 无界迭代。** 无界循环是个雷。每个主流 harness 都给迭代加 cap；问题在 *到 cap 后做什么*——拒绝继续， 还是再走一次 `tool_choice="none"` 的收尾调用。
2. **停的决定在哪。** 停可以是 host 的决定（turn clock、 工具调用数、cost 上限）或者 model 的决定（model 说 "done"）。Host 控制是常规；纯 model 控制不安全。
3. **反思的颗粒度。** Model 是每步反思、只在 stall 时反思、 还是只在终止时反思？便宜的 prompt-level steering 通常 就够；昂贵的 meta-prompt（问 model "你卡住了么？"） 是常见反模式。

**OmniScientist 的选择。** 单个有界 ReAct 循环，三层时间 尺度（stall watchdog、wall-clock deadline、soft-notice threshold），14 种不同的终止原因，最后跑一次 `tool_choice="none"` 的收尾 LLM 调用。反思在 prompt 层 （一组命名的 steer 字符串：opening-tool nudge、contract- hunt nudge、lookup-pressure nudge），不在循环层。代码里 直接记录了选 prompt 层的原因："We never send a provider `tool_choice="required"` because not every upstream honors it. We steer with a prompt nudge and verify the call landed, mirroring openclaw's tool_choice contract; codex likewise always sends `"auto"` on the wire."

### 2.2 工具层

**问题。** Model 得能行动。Harness 得把动作注册成 model 可 调用的命名接口，给 model 每个的参数 schema，把调用路由到 真正的代码，校验结果，把结果以 model 能用的形式还回去。

**设计空间。**

1. **Wire 格式。** OpenAI function-calling、Anthropic tool-use、MCP 或自定义协议。选择主要由 model 家族 决定，但解析和校验是 harness 的事。
2. **Schema 来源。** 可以手写、可以从运行时类型生成、 可以跟 skill 一起声明。"跟 skill 一起声明"是让第三方 skill 可用的模式。
3. **Model 能 name 什么 vs 看见什么。** Model 能不能 调一个工具是 *reach*；model 知不知道这个工具存在是 *exposure*。把两者混了，会把一个 token 优化（省一个 少用的 schema）变成"model 说 `unknown tool 'write_file'`" 陷阱。
4. **工具有多不可信地声明成功。** 是 model 自己说"成功"， 还是 host 单向封签结果？

**OmniScientist 的选择。** Wire 上是 OpenAI 风格 function-calling（其它格式在 client 层翻译）。Schema 跟 skill 一起声明，在注册时编译一次（`Draft202012Validator`， 外链 `$ref` 锁死，所有 `$recursiveRef` 预解析）。 **Reach 和 exposure 是分开的**：一个工具可以按名字 reachable， 但不在 per-iteration `tools` 数组里。一个 deferred 工具 首次被调用时，自动 promote 到 advertised，后续迭代都看得到。 工具结果是 host 单向封签的：工具不能伪造 `succeeded` verdict；host 用 `_host_seal` 单向 mint 这个 verdict。

### 2.3 上下文管理

**问题。** Model 的 context window 有限。Harness 得决定 放什么进去、满了扔什么、什么放边上、相关时怎么从边上 召回。

**设计空间。**

1. **System prompt 里放什么。** Identity、工具目录、 计划规则、环境、示例、recall 的 memory、项目上下文 都是候选。组合是预算化的混合；溢出时扔什么是策略 决定。
2. **History 里放什么。** 对话本身，加上工具调用和 observations。Model 看到的是 *transcript*，不是 run 框架；任何元状态被注入到 transcript 里就是泄露。
3. **压缩。** Context 满了时，三种选择：丢（失数据）、 总结（LLM 或启发式）、rollover（写 checkpoint， 从那里重启）。每一种适配不同尺度的溢出。
4. **跨会话 memory。** 什么能跨会话、什么不能。把 对话里的随口话当成 durable 知识是常见 bug；显式 排除它们是设计选择。

**OmniScientist 的选择。** 六段式 system prompt，其中**三 段** *根据 turn catalog 条件切换*——model 看到的 prompt 取决于本 turn 在 scope 内的工具是哪些。History 装载为 `system + filtered_history + user`，只透传五个 key（不 注入元数据）。压缩分两阶段：per-iteration *microcompact* 把老的 tool observations 头尾裁（保护失败 observations 和研究锚点），跨 iteration *rollover* 在窗口接近上限时 写一份 model 写的 JSON checkpoint，model 写不出有效 checkpoint 时回退到 host-owned 的 `evidence_checkpoint`。 跨会话 memory 是五层模型（M1 SESSION → M2 TASK → M3 EPISODIC → M4 SEMANTIC → M5 ARTIFACT），**M1 永不跨会话** ——这是 memory 模块里最重要的一条规则。

### 2.4 权限与沙箱

**问题。** Model 是个函数，没有天生判断力。Harness 得决定 什么它可以自由做、什么必须请求许可、什么物理上不可能。

**设计空间。**

1. **gate 的位置。** Gate 可以在 system prompt 里（不要做 X）、在调用时的策略检查里（拒绝匹配模式的调用）、或在 OS 本身（沙箱、容器、VM）。每层有不同的成本和保证。
2. **批准粒度。** 二元的"总是问"对流不友好。Grant 形态 的"X 是"（一个 exact 命令、一个校验过的 argv prefix、 或一个 turn-scoped workspace trust）让用户能精确。
3. **什么算敏感路径。** Credentials、VCS 目录、运行时 状态。列表短但不可妥协；symlink 解析后的检查关掉了 "看似无害的 symlink 指向 .env"的 TOCTOU bypass。
4. **沙箱姿态。** Fail-closed（拿不到后端就抛）还是 fail-open（warn 然后继续）。生产系统几乎都选 fail-closed；一次 sandbox escape 的成本比偶尔 broken session 的成本大。

**OmniScientist 的选择。** 单个 `ToolGateway` 是每个工具 调用的 choke point（model、skill、subagent——没有旁路）。 Gate 有三层：`ToolPolicyGuard` 强制 allow/block 列表和 per-tool 预算，`ApprovalGate` 对真正敏感的调用问用户， OS 级沙箱（Darwin seatbelt、Linux bwrap、Linux firejail） 物理上限制文件写。批准是 grant 形态（exact / argv prefix / task-bash），IM 渠道（wechat/feishu/dingtalk）永不继承 task-bash grant。沙箱是 fail-closed：拿不到任何后端就抛； fallback 是显式的"无 sandbox + WARNING"，不是悄悄降级。 VCS 目录（`.git`）在每层都被拒，没有任何 `output_roots` 配置能放松它。

### 2.5 子 agent 调度

**问题。** 一些任务对主循环来说太大或太聚焦。Harness 得 让父 model 把活儿派给 specialist，分享足够的上下文让 specialist 能干，收一个自包含的结果回来。

**设计空间。**

1. **harness 是否有固定的 subagent 类型分类法** （一个"explore" subagent、一个"writer" subagent、 一个"reviewer" subagent）还是把 subagent 当成 带 free-text role 的通用原语。固定分类法更易推理； 通用原语更灵活。
2. **Specialist 看见什么。** 完整 transcript、一个摘要、 一个 context 指针、或者什么都没有。上下文多有用但贵， 有让 specialist 重述父推理的风险。
3. **嵌套。** Specialist 能不能派生 specialist？到 多深？无限嵌套是无限循环的配方；不嵌套是错过 组合机会。
4. **质量控制。** Specialist 的输出返回前被复审吗？ 用什么复审——硬编码检查、LLM judge、还是人？一个 便宜的 LLM-as-judge pass 抓住大部分明显失败。

**OmniScientist 的选择。** 没有固定分类法。Subagent 是一 个通用原语，带 free-text `role` 字段；"reviewer" 是 specialist runner *内部* 的一个 LLM-as-judge 循环，不是 独立的 agent 类。Specialist 看不见 transcript——只看见 分配的 goal、role、可选的 `file_uris` inbox，以及一条 明确的 system prompt 告诉它返回一个自包含的 final answer （因为"coordinator 只看见 final answer"）。Specialist 的返回被截到 6 000 字符。嵌套上限 depth 2（父 + 一层 specialist）。每个 subagent 拿自己的 `task_id`、自己的 resource-lock pool 引用、自己的预算。Reviewer gate 最多 多一轮——便宜的质量检查，不是深度审计。

### 2.6 会话与状态

**问题。** 真实的 agent 工作流横跨多个 turn、多个 session、 多个后台任务，还有 crash。Harness 得把对的状态放在对的地方， 扛住重启，让人能从断的地方接上。

**设计空间。**

1. **状态在哪。** 在内存里、在文件里、在数据库里、在远程 服务里。每种选择有不同的持久性、扩展性和运维属性。
2. **什么是 "session" vs 什么是 "task"。** Session 是 聊天线；task 是工作单元。把它们混了是常见 bug—— session 是用户面的抽象；task 是 durable 记录。
3. **Cancel 语义。** Process-local cancel 很脆弱（服务器 重启会丢）。Durable cancel 跨重启有效，但实现得小心， 让一次良性的重启不要意外取消一个长跑的 turn。
4. **Retry lineage。** Task 被重试时，什么不变（input、 goal）什么是新（attempt 编号、events）。没有稳定的 `input_snapshot_json` 和 `root_task_id` 链，你没法 推理一个被重试的 task。

**OmniScientist 的选择。** 每个 workspace 一个 SQLite 数据库，外加一个 home-level SQLite 装 control state （schedules、global task index）。WAL 模式让 daemon 和 CLI 并发读；`ApplicationId=0x4F4D4E33`（"OMN3"）stamp 当前 store shape。Session 和 task 是不同的对象；session 是聊天线，task 是工作单元，长跑的工作单元是带 `Schedule` 的 task。Cancel 是 durable：process-local cancel 和 DB-backed cancel 是两件事，重启不会取消一个 长跑的 turn。每个 task 带一个不可变的 `input_snapshot_json`、 一个 retry lineage（`retry_of_task_id / root_task_id / attempt`）、和两个 content-addressed fingerprint（plan
+ catalog + contract + pre-grants）。

### 2.7 可观测

**问题。** 一个不能被检视的 harness 是一个不能被 debug 的 harness。Harness 必须把发生过的事记到足够细，让人（或者 未来的 agent）能重建这次运行、归属成本、找到错误、解释 model 的选择。

**设计空间。**

1. **trace 记什么。** Events、tool calls、plans、errors、 costs、latencies。最小有用集合小；最大有用集合大。
2. **trace 在哪。** 在文件里（text 或 JSONL）、在数据库 里、在 metrics 系统里、在远程服务里。Local-first 系统 选文件 + 数据库；SaaS 选远程服务。
3. **Model 怎么看。** Model 能不能访问自己的 trace（用来 自纠错），还是只有 harness 看？两种各有各的用处。
4. **成本归属。** Per-call、per-task、per-session、per- component。没有这个，回答不出"这次跑了多少钱？"

**OmniScientist 的选择。** 单个 append-only `task_events` 表，事件名遵循 `<component>.<verb>` 约定。Hook 成功 / 失败本身是事件，所以单个 lifecycle hook 的 timing 和 stdout 跟 model 调用在同一条流里。一张独立的 `control.sqlite3` 装跨 workspace 的 `TaskIndex`，让 `omni task --all` 不必扫每个 workspace。Token 用量按 每次调用记为 `cost.usage` 事件；计量失败被吞掉，所以 它从来不阻塞 turn。日志是单行 JSON-ish 记录，token redact 默认开，有一条硬规则反对 `basicConfig`（caller handler 保留）。Lab notebook（`<workspace>/NOTEBOOK.md`） 是 DB 事件的人类可读、git 友好对位。没有 Prometheus 或 OpenTelemetry exporter，也没有 web dashboard——DB 和 log 文件就是接口。

### 2.8 Hook 与拦截点

**问题。** 系统的 owner 必须在不改写循环的前提下能插入 策略。Harness 必须暴露一组小的、命名良好的扩展点，并保证 这些扩展点不能被用来 compromise host。

**设计空间。**

1. **Hook 能看见什么。** 工具调用（name、arguments、context）、 工具结果（或者只是调用）、或者什么都看不见。可见越多 越强。
2. **Hook 能做什么。** Allow、deny、modify、log、发外部 通知。"Deny" 是强权；其它都是 observability。
3. **Hook 的失败模式。** 静默忽略 hook 失败是安全默认； 让 hook 把循环搞崩是 footgun。
4. **谁能装 hook。** 只有 owner、还是任何持有 config 文件 的人？Owner-only 是任何能 deny 的东西唯一安全的答案。

**OmniScientist 的选择。** 一小组命名的拦截点——pre-tool、 post-tool、和几个 LLM-event hooks（notices、token deltas）— 通过 `invoke_tool_with_hooks` 暴露，它是每个工具调用 必经的 choke point。Hook 是 owner-controlled 命令（不能 进程内 patch 代码），用 `asyncio.create_subprocess_exec` 执行（无 shell），stdin 是 redacted JSON，stdout 是单个 JSON object，有硬性 output cap 和严格 timeout。Hook 返回的决定可以 `allow` 或 `deny`；hook 失败被记为 warning，主循环继续。Pre-tool hook 之后，一个 `ExecutionPolicyFrame` 用 `sha256(canonical_args)` 做 key，确保授权作用在特定一次调用实例上，不能被另一个 task 重放。其它拦截器同因存在：`defend_observation` 在 tool observations 里中和 prompt-injection 模式； `execution_ownership` 把每个 subtask 钉到一个 PID， 活进程永远不被 settler "偷"；MCP client 把不可达 server 当 warning，不是 catalog-wide failure。

### 2.9 错误恢复与重试

**问题。** 事情会失败。Model 幻觉出不存在的工具。网络抖。 用户按 Ctrl-C。磁盘满。Harness 必须分类失败、决定哪些 重试、决定什么时候放弃。

**设计空间。**

1. **错误怎么分类。** 一小组稳定错误类，model 可以直接 branch（invalid args、retryable I/O、partial success、 unpayable、fatal-turn）比自由文本错误字符串有用得多。
2. **什么重试、怎么重试。** 网络级重试便宜；语义重试 （重 prompt model）贵。Mutating 操作永远不该因为 网络失败就重试。
3. **熔断。** 某个 (tool, args) 对失败 N 次后拒绝再调。 问题在 N 是多少、key 是什么（只看工具名、还是工具 + 参数、还是 meta-tool 的 *subject 参数*，让一个坏的 skill 不毒化路由器）。
4. **升级。** 循环走不动时，失败模式是什么？一次 wrap-up 调用、一个"escalate"到后台任务、一次硬停、 还是 fallthrough 到别的 skill。

**OmniScientist 的选择。** 五个 host-owned 错误类 （`INVALID_ARGS` / `RETRYABLE_IO` / `SKILL_FAILED_PARTIAL` / `UNPAYABLE` / `FATAL_TURN`），在 wire 上稳定，所以 model 策略代码可以直接 branch。LLM 侧独立分类 （transcript-invalid、authentication、output-cap- truncated），因为 LLM 失败的形状不同。网络级重试：LLM client 最多 2 次，优先 `Retry-After`；tool calls 最多 1 次，且仅在 `replay_safe` 工具上。两套独立熔断：一个 executed-failure 计数器（5 跳闸，key 用 (tool, args)； meta-tool 用 *subject 参数* 做 key，让一个坏 skill 不 毒化路由器）和一个 unexecuted-refusal 计数器（5 触发 `no_progress` 终止）。升级是一等公民：一个专门的 `escalate_run` 工具，model 可以调它把对话交给一个 durable 后台任务，带显式 depth 限制防空工具循环。

---

## 3 跨切面属性

九个职责不是独立的。几个属性横跨所有——是把系统连在一起的 设计约束。如果你只读本文一节，读这一节。

1. **Model 不是权威。** 每种 wire 格式是 host-owned， host 代码校验。Model 的返回值是数据，不是 verdict。 这出现在工具层（封签结果）、权限层（harness 是 gate，不是 prompt）、错误恢复层（host 分类失败）， 以及一切其它地方。

2. **Reach 和 exposure 是分开的。** 一个工具可以按名字 reachable，但不在 per-iteration schema 数组里。这一 个区分就是让 token 优化不破坏 reach 的原因。

3. **每条改状态的路径都有一个 host-owned 对应物。** Host 快照输入、对它们 hash、持久化 `start` 事件、拿文件锁、 跑策略，事后检查用**同一个** hash。Model 从来不能在 事后假装它没要求过写入。

4. **Cancel 是 durable，不是 process-local。** 服务器重启 不能取消长跑的 turn；cancel 必须能跨重启。Harness 区分两者，并保证对的地方用对的。

5. **批准是 grant 形态，不是二元的。** "X 是"靠 grant X —— exact、argv prefix、或 task-bash。二元的"总是问" 对流不友好；"每次都问"对一致性不友好。

6. **安全边界 fail-closed。** 沙箱后端拿不到，harness 抛。Fallback 是显式的"无 sandbox + warning"路径， 不是悄悄降级。`.git` 目录在每层都被拒，没有任何 `output_roots` 配置能放松它。

7. **Memory 是分层的，M1 永不跨会话。** 对话里的随口话 留在产生它的对话里。这一条规则防止了长跑 agent harness 最大的 bug。

8. **Harness 是一小组命名良好的原语，不是框架。** Reach、 exposure、replay_safe、mutating、hunt window、contract hunt、lookup pressure、leftover skill pressure——这些 是名字。读代码你能预测下一个设计决定，因为词汇 一致。

9. **Harness 引用它的同行。** 注释在 design choice 与 Codex、Claude Code / openclaw、HelixForge 共享时 点名对照。这是代码可对已知参考设计审计的基础。

---

## 4 九个职责怎么串起来

单个用户请求的数据流跨过所有九个职责，但不是平铺的顺序。 同一次物理操作——调一个工具——同时是 *主循环里的一轮*、 *session 里的一个改状态事件*、*一次权限 gate 的行动*、 *一次工具层路由*、*一次 observability 事件*、*一次潜在 hook 触发*、*一次潜在重试*。

```
                            ┌────────────────────────────────────┐
                            │ 1. 主循环（ReAct）                 │
                            │   - 有界，三层时间尺度             │
                            │   - 14 种终止原因                  │
                            │   - prompt-level reflection        │
                            └──────────────┬─────────────────────┘
                                           │ calls tools
                                           ▼
   ┌─────────────────┐  pre/post  ┌────────────────────────────────┐
   │ 8. Hooks        │◀──────────▶│ 2. 工具层                       │
   │   - owner-ctrl  │            │   - reach vs exposure          │
   │   - subprocess  │            │   - sealed outcomes            │
   │   - denyable    │            │   - reach via ToolGateway      │
   └─────────────────┘            └──────────────┬─────────────────┘
                                                  │  every call
                                                  ▼
   ┌─────────────────┐  every call  ┌────────────────────────────────┐
   │ 4. 权限          │◀────────────│ 单一 ToolGateway                │
   │   - policy guard │             │   (choke point)                │
   │   - approval gate│             └──────────────┬─────────────────┘
   │   - OS sandbox   │                            │
   └─────────────────┘                            │
                                                  │  writes to
                                                  ▼
   ┌─────────────────┐  reconciles  ┌────────────────────────────────┐
   │ 6. Session/state │◀────────────│ 9. 错误恢复                     │
   │   - SQLite WAL   │             │   - 5 host-owned 错误类         │
   │   - lineage      │             │   - 2 熔断器                    │
   │   - durable cancel│            │   - escalate_run fallback      │
   └─────────────────┘             └──────────────┬─────────────────┘
                                                  │  records
                                                  ▼
   ┌─────────────────┐  from all layers  ┌────────────────────────────────┐
   │ 7. 可观测        │◀──────────────────│ Append-only task_events        │
   │   - JSONL log   │                   │   - <component>.<verb> 命名    │
   │   - cost events │                   │   - control.sqlite3 index      │
   │   - lab notebook│                   │   - log file + DB + notebook   │
   └─────────────────┘                   └────────────────────────────────┘

   Above all: ┌────────────────────────────────────────────────────┐
              │ 3. 上下文管理                                     │
              │   - 6 段式 system prompt，3 段条件切换             │
              │   - 两阶段压缩（microcompact + rollover）         │
              │   - 5 层 memory；M1 永不跨会话                    │
              │   - recall 是混合（keyword + vector + graph）     │
              └────────────────────────────────────────────────────┘

   And on demand: ┌────────────────────────────────────────────────┐
                  │ 5. 子 agent 调度                                │
                  │   - 通用原语，free-text role                     │
                  │   - depth ≤ 2；specialist 不见 transcript      │
                  │   - reviewer = LLM-as-judge 循环（非类）       │
                  └────────────────────────────────────────────────┘
```

形状密但可控。两个让它可导航的事实：(a) 每个工具调用都过 一个 choke point（`ToolGateway`），(b) 每个改状态事件 跑之前都已 durable。

---

## 5 本文不是什么

这是宏观视角。刻意停在 file:line 之上。逐行的实现分析 （每个选择在哪个文件、常量是什么、有什么实现例外）请看 [harness-dimensions.md](harness-dimensions.md) / [harness-dimensions_cn.md](harness-dimensions_cn.md)。 要看一个用户请求沿时间的走法，请看 [request-lifecycle.md](request-lifecycle.md) / [request-lifecycle_cn.md](request-lifecycle_cn.md)。要看 已建成的模块图和请求流，请看 [architecture.md](architecture.md)。 要看用户视角的运行时 invariants，请看 [agent-runtime-harness.md](agent-runtime-harness.md)。
