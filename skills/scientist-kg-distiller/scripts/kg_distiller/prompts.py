from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "1.3.1"

FACT_TYPES = {
    "explicit_judgment": "明确的方法偏好、验证标准或审美判断",
    "practical_habit": "由实验设计、消融结构或 baseline 选择暴露的操作习惯",
    "contrast_stance": "在与其他方法对比时暴露的评价标准",
    "failure_attribution": "对负面结果、限制或失败的归因与应对",
}

L2_CATEGORIES = {
    "C01": "怎样定义问题",
    "C02": "怎样选择方法",
    "C03": "怎样验证结论",
    "C04": "怎样解释结果",
    "C05": "怎样判断美丑",
    "C06": "怎样处理失败",
    "C07": "怎样产生想法",
}

L3_QUESTIONS = {
    "P01": {
        "label": "科学价值排序",
        "from": ["C02", "C05"],
        "prompt": "用 Kuhn 的原词：准确性、一致性、范围、简单性、丰产性；逐项解释此人的具体含义和相对优先级。",
        "explanation_focus": (
            "解释发生价值冲突时实际保留什么、牺牲什么，以及这种取舍由哪些"
            "方法选择、审美判断和论文行为支撑。总说明只综合决定性取舍，"
            "不要重复 value_dimensions 中五项价值的逐项解释。"
        ),
    },
    "P02": {
        "label": "核心信念",
        "from": ["C01", "C06"],
        "prompt": "说明他永不质疑什么、永不做什么，以及知识边界在哪里。",
        "explanation_focus": (
            "分别论证正面硬核、反面禁区和知识边界。只选择能区分这三部分的"
            "问题定义、失败处理方式和论文行为，不讨论身份履历或五价值排序。"
            "硬核必须是跨多项工作的抽象方法论信念；不得把恒等捷径、某种架构、"
            "组件或具体技巧写成他“永不质疑”的信念，具体方法只能作为例证。"
            "stance 只写抽象结论，不得放论文名、模型名、数据集名、组件名或"
            "“如/例如”式举例；所有具体例证只放在 explanation。"
        ),
    },
    "P03": {
        "label": "自我认知",
        "from": ["C03", "C07"],
        "prompt": "说明他认为自己是哪类科学家，以及与谁站在一起、反对什么。",
        "explanation_focus": (
            "把已确认的身份轨迹与验证方式、想法来源结合起来，解释其学术身份、"
            "所属研究传统和反对对象。必须明确连接至少一项已确认的教育、任职或"
            "研究领域事实，并重点使用怎样验证结论和怎样产生想法的证据。"
            "不要复述完整的方法清单或五价值排序。"
        ),
    },
}

SYSTEM_JSON = (
    "你是严谨的科学史与科学实践研究助理。只能依据提供的数据作答，"
    "不得补充外部事实。输出必须是单个合法 JSON 对象，不要 Markdown。"
)


def evidence_prompt(sources: list[dict[str, Any]]) -> str:
    return f"""
任务：从每份材料片段抽取具体、可回溯的科学行为或判断。不要总结跨材料模式，不要抽象人格。
事实类型：{json.dumps(FACT_TYPES, ensure_ascii=False)}

输出格式：
{{"cards":[{{"source_id":"原 source_id","excerpt":"逐字原文","location":{{"section":"章节名",
"start_char":0,"end_char":10}},"observation":"一句中文观察","fact_type":"四类之一"}}]}}

要求：
1. excerpt 必须是对应 full_text 中连续、逐字一致的片段。
2. start_char/end_char 是当前片段 full_text 内的相对字符偏移；代码会复核并映射到原文。
3. 每张卡只表达一个事实；没有合格事实的来源可以不产出卡。
4. 不得把论文贡献摘要本身误当作作者的判断或习惯。
5. 重点检查方法选择理由、baseline/ablation/验证设计、对比评价、失败分析和 limitation。
6. 实验设计本身可以构成 practical_habit，但 observation 必须只描述片段直接展示的行为。

材料片段（每个 source 在本轮最多出现一个片段）：
{json.dumps(sources, ensure_ascii=False)}
""".strip()


def classification_prompt(cards: list[dict[str, Any]]) -> str:
    compact = [
        {
            "card_id": card["card_id"],
            "observation": card["observation"],
            "fact_type": card["fact_type"],
            "excerpt": card["excerpt"],
        }
        for card in cards
    ]
    return f"""
把每张证据卡分到且仅分到一个最匹配的 L2 类别。
类别：{json.dumps(L2_CATEGORIES, ensure_ascii=False)}
输出：{{"assignments":[{{"card_id":"...","category":"C01"}}]}}
每个输入 card_id 必须出现恰好一次；category 只能是 C01-C07。
证据卡：{json.dumps(compact, ensure_ascii=False)}
""".strip()


