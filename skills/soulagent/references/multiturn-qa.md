# SoulAgent 多轮 QA 验收设计

这套 QA 把一次 SoulAgent 使用过程视为一个连续会话。每一轮都检查返回状态、
持久化任务框架、模型调用次数、人格造口和下一轮 Host 可见的人格，而不只检查回答文本。

## 会话前置条件

- 项目同时提供 `kaiming-he` 和 `fengli-xu` 两个有效 KG。
- 项目原有 `role.md`，用于验证卸载后的逐字恢复。
- Host 为 `omniscientist`。
- 测试模型采用本地确定性替身，但经过与真实 Host 相同的
  `task_sensor -> graph_pruner -> kg_decoder -> stoma_writer` 路径。

## 多轮问答与判定

| 轮次 | 用户问题 | 预期状态/回答 | 必须验证的副作用 |
|---|---|---|---|
| Q1 | “有哪些科学家？” | `listed`，列出两位科学家 | 不调用解码器，不生成造口 |
| Q2 | “用何恺明诊断 COCO 比 baseline 低 2 AP” | `refreshed`，阶段为 `failure_diagnosis` | `role.md` 与状态均提交；P04 原句逐字存在 |
| Q3 | “继续刚才同一个 COCO 掉点问题，先检查评估配置” | `unchanged_task` | 不重复调用解码器，不改写人格 |
| Q4 | “失败已经定位，接下来为同一模型设计 baseline、对照组和消融” | `refreshed`，阶段切到 `experiment_design` | 重新裁剪、解码和提交 |
| Q5 | “还是这个消融实验，对照组怎么设置更公平？” | `unchanged_task` | 改写式追问仍识别为同一任务 |
| Q6 | “现在只能单卡 GPU，且必须 8 小时内完成，其他目标不变” | `refreshed` | 保持 `experiment_design`；两个约束变为 `true` |
| Q7 | “继续刚才的消融实验，列出最小实验矩阵” | `unchanged_task` | 未明确解除的资源/时间约束继续生效 |
| Q8 | “现在 GPU 资源充足，也不着急，仍是同一个消融实验” | `refreshed` | 两个约束显式恢复为 `false`，阶段仍保持 |
| Q9 | “把 README 标题改短” | `no_scientific_task` | 人格文件和任务状态完全不变 |
| Q10 | “当前人格” | `active`，何恺明 | 不调用感知器和解码器 |
| Q11 | “切换为徐丰力，继续同一个消融实验” | `refreshed` | 完整替换人格；不得混入何恺明的 P04 |
| Q12 | “恢复你自己” | `unloaded` | 原始 `role.md` 逐字恢复，状态与备份清除 |
| Q13 | 再次“恢复你自己” | `already_inactive` | 幂等，不触碰原始文件 |

## 全程不变量

1. P04 的任何语气原句都不得出现在 task sensor 或 KG decoder 的模型请求中。
2. 每次成功刷新后，当前科学家的全部 P04 原句必须逐字出现在造口中。
3. `unchanged_task`、`no_scientific_task`、`status` 不得调用 KG decoder。
4. 同一科学家、阶段、目标和约束未发生实质变化时不得重写造口。
5. 明确的阶段变化、约束变化、科学家切换和 `force` 必须触发刷新。
6. 每轮只能写当前 Host 对应的一个造口；卸载必须恢复会话开始前的原文。
7. 中文、空格和括号路径必须通过直接参数数组及 UTF-8 子进程 I/O。

自动化实现位于 `tests/test_multiturn_qa.py`。
