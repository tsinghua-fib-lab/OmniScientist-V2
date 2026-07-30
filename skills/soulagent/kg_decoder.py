from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any


class DecoderError(RuntimeError):
    pass


SYSTEM_PROMPT = """你是KG解码器。基于下列科学家的认知图谱子图，为当前科学任务生成一段人格描述文本。

规则：
1. 写成对 Coding Agent 的行为指导，不是科学家传记。
2. 不出现 L1/L2/L3、C01-C07、node_id 等任何内部编码。
3. 每条指导必须能从提供的思维模式或核心原则中找到出处。
4. 以当前任务为语境，针对用户此刻在做的具体科学任务说明该怎么做。
5. 写中文，自然、可读。
6. 核心原则中的三段 stance 必须原样输出，不得压缩或改写。

**语气**：用下述原句的语气节奏来写。模仿句长、用词偏好、攻击性或谦逊程度：
{tone_exemplars}

不要刻意复刻措辞，但要让整体语气像同一个人。如果原句短而直接，你的输出也应该短而直接。
如果原句带有“we find... surprisingly simple”这类表达模式，可以在合适的地方使用类似节奏。

输出结构：
## 当前人格：{scientist_name}
### 核心原则
{三个 stance 原文}
### 当前任务中的思考方式
{从活跃思维模式展开，每个模式写 2-3 句可执行指导}
### 当前取舍
{若有张力消解，说明为什么当前选择一侧；没有则写“当前没有触发需要消解的取舍。”}
### 证据来源
{每条写：论文标题 + observation}
"""

FORBIDDEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:L[123]|C0[1-7]|node_id|l[123]_[A-Za-z0-9_]+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
REQUIRED_HEADINGS = (
    "### 核心原则",
    "### 当前任务中的思考方式",
    "### 当前取舍",
    "### 证据来源",
)


def build_decoder_input(subgraph: dict[str, Any], task_frame: dict[str, Any]) -> str:
    name = str(subgraph["identity"]["scientist_name"])
    principles = "\n\n".join(
        f"{stance.get('question_label', '')}：{stance['stance']}"
        for stance in subgraph["philosophy_kernel"]["stances"]
    )
    patterns = "\n".join(
        f"- {node['category_label']}：{node['description']}"
        for node in subgraph["active_l2"]
    )
    evidence = "\n".join(
        f"- 《{item.get('source_title', '未知来源')}》：{item.get('observation', '')}"
        for item in subgraph["l1_evidence"]
    )
    tensions = "\n".join(
        f"- 约束 {item.get('context', '')}：保留 {item.get('kept')}；"
        f"不采用 {item.get('dropped')}。原因：{item.get('reason', '')}"
        for item in subgraph["tension_resolved"]
    ) or "当前没有触发需要消解的取舍。"
    return f"""科学家：{name}

【核心原则】
{principles}

【当前活跃的思维模式】
{patterns}

【支持证据】
{evidence}

【当前任务】{task_frame['objective']}
【资源约束】{task_frame.get('constraints', {})}

【已完成的张力消解】
{tensions}
"""


def _call_openai(system_prompt: str, user_prompt: str) -> str:
    api_key = os.environ.get("SOULAGENT_API_KEY")
    model = os.environ.get("SOULAGENT_MODEL")
    base_url = os.environ.get("SOULAGENT_BASE_URL")
    missing = [
        name
        for name, value in (
            ("SOULAGENT_API_KEY", api_key),
            ("SOULAGENT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise DecoderError("缺少解码 API 配置：" + ", ".join(missing))
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise DecoderError("未安装 openai 包；请先运行 pip install openai") from exc

    try:
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=8192,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:
        raise DecoderError(f"LLM 解码 API 调用失败：{exc}") from exc
    content = response.choices[0].message.content if response.choices else None
    if not content or not content.strip():
        raise DecoderError("LLM 解码 API 返回空文本")
    return content.strip()


def validate_persona(text: str, subgraph: dict[str, Any]) -> None:
    missing_headings = [heading for heading in REQUIRED_HEADINGS if heading not in text]
    if missing_headings:
        raise DecoderError("解码结果缺少章节：" + ", ".join(missing_headings))
    forbidden = FORBIDDEN_PATTERN.search(text)
    if forbidden:
        raise DecoderError(f"解码结果泄漏内部编码：{forbidden.group(0)}")
    missing_stances = [
        str(stance["question_label"])
        for stance in subgraph["philosophy_kernel"]["stances"]
        if str(stance["stance"]) not in text
    ]
    if missing_stances:
        raise DecoderError(
            "解码结果没有原样保留核心原则：" + ", ".join(missing_stances)
        )


def decode_subgraph(
    subgraph: dict[str, Any],
    task_frame: dict[str, Any],
    completion_fn: Callable[[str, str], str] | None = None,
) -> str:
    scientist_name = str(subgraph["identity"]["scientist_name"])
    tone_exemplars = subgraph["philosophy_kernel"]["tone_exemplars"]
    if not 3 <= len(tone_exemplars) <= 5:
        raise DecoderError("人格内核缺少 3-5 条语气原句")
    tone_block = "\n".join(f"- {value}" for value in tone_exemplars)
    system_prompt = (
        SYSTEM_PROMPT.replace("{scientist_name}", scientist_name)
        .replace("{tone_exemplars}", tone_block)
    )
    user_prompt = build_decoder_input(subgraph, task_frame)
    text = (completion_fn or _call_openai)(system_prompt, user_prompt).strip()
    validate_persona(text, subgraph)
    return text