def induction_prompt(category: str, cards: list[dict[str, Any]]) -> str:
    return f"""
类别 {category}（{L2_CATEGORIES[category]}）。
仅依据下列证据，用 1-2 段中文描述这位科学家在该类别中的具体思维模式。
不得写通用科研建议，不得声称证据没有展示的稳定偏好。
同时给出会激活该模式的任务场景和不应由该模式主导的场景。
description、trigger_contexts 和 contraindicated_contexts 面向 Coding Agent，
不得出现 l1_、l2_、l3_ 等内部节点 ID；引用证据时用论文标题、实验行为或自然语言描述。
输出：
{{"description":"...","trigger_contexts":["snake_case"],"contraindicated_contexts":["snake_case"]}}
证据：{json.dumps(cards, ensure_ascii=False)}
""".strip()


def l3_prompt(
    question: str,
    definition: dict[str, Any],
    l2_nodes: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    profile: dict[str, Any],
) -> str:
    evidence = [
        {
            "card_id": card["card_id"],
            "observation": card["observation"],
            "excerpt": card["excerpt"],
            "fact_type": card["fact_type"],
        }
        for card in cards
    ]
    return f"""
依据全部七个“怎么做”描述和全部原始论文证据回答一个人格核心问题。
全部材料是检索范围，不是回答提纲。只选对当前问题有决定性作用的模式和证据。
问题 ID：{question}
问题定义：{json.dumps(definition, ensure_ascii=False)}
输出：
{{"stance":{{"question":"{question}","stance":"完整中文 prose","explanation":"只论证当前问题的决定性依据","relevant_L2":["C01"],"exemplar_L1":["l1_..."],"value_dimensions":[{{"name":"准确性","relative_priority":"中文相对优先级说明","explanation":"此人语料中准确性具体指什么"}}]}}}}
必须给出 explanation、至少一个 relevant_L2。若原始证据不少于 3 条，
exemplar_L1 必须选择 3-8 条能直接支撑主要判断的代表证据；证据不足 3 条时应全部列出。
relevant_L2 必须包含问题定义 from 中的核心类别，总数不得超过 4 个。
explanation 应为 1-3 段连贯中文，直接说明 stance 中最关键的判断为何成立；
不得按七个“怎么”逐项巡检，不得以“基于七个……”“根据七个……”开头，
不得把相同的七项摘要复用于不同人格核心。每个证据都应服务于当前问题。
“怎样……”只是思维模式名称，不是书名或材料来源，不得写成《怎样……》。
若问题是 P01，value_dimensions 必须且只能有五项，名称必须严格为：准确性、一致性、范围、简单性、丰产性；不得把公平对比、高效、产出数量等改写成新的价值名。公平对比和高效只能作为解释这些原词在此人实践中的含义。若不是 P01，value_dimensions 输出空数组。
丰产性只指产生新发现、新问题和后续研究方向的能力，绝不指论文、成果或产出的数量。
不要求五项形成总排序；relative_priority 可说明并列、条件性优先或证据不足。推断强度不得超过 L1/L2；证据不足时在 explanation 中明确边界。
stance、explanation、relative_priority 和 value_dimensions 的 explanation
面向 Coding Agent，严禁出现 L1、L2、L3、C01-C07 或 l1_、l2_、l3_
等内部层级名和节点 ID。必须直接写“原始论文证据”和七个“怎么”的中文名称；
需要解释依据时，使用论文标题、具体实验行为或自然语言描述。内部 ID 只能出现在
relevant_L2 和 exemplar_L1 结构化字段。
若问题是 P03，自我认知还必须结合身份档案说明“他是谁”、学习经历、
就职轨迹和研究领域。只能使用身份档案已有事实，缺失内容明确说未知，
不得按常识补全。
L2：{json.dumps(l2_nodes, ensure_ascii=False)}
L1：{json.dumps(evidence, ensure_ascii=False)}
身份档案：{json.dumps(profile, ensure_ascii=False)}
""".strip()


