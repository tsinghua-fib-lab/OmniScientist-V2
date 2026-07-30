# 设计稿 04：SoulAgent

## 定位

SoulAgent 是一个 **Skill**——它被主 Coding Agent 安装、启用、触发、关闭。Skill 内部封装了一个 Core，执行四步逻辑。

Skill 不内置任何科学家人格。KG 文件是外部输入，放在当前项目的 `scientist-kg/` 目录下。

---

## Skill 层与 Core 层

```
┌─ Skill 层（主 Agent 可见）─────────────────────────┐
│  SKILL.md       声明文件，主 Agent 读取              │
│  发现可用人格     扫描 scientist-kg/，列出可选的科学家 │
│  用户确认         用户选择一位科学家（自然对话）        │
│  自动装载         检测科学任务 → 调用 Core             │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─ Core 层（主 Agent 不可见）─────────────────────────┐
│  ① 感知     对话 → TaskFrame                         │
│  ② 裁剪     扩散激活 → KG 子图                        │
│  ③ 解码     LLM → 人格 prose                         │
│  ④ 覆写     根据 Host 只更新对应的一个造口             │
└─────────────────────────────────────────────────────┘
```

**没有命令。** SoulAgent 和主 Agent 之间是自然语言交互。Skill 声明文件告诉主 Agent：

> "我是一个科学家人格装载器。你项目下的 `scientist-kg/` 目录里有可用的科学家 KG。
>  当用户需要科学判断帮助时，你先主动告知用户可选哪些人格，请用户确认。
>  装载后，每当用户在讨论科学问题，你就自动触发我来刷新当前的人格描述。"

**装载流程**——主 Agent 主动发起：

```
主 Agent 启动，扫描 scientist-kg/，发现三个 KG
  → 主动对用户说：
    "我注意到这个项目下有三份科学家人格：何恺明、Yann LeCun、徐丰力。
     需要装载一位来辅助思考吗？输入名字即可。"

  → 用户："装何恺明"
  → 主 Agent 确认："好的，已装载 Kaiming He 的研究人格。之后讨论科学问题时我会以他的判断方式为参考。"
  → 触发 SoulAgent，装载 scientist-kg/kaiming-he/ 目录下的 KG
  → 之后每轮科学任务自动刷新人格
```

**切换科学家**——用户有切换意图时，主 Agent 确认：

```
用户："帮我用 Kaiming He 的思维方式来设计这个实验"
  → 主 Agent 当前未装载任何人格，主动列出可选清单，用户确认后装载

用户："换 Yann LeCun 的方式来看看"
  → 主 Agent："确认从何恺明切换到 Yann LeCun？"
  → 用户确认 → 卸载当前，装载新的

用户："不用科学家的人格了，恢复你自己"
  → 主 Agent："确认卸载。后续我将恢复我自己的判断方式。"
  → 卸载，造口恢复原始状态
```

---

## ① 感知：从对话推断当前科学任务

**输入**：主 Agent 的当前对话（最近几轮的 user message + agent response 摘要）。

**输出**：一个 TaskFrame。

```json
{
  "phase": "experiment_design",
  "objective": "用户让我设计一个实验方案来验证新方法的有效性",
  "constraints": {
    "compute_constraint": true,
    "time_pressure": false
  }
}
```

**phase 枚举**——从对话中推断：

| phase | 含义 | 典型触发词 |
|-------|------|-----------|
| `problem_formulation` | 定义或重新框定问题 | "这个问题应该怎么定义""换个角度想" |
| `method_selection` | 选择方法、工具、架构 | "选哪个 backbone""用什么框架" |
| `experiment_design` | 设计实验、消融、基线 | "怎么设计实验""怎么消融" |
| `result_analysis` | 分析结果、解释现象 | "这个结果说明什么""为什么变好了" |
| `review` | 审稿、批判性评估 | "帮我审一下这篇""这个方法有什么问题" |
| `failure_diagnosis` | 排查失败、定位问题 | "为什么没效果""哪里出错了" |
| `ideation` | 产生或筛选新想法 | "有什么新方向""还值得做什么" |
| `implementation` | 落地为代码 | "帮我实现这个""怎么写" |
| `general` | 无法明确分类的科学对话 | — |

如果无法判断 phase，使用 `general` 作为 fallback。如果当前对话不属于科学任务，Core 退出，不做任何操作。

**constraints 推断**——从对话中捕捉：
- `compute_constraint`：用户提到"资源有限""GPU 不够""跑不了太多实验"则为 true
- `time_pressure`：用户提到"赶 deadline""快速""紧急"则为 true

