# Codex 测试指导文档

本文用于在 Codex 中检查 SoulAgent 是否被正确触发，以及科学家人格能否按任务加载、刷新、切换和卸载。

## 1. 测试对象

本次测试的入口是 `soulagent`：

```text
skills/soulagent/
```

仓库附带的只读示例数据位于：

```text
skills/soulagent/examples/scientist-kg/
├── fengli-xu/
└── kaiming-he/
```

实际运行时，SoulAgent 从测试项目或用户项目根目录的
`scientist-kg/` 读取数据，不把生成结果写回 Skill 安装目录。

测试过程中，Codex 应调用：

```text
Skill(soulagent)
```

如果界面只显示 `Skill(kaiming-he)` 或 `Skill(fengli-xu)`，说明调用的是独立科学家 Skill，没有经过 SoulAgent 的任务感知、图裁剪和动态写入流程。

## 2. 测试前准备

### 2.1 安装 SoulAgent Skill

将下面的整个目录复制到 Codex 的 Skills 目录：

```text
源目录：skills/soulagent
目标目录：<CODEX_HOME>/skills/soulagent
```

Windows 默认的 `CODEX_HOME` 通常为：

```text
C:\Users\<用户名>\.codex
```

安装后重新启动 Codex，使其重新发现 Skill。

### 2.2 配置解码模型

SoulAgent 调用 OpenAI 兼容接口，将裁剪后的 KG 子图解码为当前任务的人格指令。运行 Codex 前，需要在同一个终端中设置：

```powershell
$env:SOULAGENT_API_KEY = "<API_KEY>"
$env:SOULAGENT_MODEL = "<MODEL_NAME>"
$env:SOULAGENT_BASE_URL = "<OPENAI_COMPATIBLE_BASE_URL>"
```

使用 OpenAI 官方接口时，`SOULAGENT_BASE_URL` 可以不设置。不要把 API Key 写进仓库、测试记录或命令参数。

### 2.3 准备独立测试项目

不要直接修改 Skill 内的只读示例。先创建测试项目，并将示例 KG
复制到项目根目录：

```powershell
New-Item ".\soulagent-smoke" -ItemType Directory -Force
Copy-Item ".\skills\soulagent\examples\scientist-kg" `
  ".\soulagent-smoke\scientist-kg" -Recurse
