from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


class DecoderError(RuntimeError):
    pass


DECODER_CONTRACT_VERSION = 3
# Skill-local decoder budget (portable runner + Omni adapter). Not the agent loop cap.
DECODER_MAX_TOKENS = 8192
DECODER_TIMEOUT_SECONDS = 120.0
COMPACT_RETRY_HINT = (
    "\n\n【压缩重试】上次输出被截断或结构不完整。"
    "必须包含全部五个三级标题。"
    "每个思维模式最多两句；证据每条一行「标题：观察」。"
    "不要重复核心原则，不要写传记。"
)


SYSTEM_PROMPT = """你是KG解码器。基于下列科学家的认知图谱子图，为当前科学任务生成一段人格描述文本。

规则：
1. 写成对 Coding Agent 的行为指导，不是科学家传记。
2. 不出现 L1/L2/L3、C01-C07、node_id 等任何内部编码。
3. 每条指导必须能从提供的思维模式或核心原则中找到出处。
4. 以当前任务为语境，针对用户此刻在做的具体科学任务说明该怎么做。
5. 写中文，自然、可读。
6. 核心原则与表达语气均由程序注入原文，不需要生成、归纳、改写或模仿。
7. 控制篇幅：每个思维模式 2-3 句；证据来源每条一行「论文标题：观察」。不要写传记，不要重复核心原则。

输出结构：
## 当前人格：{scientist_name}
### 表达语气
{由程序注入 P04 原句，此处无需生成}
### 核心原则
{由程序注入，此处无需重复}
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
    "### 表达语气",
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


def _section_body(text: str, heading: str) -> str:
    """Return one Markdown section body without consuming the next heading."""
    start = text.find(heading)
    if start == -1:
        return ""
    body_start = start + len(heading)
    next_heading = text.find("\n### ", body_start)
    if next_heading == -1:
        return text[body_start:].strip()
    return text[body_start:next_heading].strip()


def _replace_section(text: str, heading: str, body: str) -> str:
    """Replace one generated section with canonical KG text."""
    start = text.find(heading)
    if start == -1:
        raise DecoderError("解码结果缺少章节：" + heading)
    body_start = start + len(heading)
    next_heading = text.find("\n### ", body_start)
    if next_heading == -1:
        return text[:body_start] + "\n" + body
    return text[:body_start] + "\n" + body + "\n" + text[next_heading:]


def _call_openai(system_prompt: str, user_prompt: str) -> str:
    # Lazy import keeps this module independently loadable for contract tests.
    from llm_client import LLMClientError, complete_chat

    try:
        return complete_chat(
            system_prompt,
            user_prompt,
            max_tokens=DECODER_MAX_TOKENS,
            timeout_seconds=DECODER_TIMEOUT_SECONDS,
        )
    except LLMClientError as exc:
        raise DecoderError(f"LLM 解码 API 调用失败：{exc}") from exc


def validate_persona(text: str, subgraph: dict[str, Any]) -> None:
    missing_headings = [heading for heading in REQUIRED_HEADINGS if heading not in text]
    if missing_headings:
        raise DecoderError("解码结果缺少章节：" + ", ".join(missing_headings))
    forbidden = FORBIDDEN_PATTERN.search(text)
    if forbidden:
        raise DecoderError(f"解码结果泄漏内部编码：{forbidden.group(0)}")
    tone_body = _section_body(text, "### 表达语气")
    missing_tone = [
        value
        for value in subgraph["philosophy_kernel"]["tone_exemplars"]
        if value not in tone_body
    ]
    if missing_tone:
        raise DecoderError("解码结果没有逐字保留全部语气原句")
    principles_body = _section_body(text, "### 核心原则")
    # Use prefix matching (first 150 chars) for stance verification — some LLMs
    # (e.g., DeepSeek) may slightly reformat while preserving substance.
    principles_norm = " ".join(principles_body.split())
    missing_stances = [
        str(stance["question_label"])
        for stance in subgraph["philosophy_kernel"]["stances"]
        if " ".join(str(stance["stance"]).split())[:150] not in principles_norm
    ]
    if missing_stances:
        raise DecoderError("解码结果没有逐字保留核心原则：" + ", ".join(missing_stances))


def _inject_canonical_sections(text: str, subgraph: dict[str, Any]) -> str:
    """Replace model-written P01-P04 sections with verbatim KG text."""
    tone_text = "\n\n".join(
        f"- {value}" for value in subgraph["philosophy_kernel"]["tone_exemplars"]
    )
    stances_text = "\n\n".join(
        f"{stance.get('question_label', '')}：{stance['stance']}"
        for stance in subgraph["philosophy_kernel"]["stances"]
    )
    text = _replace_section(text, "### 表达语气", tone_text)
    return _replace_section(text, "### 核心原则", stances_text)


def _finalize_persona(text: str, subgraph: dict[str, Any]) -> str:
    finalized = _inject_canonical_sections(text, subgraph)
    validate_persona(finalized, subgraph)
    return finalized


def decode_subgraph(
    subgraph: dict[str, Any],
    task_frame: dict[str, Any],
    completion_fn: Callable[[str, str], str] | None = None,
) -> str:
    scientist_name = str(subgraph["identity"]["scientist_name"])
    tone_exemplars = subgraph["philosophy_kernel"]["tone_exemplars"]
    if not 3 <= len(tone_exemplars) <= 5:
        raise DecoderError("人格内核缺少 3-5 条语气原句")
    system_prompt = SYSTEM_PROMPT.replace("{scientist_name}", scientist_name)
    user_prompt = build_decoder_input(subgraph, task_frame)
    complete = completion_fn or _call_openai
    first = complete(system_prompt, user_prompt).strip()
    try:
        return _finalize_persona(first, subgraph)
    except DecoderError as first_error:
        compact = complete(system_prompt, user_prompt + COMPACT_RETRY_HINT).strip()
        try:
            return _finalize_persona(compact, subgraph)
        except DecoderError as retry_error:
            raise DecoderError(
                "任务化人格生成两次均未得到完整结果"
                f"（首次：{first_error}；压缩重试：{retry_error}）"
            ) from retry_error