### 触发条件

**不是每轮都触发。** 只有以下情况才重新跑裁剪→解码→覆写：

| 触发条件 | 说明 |
|---------|------|
| phase 变化 | 用户从"设计实验"转到"分析结果"，phase 变了，需要不同的人格侧面 |
| objective 显著变化 | 同一 phase 内，用户转向了不同的具体任务 |
| constraints 变化 | 用户说"GPU 管够了"或"现在得赶 deadline 了" |
| 科学家被切换 | 用户显式要求换科学家 |
| KG 目录被更新 | 蒸馏器更新了 `scientist-kg/{id}/`，SoulAgent 检测到 manifest hash 变化 |

**不触发的情况**：

- 同一科学任务内的连贯对话——用户连续几轮都在讨论同一个实验设计，不重新裁剪
- 普通代码编辑——用户只是修改代码实现，不改变科学判断需求
- 用户发了一条与科学任务无关的消息

### 并发防护

SoulAgent 覆写造口时主 Agent 不能读。如果 SoulAgent 正在覆写中，主 Agent 又触发了新一轮（用户快速发了两条消息）：

```
SoulAgent 正在覆写
  → 主 Agent 发现 lock/writing 存在，暂停等待
  → 用户又发了一条消息，主 Agent 判断需要重新感知
  → 主 Agent 不打断当前覆写，等 ready 出现后，用最新的对话状态重新触发
  → 如果连续两次触发之间的 TaskFrame 相同，跳过第二次
```

---

## ② 裁剪：扩散激活 → KG 子图

**输入**：TaskFrame + 目标科学家的 KG 目录。

**输出**：KG 子图（哪些 L2 被激活 + L1 证据 + L3 全量）。

### 2.1 种子命中

| phase | 直接命中的 L2 |
|-------|-------------|
| `problem_formulation` | C01 怎样定义问题 |
| `method_selection` | C02 怎样选择方法、C05 怎样判断美丑 |
| `experiment_design` | C02 怎样选择方法、C03 怎样验证结论 |
| `result_analysis` | C03 怎样验证结论、C04 怎样解释结果 |
| `review` | C03 怎样验证结论、C04 怎样解释结果 |
| `failure_diagnosis` | C06 怎样处理失败、C03 怎样验证结论 |
| `ideation` | C07 怎样产生想法、C01 怎样定义问题 |
| `implementation` | C02 怎样选择方法、C06 怎样处理失败 |
| `general` | objective 文本与所有 L2 的 trigger_contexts 语义匹配 |

### 2.2 扩散激活

种子 L2 确定后，沿 KG 中的边传播：

- **强化边**：若 A 入选，B 连带入选。A 和 B 是同一认知姿态的不同侧面。
- **条件边**：若 B 入选，A 连带入选（单向——不理解前提就看不懂结果）。
- **张力边**：若 A 和 B 同时入选，检查当前 constraints 是否触发该张力的 context。触发则做取舍，不触发则共存。
- **L3**：P01-P03 始终全量入选，完整注入，不压缩。P04 不参与裁剪或图遍历，
  只把原句放入 SoulPack 的语气字段。
- **L1**：每个入选的 L2 沿支撑边拉出其归属 L1 的前 5 条。L1 不参与扩散——它是叶子。L1 排序规则：优先取 L3 的 exemplar_L1（如果有）、其次按 source 类型多样性（paper/talk/code 混合优先）、同类型内按与当前 objective 的语义相关性排列。

### 2.3 约束

- L2 至少 2 个，至多 5 个

### 2.4 SoulPack 人格内核

```json
{
  "philosophy_kernel": {
    "stances": ["P01", "P02", "P03"],
    "tone_exemplars": ["P04 中的 3-5 条原句"]
  }
}
```

`tone_exemplars` 只控制解码措辞的节奏、句长和谦逊或攻击程度，不作为事实、
立场或图关系参与裁剪。

---

## ③ 解码：KG 子图 → 人格 prose

**输入**：裁剪出的 KG 子图 + TaskFrame。

**输出**：一段自然语言 prose。LLM 完成此步骤。

**解码 prompt 约束**：