```

### 2.4 从正确目录启动 Codex

SoulAgent 默认从当前项目根目录的 `scientist-kg/` 读取数据，因此应以刚创建的独立测试项目为工作目录启动：

```powershell
codex -C "<仓库路径>\soulagent-smoke"
```

## 3. 最短验证流程

按顺序向 Codex 输入下面的内容。

### 3.1 列出科学家

```text
使用 $soulagent，扫描当前项目的 scientist-kg，列出所有可用科学家，不要启用。
```

预期结果：

- Codex 调用 `Skill(soulagent)`。
- 返回 `kaiming-he` 和 `fengli-xu`。
- 不生成或修改人格文件。

### 3.2 加载何恺明并进行失败诊断

```text
使用 $soulagent，启用何恺明。我的新方法在 COCO 上比 baseline 低了 2 AP，请帮我诊断原因。
```

预期结果：

- 返回状态 `refreshed`。
- 当前 Host 为 `codex`。
- 项目根目录生成或更新 `agent.md`。
- `.soulagent/state.json` 记录 `kaiming-he` 和当前任务阶段。
- 回答围绕当前实验问题展开，不只是介绍科学家生平或模仿说话口吻。

### 3.3 在同一任务中继续提问

```text
使用 $soulagent，继续处理刚才完全相同的 COCO 实验下降问题；先判断是否需要刷新人格，不需要就保持现状。
```

预期结果：

- 如果任务阶段、目标和约束没有实质变化，返回 `unchanged_task`。
- 不重复改写 `agent.md`。

### 3.4 切换任务阶段

```text
使用 $soulagent。当前任务从“分析实验失败”切换到“设计验证实验”，请刷新人格，并设计对照组、消融实验和失败判据。
```

预期结果：

- 返回状态 `refreshed`。
- 任务阶段发生变化。
- 新生成的人格内容更关注实验设计、对照、消融和证据要求。

### 3.5 改变资源约束

```text
使用 $soulagent。实验现在只能使用单卡 GPU，并且必须在 8 小时内完成，请根据新约束刷新当前人格。
```

预期结果：

- SoulAgent 识别到资源约束变化。
- 返回状态 `refreshed`。
- 输出中的实验建议应考虑计算预算和时间压力。

### 3.6 切换科学家

```text
使用 $soulagent，把当前科学家切换为徐丰力，继续分析刚才 COCO 实验下降 2 AP 的问题。
```

预期结果：

- 返回状态 `refreshed`。
- `.soulagent/state.json` 中的科学家变为 `fengli-xu`。
- `agent.md` 被完整替换，不应同时拼接两位科学家人格。
- 回答的判断重点与何恺明版本存在可辨认差异。

### 3.7 检查非科研任务

```text
使用 $soulagent，帮我把 README 的标题改短。先判断这是不是科研任务，不是就不要刷新人格。
```

预期结果：

- 返回 `no_scientific_task`。
- 不修改当前人格文件。

### 3.8 查看当前状态

```text
使用 $soulagent，检查当前加载的科学家、任务阶段、资源约束和人格文件状态。
```

预期结果：

- 能说明当前科学家、Host、任务框架和人格文件位置。
- 不在输出中泄露 API Key。

### 3.9 卸载人格

```text
使用 $soulagent，卸载当前科学家人格，恢复 Codex 原始行为和原始 agent.md。
```

预期结果：

- 返回 `unloaded`。
- 恢复测试前的 `agent.md`；测试前不存在该文件时，卸载后不应遗留人格内容。
- 清除 SoulAgent 的状态、备份和写入锁。

## 4. 补充任务示例

下面的请求可用于检查不同科研阶段的动态裁剪效果。

### 问题界定

```text
使用 $soulagent，启用何恺明。我要设计一个新的视觉表征学习方法，请先判断真正需要解决的瓶颈，并指出哪些常见做法只是默认习惯。
```

### 实验设计

```text
使用 $soulagent，启用何恺明。请为一个新的目标检测模块设计公平基线、消融实验、压力测试和失败条件。
```

### 数据与社会技术风险

```text
使用 $soulagent，启用徐丰力。我们准备使用城市移动数据预测用户行为，请检查数据偏差、隐私风险、系统机制和结论边界。
```

### 结果解释

```text
使用 $soulagent，启用徐丰力。模型总体指标提高了，但少数区域和稀有群体表现下降，请判断聚合指标掩盖了什么，并设计进一步分析。
```

## 5. 建议记录内容

每次测试建议记录：

| 项目 | 记录内容 |
|---|---|
| 测试请求 | 输入给 Codex 的完整文本 |
| 实际调用 | 是否显示 `Skill(soulagent)` |
| 返回状态 | `refreshed`、`unchanged_task`、`no_scientific_task` 或 `unloaded` |
| 科学家 | `kaiming-he` 或 `fengli-xu` |
| 任务变化 | 阶段、目标或资源约束是否被识别 |
| 文件变化 | `agent.md` 和 `.soulagent/state.json` 是否符合预期 |
| 回答变化 | 判断顺序、证据要求、实验设计和失败分析是否发生变化 |
| 异常信息 | 完整错误文本及必要截图 |

## 6. 常见问题

### Codex 没有显示 SoulAgent

确认目录为 `<CODEX_HOME>/skills/soulagent/SKILL.md`，然后重新启动 Codex。

### Codex 调用了独立科学家 Skill

测试请求中显式写 `使用 $soulagent`，不要只写“启用何恺明”。

### 找不到科学家 KG

确认 Codex 的项目根目录下直接存在：

```text
scientist-kg/kaiming-he/
scientist-kg/fengli-xu/
```

### 提示缺少解码 API 配置

确认启动 Codex 的同一个终端中已经设置 `SOULAGENT_API_KEY` 和 `SOULAGENT_MODEL`。使用非 OpenAI 官方接口时，同时设置 `SOULAGENT_BASE_URL`。

### 返回 `no_scientific_task`

这表示任务感知器没有把请求识别为科研任务。测试动态人格时，应在请求中明确实验、假设、数据、模型、证据、消融、验证或失败分析等科研语境。