def tone_candidate_prompt(
    source: dict[str, Any], passage: str
) -> str:
    metadata = {
        "source_id": source["source_id"],
        "title": source["title"],
        "source_type": source["source_type"],
        "author_role": source["author_role"],
    }
    return f"""
任务：从给定论文 introduction 或 talk 转录中逐字提取语气候选。
不是生成任务。只能复制输入中连续出现的完整原句，一个字都不能改。
保留原文的空格、制表符和换行；JSON 字符串中的换行必须写成 `\\n`，
不得把换行折叠为空格。

选择第一人称或评论性表达，优先选择能体现句子节奏、用词偏好以及
攻击性或谦逊程度的句子。不要概括，不要转述，不要输出风格描述。
每段最多返回 5 条；没有合格原句时返回空数组。

输出：{{"tone_exemplars":["输入中逐字存在的原句"]}}
来源元数据：{json.dumps(metadata, ensure_ascii=False)}
可引用原文：
{passage}
""".strip()


def tone_selection_prompt(candidates: list[str]) -> str:
    return f"""
从候选原句中选出最终语气样例。只能逐字复制候选项，不得改写。
选择 3-5 条，覆盖不同的句长、用词偏好以及攻击性或谦逊程度。
不要概括，不要转述，不要输出风格描述。
输出：{{"tone_exemplars":["候选中的原句"]}}
候选：{json.dumps(candidates, ensure_ascii=False)}
""".strip()


def edge_prompt(edge_type: str, l2_nodes: list[dict[str, Any]]) -> str:
    definitions = {
        "reinforces": (
            "判断 reinforces：两个模式是否是同一认知姿态的不同侧面，"
            "激活任一方时另一方也应连带激活。它是无向语义，每对只列一次。"
            "必须通过“缺少另一方会不会让该科学家的姿态变得残缺或误导”测试；"
            "仅仅都使用实验、数据、比较或都追求准确，不构成强化边。"
        ),
        "enables": (
            "判断 enables：理解或使用 to 是否必须先理解 from。"
            "这是有方向的前提关系，不能把时间上相邻或经常配合误当成必要前提。"
            "逐条做反向检查：如果不理解 from 仍能完整理解 to，就不要输出。"
        ),
        "tension": (
            "判断 tension：两个模式是否会在明确的资源、时间、范围或任务约束下"
            "竞争，不能同时主导。既可以来自直接偏好冲突，也可以来自两种有价值"
            "实践争夺同一稀缺资源。必须给出 snake_case context；一般差异不算张力。"
            "reason 必须引用至少一侧的 trigger_contexts 或"
            "contraindicated_contexts，并说明另一侧如何在该语境中竞争；"
            "仅仅关注点不同或可能难以兼顾不算张力。"
        ),
    }
    output = {
        "reinforces": (
            '{"reinforces":[{"from":"l2_...","to":"l2_...",'
            '"reason":"引用两侧描述中可核验的共同认知姿态"}]}'
        ),
        "enables": (
            '{"enables":[{"from":"l2_...","to":"l2_...",'
            '"reason":"说明为什么理解 to 必须先理解 from"}]}'
        ),
        "tension": (
            '{"tension":[{"between":["l2_...","l2_..."],'
            '"context":"snake_case","reason":"说明该约束下如何竞争"}]}'
        ),
    }
    if edge_type not in definitions:
        raise ValueError(f"Unsupported edge type: {edge_type}")
    limits = {"reinforces": 6, "enables": 6, "tension": 3}
    return f"""
依据以下七个思维模式判断横向边。本轮只判断一种边：{edge_type}。
{definitions[edge_type]}

必须系统检查所有可能的节点对，而不是只返回最显眼的一两对。
description、trigger_contexts 和 contraindicated_contexts 都是判断依据。
返回全部有明确文本依据的边；没有依据的组合不要输出，也不得为了凑数造边。
若合格候选超过 {limits[edge_type]} 条，只保留最能影响图遍历和人格理解的
{limits[edge_type]} 条，避免把七个节点连成近似完全图。
每条 reason 必须具体说明两侧文本如何支持该关系，不能只重复边定义。
reason 面向最终用户，必须使用自然中文名称（例如“怎样选择方法”），
不得出现 C01-C07、L1/L2/L3 或 l1_/l2_/l3_ 等内部编号。
不得提及 trigger_contexts、contraindicated_contexts 等字段名，也不得复制
snake_case 场景值；必须把场景含义转述为自然中文。

输出单个 JSON 对象，且只能包含 {edge_type}：
{output[edge_type]}
from、to、between 是结构化字段，必须逐字使用输入中的 node_id（l2_...）；
只有 reason 使用自然中文名称。不得输出自环，不得使用列表外的节点。
这是“判断横向边”任务。
思维模式：{json.dumps(l2_nodes, ensure_ascii=False)}
""".strip()