```
基于该科学家的 KG 子图，生成一段人格描述文本。

规则：
1. 写成 Coding Agent 的行为指导，不是科学家传记。
2. 不出现 L1/L2/L3、C01-C07、node_id 等内部编码。
3. 每条指导必须能从子图中的 L2 找到出处。
4. 以当前任务为语境——针对用户此刻正在做的具体科学任务来说明该怎么做。
5. 写中文。自然、可读。

**语气**：用下述原句的语气节奏来写。模仿句长、用词偏好、攻击性/谦逊程度：
{P04 语气：逐条列出原句}

输出结构：
## 当前人格：{科学家姓名}
### 核心原则
{P01-P03 完整，不压缩}
### 当前任务中的思考方式
{从激活的 L2 展开，每条 2-3 句可执行的指导}
### 当前取舍
{如果张力被触发，说明为什么在当前任务下选A不选B}
### 证据
{被选中的 L1 证据，每条：论文标题 + 观察}
```

---

## ④ 覆写与恢复

### Host 与造口

主 Agent 调用 Core 时必须用 `--host` 声明运行环境。SoulAgent 只操作当前 Host
对应的一个造口，不能写入、备份或恢复其他 Host 的文件。

| Host | 参数 | 造口 |
|------|------|------|
| WorkBuddy | `workbuddy` | `soul.md` |
| Claude Code | `claude` | `claude.md` |
| Codex | `codex` | `agent.md` |
| OmniScientist | `omniscientist` | `role.md` |

### 首次装载时的备份

SoulAgent 第一次为当前 Host 装载科学家人格时，只备份该 Host 的原始造口，再覆写。

```
workbuddy      soul.md   → soul.md.soulagent.bak
claude         claude.md → claude.md.soulagent.bak
codex          agent.md  → agent.md.soulagent.bak
omniscientist  role.md   → role.md.soulagent.bak
```

之后每次覆写只更新人格部分，不碰备份文件。

### 切换科学家时的恢复

```
用户："换 LeCun"
  → SoulAgent 把当前 Host 的造口恢复为原始备份
  → 装载 LeCun 的 KG
  → 重新跑 ①→②→③→④，写入新人格
```

### 卸载时的恢复

```
用户："不用科学家了，恢复你自己"
  → SoulAgent 把造口恢复为原始备份
  → 删除备份文件，清理状态
```

### 覆写协议

SoulAgent 在覆写造口前需要让主 Agent 停下来。如果主 Agent 正在读取当前 Host
造口的同时 SoulAgent 在覆写，主 Agent 可能读到半截的文件或旧版本。

```
SoulAgent 准备覆写
  → 通知主 Agent："我要更新人格造口了，暂停一下"
  → 主 Agent 暂停处理当前轮次
  → SoulAgent 只写入当前 Host 对应的造口
  → SoulAgent 通知主 Agent："写完了，继续"
  → 主 Agent 读取新版造口，继续推理
```

**实现方式**：SoulAgent 和主 Agent 之间通过一个标记文件同步。

```
.soulagent/
└── lock
    ├── writing    # SoulAgent 正在覆写，主 Agent 看到此文件即暂停
    └── ready      # SoulAgent 覆写完成，主 Agent 读取后再继续
```

主 Agent 在每轮推理前检查：
- 如果 `writing` 存在 → 等待，直到 `ready` 出现
- 如果 `ready` 存在 → 读取最新造口，继续推理

SoulAgent 覆写时：
1. 创建 `writing`
2. 写入当前 Host 对应的造口文件
3. 删除 `writing`，创建 `ready`

| 文件 | 目标 | 写入方式 |
|------|------|---------|
| `soul.md` | WorkBuddy | 全量替换人格部分 |
| `claude.md` | Claude Code | 全量替换 |
| `agent.md` | Codex CLI | 全量替换 |
| `role.md` | OmniScientist | 全量替换 |

其他 Host 的造口不写、不备份、不恢复。

---

## 失败安全

| 情况 | 行为 |
|------|------|
| KG 不存在或格式错误 | 终止，不注入 |
| 对话不属于科学任务 | 退出，不操作 |
| phase 无法判断 | fallback 为 `general` |
| 解码 LLM 调用失败 | 终止，记日志 |
| 造口写入失败 | 终止，恢复备份 |
| 7 个 L2 全部入选 | 判定裁剪失败，降为全量 L2+L3 |

---

## 目录结构

```
soulagent/
├── SKILL.md              # Skill 声明（主 Agent 入口）
├── core.py               # Core 四步逻辑
├── kg_loader.py          # 读取 KG 目录（manifest + l3/l2/edges/l1-evidence）
├── task_sensor.py        # ① 感知
├── graph_pruner.py       # ② 裁剪
├── kg_decoder.py         # ③ 解码（调用 LLM）
├── stoma_writer.py       # ④ 覆写
└── backup/               # 造口旧版本
```
