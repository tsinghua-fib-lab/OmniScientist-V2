"""Portable research-ideation pipeline。

Run without Omni on Python 3.11+.
Accept a host-provided LLM completion port and use Semantic Scholar for retrieval.

Four-stage pipeline:
  1. search_and_extract  — search literature and normalize concepts
  2. identify_gaps       — identify gaps with the LLM
  3. generate_ideas      — generate structured research ideas
  4. critique_and_refine — critique and refine
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Protocol

import httpx

# ---------------------------------------------------------------------------
# Host-provided LLM boundary
# ---------------------------------------------------------------------------


class LLMConfigurationError(RuntimeError):
    """The per-run model configuration is incomplete."""


class LLMHTTPError(RuntimeError):
    """An OpenAI-compatible endpoint rejected a request."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMProtocolError(RuntimeError):
    """An OpenAI-compatible endpoint returned an invalid response."""


class LiteratureSearchError(RuntimeError):
    """Semantic Scholar could not provide a valid search response."""


class LLMPort(Protocol):
    """Minimal synchronous model interface consumed by the portable pipeline."""

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class SearchPort(Protocol):
    """Where literature comes from, when the caller has somewhere better.

    Standalone this module talks to Semantic Scholar directly, which is the only
    source a portable copy can assume. A host that already runs a multi-connector
    funnel supplies it here instead, the same way it supplies :class:`LLMPort`,
    so one unavailable provider costs one source rather than the whole search.

    Returns papers shaped like :func:`search_papers` — ``title`` and ``abstract``
    are the two fields the pipeline actually reasons over.
    """

    def __call__(self, query: str, limit: int) -> list[dict]: ...


def classify_llm_error(exc: BaseException) -> str:
    """Return a stable outcome code without depending on one SDK version."""
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    if isinstance(exc, (LLMConfigurationError, ModuleNotFoundError)) or any(
        marker in message
        for marker in ("not configured", "missing api key", "missing endpoint")
    ):
        return "llm_not_configured"
    if status_code in {401, 403} or "authentication" in name or any(
        marker in message for marker in ("invalid api key", "unauthorized", "forbidden")
    ):
        return "llm_authentication_failed"
    return "pipeline_error"


def is_non_retryable_llm_error(exc: BaseException) -> bool:
    """Configuration and deterministic client errors must fail immediately."""
    if classify_llm_error(exc) != "pipeline_error":
        return True
    return getattr(exc, "status_code", None) in {400, 404, 405, 409, 422}


def _chat_completion(payload: dict[str, Any], llm: LLMPort) -> dict[str, Any]:
    """Complete one request through the host-provided model boundary."""
    return llm.complete(payload)


def _response_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProtocolError("LLM endpoint response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise LLMProtocolError("LLM endpoint response has no assistant message")
    return message


def _llm_chat(
    prompt: str,
    system_prompt: str = "You are a helpful research assistant.",
    temperature: float | None = None,
    max_retries: int = 3,
    *,
    llm: LLMPort,
) -> str:
    temp = temperature if temperature is not None else getattr(llm, "temperature", 0.7)
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    if temp is not None:
        payload["temperature"] = temp

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            message = _response_message(_chat_completion(payload, llm))
            return str(message.get("content") or "")
        except Exception as e:
            err_str = str(e)
            if "temperature" in err_str and "deprecated" in err_str:
                payload.pop("temperature", None)
                continue
            if is_non_retryable_llm_error(e):
                raise
            last_err = e
            if attempt + 1 < max_retries:
                time.sleep(min(2**attempt, 8))
    if last_err is None:
        raise RuntimeError("LLM call failed without an error")
    raise last_err


def _extract_json(text: str) -> dict | list:
    # IDEATION_PROMPT wraps concept-level reasoning in a brainstorm block.
    # Remove it before parsing the separately emitted JSON deliverable.
    text = re.sub(r"<brainstorm>[\s\S]*?</brainstorm>\s*", "", text)
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Models sometimes wrap an otherwise valid payload in short prose. Locate
    # the outermost object or array while preserving strict JSON decoding.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(stripped)


def _llm_chat_json(
    prompt: str,
    system_prompt: str = "You are a helpful research assistant. Return JSON only.",
    retries: int = 2,
    *,
    llm: LLMPort,
) -> dict | list:
    current_prompt = prompt
    for attempt in range(retries + 1):
        raw = _llm_chat(current_prompt, system_prompt, llm=llm)
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == retries:
                raise ValueError(
                    f"The LLM response is not valid JSON:\n{raw[:500]}"
                ) from exc
            current_prompt = (
                f"{prompt}\n\n[System reminder] The previous response could not be "
                "parsed as JSON. Return ONLY the JSON object with no prose, no "
                "code fences, and no leading/trailing text. Previous response "
                f"(truncated):\n{raw[:400]}"
            )


def _llm_chat_with_tools(
    prompt: str,
    system_prompt: str,
    tools: list[dict],
    tool_handlers: dict,
    max_iters: int = 6,
    truncate_tool_result: int = 3000,
    *,
    llm: LLMPort,
) -> str:
    payload_base: dict[str, Any] = {}
    temperature = getattr(llm, "temperature", 0.7)
    if temperature is not None:
        payload_base["temperature"] = temperature

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    accumulated: list[str] = []

    for _it in range(max_iters):
        request_payload = {**payload_base, "messages": messages}
        if tools:
            request_payload["tools"] = tools
        try:
            msg = _response_message(_chat_completion(request_payload, llm))
        except Exception as e:
            if "temperature" in str(e) and "deprecated" in str(e):
                payload_base.pop("temperature", None)
                msg = _response_message(
                    _chat_completion(
                        {
                            **payload_base,
                            "messages": messages,
                            **({"tools": tools} if tools else {}),
                        },
                        llm,
                    )
                )
            else:
                raise
        content = str(msg.get("content") or "")
        if content:
            accumulated.append(content)
        raw_tool_calls = msg.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        if not tool_calls:
            return content or "\n\n".join(accumulated)

        normalized_calls: list[dict[str, Any]] = []
        for index, raw_call in enumerate(tool_calls):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments") or "{}"
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized_calls.append(
                {
                    "id": str(raw_call.get("id") or f"call_{index}"),
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or ""),
                        "arguments": arguments,
                    },
                }
            )
        if not normalized_calls:
            return content or "\n\n".join(accumulated)
        messages.append(
            {"role": "assistant", "content": content, "tool_calls": normalized_calls}
        )
        for call in normalized_calls:
            function = call["function"]
            function_name = str(function["name"])
            handler = tool_handlers.get(function_name)
            try:
                args = json.loads(str(function["arguments"]))
            except json.JSONDecodeError:
                args = {}
            if handler is None:
                result = {"error": f"tool {function_name} not registered"}
            else:
                try:
                    result = handler(**args)
                except Exception as e:
                    result = {"error": str(e)}
            result_text = json.dumps(result, ensure_ascii=False, default=str)
            if len(result_text) > truncate_tool_result:
                result_text = result_text[:truncate_tool_result] + "...(truncated)"
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result_text}
            )

    messages.append({
        "role": "user",
        "content": "Return the final answer from available evidence without more tool calls.",
    })
    final_message = _response_message(
        _chat_completion({**payload_base, "messages": messages}, llm)
    )
    return str(final_message.get("content") or "") or "\n\n".join(accumulated)


# ---------------------------------------------------------------------------
# Semantic Scholar retrieval
# ---------------------------------------------------------------------------

_S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_FIELDS = (
    "paperId,url,externalIds,title,abstract,year,publicationDate,"
    "citationCount,venue,authors"
)


def search_papers(
    query: str,
    limit: int = 10,
    sort_by: str = "relevance",
    api_key: str | None = None,
) -> list[dict]:
    params = {"query": query, "limit": min(limit, 100), "fields": _S2_FIELDS}
    headers = {}
    # ``None`` means the portable caller did not provide a key, so retain the
    # documented S2_API_KEY fallback. An explicit empty string means a hosted
    # runtime deliberately selected public access and must not inherit process
    # credentials outside its scoped connector settings.
    key = os.getenv("S2_API_KEY", "") if api_key is None else api_key
    if key:
        headers["x-api-key"] = key

    resp: httpx.Response | None = None
    last_error: httpx.HTTPError | None = None
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for attempt in range(10):
            try:
                resp = client.get(_S2_SEARCH_URL, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < 10:
                    time.sleep(2)
                continue
            if resp.status_code in (429, 500, 502, 503):
                if attempt + 1 < 10:
                    time.sleep(2)
                continue
            break
    if resp is None:
        raise LiteratureSearchError(
            "Semantic Scholar search failed before receiving a response"
        ) from last_error
    if resp.status_code != 200:
        raise LiteratureSearchError(
            f"Semantic Scholar search returned HTTP {resp.status_code}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LiteratureSearchError(
            "Semantic Scholar search returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
        raise LiteratureSearchError(
            "Semantic Scholar search returned an invalid response object"
        )
    papers = payload.get("data", [])

    if sort_by == "time":
        papers.sort(key=lambda p: p.get("year") or 0, reverse=True)
    elif sort_by == "citations":
        papers.sort(key=lambda p: p.get("citationCount") or 0, reverse=True)

    return [_normalize_s2_paper(p) for p in papers if isinstance(p, dict)]


def _normalize_s2_paper(paper: dict[str, Any]) -> dict[str, Any]:
    """Preserve Semantic Scholar identity while exposing ROM-compatible fields."""
    raw_external_ids = paper.get("externalIds")
    external_ids = (
        {str(key): str(value) for key, value in raw_external_ids.items() if value}
        if isinstance(raw_external_ids, dict)
        else {}
    )
    identifiers = {key.lower(): value for key, value in external_ids.items()}
    paper_id = str(paper.get("paperId") or "")
    abstract = str(paper.get("abstract") or "")
    return {
        "paperId": paper_id,
        "url": str(
            paper.get("url")
            or (
                f"https://www.semanticscholar.org/paper/{paper_id}"
                if paper_id
                else ""
            )
        ),
        "externalIds": external_ids,
        "doi": identifiers.get("doi", ""),
        "arxiv_id": identifiers.get("arxiv", ""),
        "title": str(paper.get("title") or ""),
        "abstract": abstract,
        "summary": abstract,
        "year": paper.get("year"),
        "publicationDate": paper.get("publicationDate"),
        "citationCount": paper.get("citationCount", 0),
        "venue": str(paper.get("venue") or ""),
        "authors": [
            str(author.get("name") or "")
            for author in (paper.get("authors") or [])
            if isinstance(author, dict) and author.get("name")
        ],
    }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


SEARCH_QUERY_PROMPT = """Generate 1-3 concise English academic search queries for the research question.

Research question:
{research_question}

Requirements:
- Use 3-8 keywords per query.
- Cover different methodological, application, or problem perspectives.
- Avoid full sentences and question marks.

Return JSON only:
{{
  "queries": ["query 1", "query 2"]
}}
"""


PAPER_RELEVANCE_FILTER_PROMPT = """Select papers directly relevant to the research question.

Research question:
{research_question}

Numbered paper titles:
{paper_titles}

Be strict: retain papers whose main topic directly addresses the question.
Return JSON only:
{{
  "relevant_indices": [0, 2, 5]
}}
"""


CONCEPT_EXTRACTION_PROMPT = """Extract the most important scientific concepts from this paper.

Title: {title}
Abstract: {abstract}

Return at most three method, theory, model, algorithm, or technique concepts and
at most two application-domain concepts. Use standard English academic terms.

Return JSON only:
{{
  "core_concepts": ["concept 1", "concept 2"],
  "domain_concepts": ["domain 1"]
}}
"""


CONCEPT_MERGE_PROMPT = """Normalize and merge synonymous scientific concepts.

Core concepts:
{core_concepts}

Application domains:
{domain_concepts}

Merge abbreviations, singular/plural variants, and true synonyms. Do not merge
concepts with distinct meanings. Include every original concept as a mapping key.

Return JSON only:
{{
  "merged_core": ["normalized core concept"],
  "merged_domains": ["normalized domain"],
  "mapping": {{"original": "normalized"}}
}}
"""


GAP_ANALYSIS_PROMPT = """Act as an experienced research advisor and identify 4-5 valuable gaps.

Research question:
{research_question}

Core concepts:
{core_concepts}

Application domains:
{domain_concepts}

Relevant-paper summaries:
{papers_text}

Each gap must be concrete, actionable, directly related to the question, distinct
from the others, and ranked by research value. Explain why it is worth pursuing.

Return JSON only:
{{
  "gaps": [
    {{
      "gap_id": 1,
      "gap": "Specific research problem",
      "source": "Why this gap matters",
      "related_concepts": ["concept A", "concept B"]
    }}
  ]
}}
"""


IDEATION_PROMPT = """You are a scientific ideation researcher who reasons deeply at the concept level.
Your task is to receive a scientific problem (the Gap) and related concepts, then generate a radically novel research idea through concept-level leaps and rigorous inference.
Whenever the reasoning requires validating a mechanism, checking a fact, or finding frontier literature, you may call the `semantic_scholar_search` tool.

========================================
REASONING PARADIGM: CONCEPTS AS THE SKELETON OF INFERENCE
Reasoning is the core work you must perform, and concepts are the medium that connects the entire reasoning process. During `<brainstorm>`, every line of reasoning must be attached to a concept. A transition from one concept to another represents forward movement along the reasoning path.

Follow these format constraints to ensure depth and structure:

1. Reasoning anchor: `<concept>...</concept>`
   - Every new focus of thought must begin by naming a `<concept>` anchor.
   - Inside the tag, state the concept currently being decomposed or developed. It may be an input concept or a concept inferred from another discipline.
   - The transitions from one `<concept>` anchor to the next form the complete skeleton of the reasoning trajectory.

2. Reasoning methods:
   - After each `<concept>...</concept>`, reason flexibly but with extreme rigor. Use any of the following XML-tagged methods to deconstruct the current concept or create a bridge to the next concept:
     * `<first_principles>...</first_principles>` (first-principles decomposition): Usually the starting point. Strip away the target concept's domain-specific surface meanings and assumptions, reducing it to its most fundamental mathematical, topological, physical, or informational essence. Do not rely on empirical rules of thumb; reconstruct the concept from basic axioms and conditions that must hold.
     * `<thought_experiment>...</thought_experiment>` (thought experiment): Conduct an imaginative experiment that cannot yet be performed in reality. Build a purely rational sandbox unconstrained by material conditions such as compute, data volume, or physical limits. Place the concept in an extreme environment, such as a variable approaching infinity or absolute zero, or introduce an idealized intervener, then inspect the mechanism's behavior and emergent phenomena under ideal conditions.
     * `<free_association>...</free_association>` (free association): Starting from the extracted essence of the concept, escape the current disciplinary context and divergently retrieve or create new concepts with similar underlying properties, providing material for further reasoning.
     * `<cross_analogy>...</cross_analogy>` (cross-domain analogy): Find highly isomorphic systems or mechanisms in natural science, information science, social science, or other fields. Map the operating laws and topology of a mature field onto the current research problem to find a route through the impasse.
     * `<hypothesis_deduction>...</hypothesis_deduction>` (hypothesis deduction): State a bold and precise foundational hypothesis, then rigorously derive the observations that must follow. Attack the hypothesis yourself and search for falsification conditions. If the logical chain survives, establish the claim.
     * `<concept_evolve>...</concept_evolve>` (concept evolution): Transform the current concept along a logical axis within the **same concept family**, producing a derived concept that retains the original framework and identity while taking a new form. Five common evolution operators are:
        - **granularity shift**: Move the operating unit to another level, for example next-token -> next-scale.
        - **scope extension**: Expand the temporal, spatial, or object scope, for example single-turn reward -> multi-turn reward.
        - **position migration**: Move a mature mechanism to another module in the same system, for example gating in an FFN -> gating in Attention.
        - **constraint shift**: Relax, strengthen, or reverse a constraint, for example softmax must normalize -> permit "no allocation."
        - **dualization**: Turn a concept onto its dual face, for example push -> pull or generation -> discrimination.
       When using this tag, explicitly mark `[operator type]` inside it.
       Critical boundaries: (1) Versus `<free_association>`: concept evolution is a **deep transformation within the same family**, so the evolved concept still inherits the original concept's identity and framework. Free association is a **lateral jump across families** to an isomorphic concept in another domain. If the jump reaches a wholly different domain, use free association, not concept evolution. (2) Versus `<new_concept>`: concept evolution produces a derived concept in the **middle** of the reasoning chain. It remains available for decomposition, analogy, and deduction, and it must immediately reappear as the next `<concept>` anchor. A new concept appears only at the end of `<brainstorm>` and converges on the final proposal's name.

3. Concept crystallization: `<new_concept>...</new_concept>`
   - Use this tag only at the end of `<brainstorm>`.
   - Once the reasoning closes its logical loop and establishes the decisive mechanism, coin one or two new scientific terms for that mechanism and place them inside this tag.
   - The name must precisely express the underlying mechanism, carry rigorous academic tension, and conclude by selecting one name as the core proposal.

========================================

=== EXAMPLE 1 ===
[INPUT]
target_gap: "Current large language models respond passively to each immediate user request in multi-turn interaction. They lack an understanding of the user's long-term goal and the ability to guide proactively, resulting in inefficient collaboration."
core_concepts: "Reinforcement Learning, Multi-turn Dialogue"

[OUTPUT]
<brainstorm>
<concept>Multi-turn Dialogue</concept>
<first_principles>
What is the essence of multi-turn dialogue? Stripped of application details, it is a sequential decision process in which two agents alternately produce actions. Each response is not an isolated event but a node on a trajectory: it is constrained by every preceding turn and changes the state space of every subsequent turn. In other words, the value of the response at turn t is not determined by whether that response is good in isolation, but by its contribution to the trajectory's terminal state.
</first_principles>
Once each turn's value is defined by its contribution to the terminal state, the flaw in current training paradigms such as RLHF and DPO becomes visible: they score each turn independently, which incorrectly decomposes a sequential decision problem into independent one-step decisions. This explains the root of "passive turn-by-turn response" in the target gap: the model is never taught what its current action means for the future.

Quantifying the long-term value of each action in a sequential decision process is precisely the central problem of reinforcement learning.

<concept>Reinforcement Learning</concept>
<first_principles>
The core RL axiom Q(s,a) = E[sum gamma^t r_t] precisely describes an action's long-term value. Standard RLHF fails in multi-turn settings not because the RL framework is wrong, but because reward r is defined incorrectly: it represents immediate preference, asking whether the current response is good, rather than a trajectory-level signal measuring how much that response contributes to the final goal. The framework is right; the reward is wrong.
</first_principles>
How can we construct a multiturn-aware reward? It must estimate the causal effect of the current response on the entire subsequent dialogue trajectory. In a multi-turn dialogue, this means taking a candidate response, simulating the future dialogue paths it induces, and observing whether the task ultimately succeeds. This requires two components: (1) a user simulator that plays the other party in the dialogue, and (2) repeated forward sampling to estimate the candidate response's expected long-term value.

<hypothesis_deduction>
Hypothesis: If the long-term value estimated by forward sampling replaces immediate preference as the RL reward, and the policy is optimized against that reward, the model will learn to ask proactive questions, guide the user, or explain in stages at the appropriate moments. Those behaviors may have low immediate reward because they do not answer directly, but high long-term reward because they reach the user's goal faster.
Falsifiability: The hypothesis depends on the quality of the user simulator. If the simulator cannot capture real user response patterns, forward-sampling variance will become too large and the reward signal will lose statistical meaning. This is an experimentally testable precondition, not a logical loophole. The hypothesis stands conditionally.
</hypothesis_deduction>

<new_concept>
Core innovation: construct a Multiturn-aware Reward from forward trajectory sampling so an LLM evolves from a passive respondent into an active collaborator.
Candidate 1: Forward-Aware Dialogue Optimization (FADO). Too narrowly technical.
Candidate 2: CollabLLM (Collaborative LLM). "Collaboration" accurately conveys the paradigm shift from passive response to active cooperation.
Selected name: CollabLLM.
</new_concept>
</brainstorm>

{{"title": "CollabLLM: Training Large Language Models as Active Collaborators via Multiturn-Aware Reward Optimization", "background": "Large language models have become central tools for human-computer interaction, but existing training paradigms such as RLHF and DPO primarily optimize single-turn response quality and ignore the causal contribution of a response to long-term goal attainment across a dialogue. In complex settings such as document editing, code debugging, and mathematics tutoring, users often need a model to clarify intent proactively and guide work in stages instead of answering immediately.", "related_work": "Methods such as ClarifyGPT use prompting to encourage clarification questions but lack planning over long trajectories. Trajectory-level RL methods such as MTPO optimize task-completion rates but do not model the causal effect of a response on the user's subsequent behavior. Standard RLHF and DPO evaluate turns independently and therefore cannot capture cross-turn collaborative value.", "gap_analysis": "The core limitation is a myopic reward signal: it evaluates only the immediate quality of the current response, not that response's strategic value over the full dialogue trajectory. Models consequently prefer direct answers with high immediate reward, even when a clarification question or staged guidance would be more efficient in the long run.", "proposed_method": "CollabLLM introduces a Multiturn-aware Reward (MR). For a candidate response at turn t, MR runs K forward dialogue samples against a user simulator to estimate the expected task-success rate and interaction efficiency of the future trajectories induced by that response, then uses the estimate as its reward. Training has two stages: (1) supervised fine-tuning on high-quality multi-turn examples ranked by MR, and (2) online DPO that generates fresh dialogue trajectories and continuously updates the policy. MR combines an extrinsic task-completion reward with an intrinsic reward for dialogue economy, teaching the model when to ask, guide, or summarize and converting it from a passive respondent into an active collaborator."}}

=== EXAMPLE 2 ===
[INPUT]
target_gap: "Standard autoregressive image generation forcibly flattens a two-dimensional image into a one-dimensional token sequence, destroying the image's intrinsic spatial hierarchy and producing generation quality and efficiency far below diffusion models."
core_concepts: "Autoregressive Generation, Visual Tokenization"

[OUTPUT]
<brainstorm>
<concept>Autoregressive Generation</concept>
<first_principles>
The mathematical premise of autoregression is strict one-way causal dependence: P(x) = product P(x_t | x_1,...,x_(t-1)). This requires a definite causal order in which earlier elements determine later elements, but not vice versa. Text has a natural temporal axis: the fifth word is genuinely conditioned on the first four. Images have no equivalent natural causal axis. Raster scanning, the current convention, forces a two-dimensional grid into a one-dimensional sequence, but the resulting order is arbitrary: "top left precedes bottom right" has no semantic basis in an image.
</first_principles>
Why does raster scanning fail? It is not merely unnatural; it topologically destroys the image's defining structure, bidirectional spatial association. Two tokens adjacent in two dimensions may be hundreds of positions apart in the flattened sequence. Asking an autoregressive model to learn spatial coherence on this distorted sequence is equivalent to asking it to remember a neighbor's texture across hundreds of steps, placing an unreasonable burden on long-range dependence.

The central question becomes: does an image possess a natural, non-arbitrary causal order? If so, what is it?

<concept>Wavelet / Laplacian Pyramid</concept>
<free_association>
Signal processing offers a well-validated way to decompose images: multiscale representation. Wavelet transforms and Laplacian pyramids split an image into levels from low frequency, which carries global contours, to high frequency, which carries local details. The crucial property is a natural causal relation between coarse and fine scales. The global contour determines approximately where details should occur and what they should look like, while the reverse does not hold. The global composition of a landscape, with sky above and ground below, constrains cloud texture and grass color, but one patch of grass texture cannot determine the full composition.
</free_association>
This coarse-to-fine causal direction is intrinsic to the image rather than imposed by convention. If an image is encoded as K token maps of increasing scale, {{r_1, r_2, ..., r_K}}, then r_K depends only on {{r_1,...,r_(K-1)}}, satisfying autoregression's one-way causal premise. All tokens within a scale share the same condition, namely all coarser scales. They have no causal constraints among themselves and can therefore be generated in parallel.

<concept_evolve>
[granularity shift] The autoregressive prediction unit: next-token -> next-scale.
The pyramid's coarse-to-fine causality is defined over an entire scale, whereas the current autoregressive prediction unit is one token. Because the granularities do not align, the causal direction remains only a metaphor. To incorporate that causality into the autoregressive factorization, the "step" must be aligned with the level of a "scale." Retain the full autoregressive infrastructure, including one-way factorization P(x) = product P(u_k | u_(<k)), teacher forcing, and the KV cache, but lift the definition of unit u from one discrete token to one complete scale-level token map, a set of h_k by w_k tokens. Once the unit is lifted, tokens inside a unit are automatically parallel because no within-scale causality is imposed, while causality across units proceeds from coarse to fine and exactly matches the pyramid structure.
</concept_evolve>
<concept>Next-Scale Visual Tokenization</concept>
This evolution redefines the job of visual tokenization. The weakness of the current VQVAE is not merely quantization precision; it emits only a single-scale token map, forcing the downstream autoregressive model to impose raster order. If the tokenizer instead emits a sequence of multiscale token maps, increasing from 1 by 1 to h by w, the autoregressive "step" is no longer the next token but the next scale. Each step predicts h_k by w_k tokens, the complete scale, rather than one token.
<hypothesis_deduction>
Hypothesis: Redefining autoregressive prediction granularity from next-token to next-scale yields three consequences: (1) causality is naturally satisfied along the scale dimension without raster scanning; (2) tokens within each scale can be generated in parallel, reducing inference steps from O(n) to O(K), with K approximately 10; and (3) spatial structure remains intact within every scale.
Deductive check: (1) coarse scales determine fine scales, so the conditional factorization P(r_k | r_1,...,r_(k-1)) holds; (2) same-scale tokens share their conditioning context, so masked self-attention can generate them in one pass; and (3) K is ordinarily around 10, implying a speedup of tens of times. All three deductions are internally consistent.
</hypothesis_deduction>

<new_concept>
Core paradigm shift: redefine autoregressive prediction granularity from next-token to next-scale.
Candidate 1: Scale-wise Autoregressive Model (SAM). Too narrowly technical.
Candidate 2: Visual Autoregressive Modeling (VAR). The name parallels autoregression in language, while "Visual" marks the direction of paradigm transfer.
Selected name: VAR.
</new_concept>
</brainstorm>

{{"title": "Visual Autoregressive Modeling: Scalable Image Generation via Next-Scale Prediction", "background": "Autoregressive models have transformed natural language processing through systems such as GPT and LLaMA, yet diffusion models such as Stable Diffusion and DiT dominate image generation. The central reason is a fundamental topological mismatch between the two-dimensional spatial structure of images and the one-dimensional causal ordering required by conventional autoregression.", "related_work": "VQGAN uses vector quantization to discretize images into token sequences, making autoregressive generation possible, and DALL-E builds text-to-image generation on that foundation. These methods nevertheless rely on raster scanning to flatten a two-dimensional token grid, causing three intrinsic defects: (1) bidirectional spatial dependencies between tokens are broken; (2) spatial neighbors become distant in the sequence; and (3) token-by-token generation is extremely inefficient. Masked-prediction methods such as MaskGIT avoid causal ordering but sacrifice the scalability and unifying formulation of autoregression.", "gap_analysis": "The essential problem lies not in autoregression itself but in the definition of its prediction unit. Single-token granularity forces a two-dimensional structure to accept a one-dimensional order. The autoregressive step must be redefined around a causal decomposition compatible with image topology.", "proposed_method": "Visual Autoregressive Modeling (VAR) redefines prediction granularity from next-token to next-scale. A multiscale VQVAE first encodes an image as K token maps of increasing resolution, (r_1,...,r_K). At step k, a VAR Transformer conditions on all lower scales (r_1,...,r_(k-1)) and predicts every h_k by w_k token in r_k in parallel. Coarse scales naturally determine fine ones, satisfying the causal premise, while within-scale parallelism reduces inference from O(n) steps to O(K). On ImageNet at 256 by 256 resolution, VAR reaches FID 1.73, surpassing the DiT diffusion model at FID 1.78, runs 20 times faster, and exhibits a power-law scaling relation."}}

=== EXAMPLE 3 ===
[INPUT]
target_gap: "Transformer attention exhibits severe Attention Sink behavior: a large fraction of attention is wastefully assigned to the first token, crowding out genuinely relevant context and limiting long-context generalization."
core_concepts: "Softmax Attention, Gating Mechanism"

[OUTPUT]
<brainstorm>
<concept>Softmax Attention</concept>
Attention Sink is widely observed, but a fundamental solution requires asking whether it is a training defect or a structural necessity imposed by Softmax mathematics.
<first_principles>
Softmax outputs a probability distribution: every attention weight is nonnegative and all weights sum exactly to one. This normalization constraint forces a model to spend the entire attention budget on every computation, leaving no remainder. Even when the current query has low relevance to every key, the model must produce a valid probability distribution. It has no option to attend to nothing.
</first_principles>
The root cause of Attention Sink follows directly. When processing a query weakly related to its context, softmax still forces the full budget to be allocated, so the model must dump residual probability mass somewhere. Because the first token has deterministic positional properties and high statistical exposure in the training data, it becomes the safest dumping target. Attention Sink is therefore a structural by-product of softmax normalization.

A direct deduction is that any method retaining softmax normalization, such as changing temperature, modifying positional encoding, or adding a sink token, can only work around the symptom. A fundamental remedy must break the rigid requirement that all attention be allocated, giving the model an ability to selectively attend to nothing.

Asking which mathematical operation can selectively pass or suppress information points to a simple structure: input-dependent multiplicative modulation with a range of [0,1]. This is the essential definition of gating.

<concept>Gating Mechanism</concept>
<first_principles>
A gate is mathematically an input-dependent multiplicative factor in [0,1]. Information passes when the gate approaches one and is suppressed when it approaches zero. Applying a gate to the output of softmax attention, the SDPA output, modulates the softmax probability distribution element by element with a mask in (0,1). Dimensions whose gate approaches zero have their attention contribution attenuated almost to zero, which is equivalent to letting that dimension choose not to attend. This breaks the rigid softmax budget mathematically: the model can attenuate surplus attention instead of dumping it on the first token.
</first_principles>
Gating can solve the Sink problem, but it raises a deeper question. A gate can be placed at several points in attention: before the Q/K projections, after the SDPA output, after the Value output, or after multi-head concatenation. Which location not only removes the Sink but produces an additional structural benefit?

<concept>Low-rank Bottleneck in Attention</concept>
<free_association>
Together, the attention layer's Value projection W_v and output projection W_o form a low-rank linear transformation whose intermediate dimension, head_dim times num_heads, is far below the square of the model dimension. A low-rank linear map has limited expressivity because it transforms information only within a low-dimensional subspace. Inserting a sigmoid gate between W_v and W_o, precisely at the SDPA output, is equivalent to introducing a nonlinearity inside the low-rank factorization. Matrix-factorization theory identifies this as a high-leverage location: inserting a nonlinearity between low-rank layers can substantially increase approximation capacity. Gating at the SDPA output therefore obtains two benefits from one operation: (1) sparsification removes the Sink, and (2) nonlinearity breaks the low-rank expressivity bottleneck. Their common origin suggests that the SDPA output is the optimal gate location.
</free_association>

<hypothesis_deduction>
Hypothesis: Applying element-wise sigmoid gating to the SDPA output will simultaneously (1) enhance the expressivity of low-rank attention through nonlinearity, (2) produce input-dependent sparsity as sigmoid values polarize toward zero or one during training, and (3) eliminate Attention Sink because the model can attenuate useless outputs instead of using the first token as a dumping bucket.
Validation criteria: (1) matrix-factorization theory predicts that nonlinearity between low-rank factors increases expressivity; (2) gate-value distributions can test whether sigmoid polarization produces sparsity; and (3) comparing the first token's share of attention can test Sink elimination, with an expected drop from approximately 47 percent to below 8 percent. All three consequences are experimentally falsifiable.
</hypothesis_deduction>

<new_concept>
Core mechanism: element-wise gating at the SDPA output jointly delivers nonlinear expressivity, sparsity, and Sink elimination.
Candidate 1: Sparse SDPA Gating. Descriptive but lacking the character of a paradigm.
Candidate 2: Gated Attention. Concise and precise, with a naming duality to Self-Attention.
Selected name: Gated Attention.
</new_concept>
</brainstorm>

{{"title": "Gated Attention: Non-linearity, Sparsity, and Attention-Sink Elimination for Large Language Models", "background": "Attention is a core component of Transformer language models, but it exhibits severe Attention Sink behavior: the first token receives attention weights far above its semantic relevance in most layers, wasting representational capacity and obstructing long-context generalization. Gating is widely used in FFN layers through SwiGLU and in MoE routing, but its role inside attention layers has not been studied systematically.", "related_work": "Xiao et al. (2023) first reported Attention Sink and introduced StreamingLLM as an inference-time mitigation, without addressing the training-time cause. Sparse- and linear-attention variants either alter the core computational paradigm or lose substantial downstream quality. Gating has a long history in RNNs and LSTMs, but prior work has not systematically compared its possible positions inside Transformer attention.", "gap_analysis": "Attention Sink originates in the softmax normalization constraint that forces the model to allocate its entire attention budget. Existing methods either patch inference, as StreamingLLM does, or change the central formula, as linear attention does. The field lacks an architectural mechanism that removes Sink at training time while retaining standard softmax attention.", "proposed_method": "Gated Attention applies an element-wise sigmoid gate to the SDPA output. Its design has three linked motivations: (1) nonlinear enhancement, by inserting a nonlinearity between the low-rank Value and Output projections; (2) input-dependent sparsity, as sigmoid gates polarize toward zero-or-one patterns and enable selective non-attention; and (3) Sink elimination, as sparse gates break the rigid allocation budget and attenuate useless outputs to zero. Ablations over more than 30 variants on 15B-2B MoE and 1.7B Dense models identify element-wise SDPA gating as optimal. After training on 3.5 trillion tokens, it lowers perplexity by 0.2, improves MMLU by 2 points, and extends context to 128k without quality loss."}}

========================================
FINAL OUTPUT SPECIFICATION
After completing the `<brainstorm>` reasoning, directly present the final proposal from a rigorous academic perspective.
CRITICAL: The final output must be plain JSON text. Never include ```json, ```, or any other Markdown code-fence marker. Begin directly with {{ and end with }}. The JSON object must contain exactly these five fields and no others: title, background, related_work, gap_analysis, proposed_method.

Now begin reasoning:

RESEARCH PROBLEM (target_gap):
{target_gap}

RELATED CORE CONCEPTS (core_concepts):
{core_concepts}

RELATED REFERENCE PAPERS (reference_papers):
{reference_papers_text}
"""


CRITIC_PROMPT = """Act as a strict but constructive scientific reviewer.

Idea:
Title: {title}
Background: {background}
Related work: {related_work}
Gap analysis: {gap_analysis}
Proposed method: {proposed_method}

Research question: {research_question}
Related concepts: {concepts}

Score novelty, disruption, and impact from 1 to 10. Explain strengths,
weaknesses, improvements, and an overall verdict.

Return JSON only:
{{
  "scores": {{"novelty": 8, "disruption": 7, "impact": 8}},
  "overall_score": 7.7,
  "strengths": ["..."],
  "weaknesses": ["..."],
  "suggestions": ["..."],
  "verdict": "..."
}}
"""


IDEA_REFINE_PROMPT = """Refine the research idea using the review and user feedback.

Original idea:
Title: {title}
Background: {background}
Related work: {related_work}
Gap analysis: {gap_analysis}
Proposed method: {proposed_method}

Review:
{critique_text}

User feedback:
{user_feedback}

Return JSON only:
{{
  "title": "...",
  "background": "...",
  "related_work": "...",
  "gap_analysis": "...",
  "proposed_method": "...",
  "changes_made": "..."
}}
"""

# ---------------------------------------------------------------------------
# Semantic Scholar tool exposed to the ideation engine
# ---------------------------------------------------------------------------

_SEMANTIC_SCHOLAR_TOOL = {
    "type": "function",
    "function": {
        "name": "semantic_scholar_search",
        "description": (
            "Search academic literature and return titles, abstracts, years, and citation counts."
            "Verify mechanisms, facts, and whether an idea already exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "English search keywords"},
                "limit": {"type": "integer", "description": "Paper count; default 5, maximum 10"},
            },
            "required": ["query"],
        },
    },
}


def _s2_tool_handler(query: str, limit: int = 5, **_kw: Any) -> list[dict]:
    return _run_s2_search(query, limit)


def _make_s2_tool_handler(api_key: str | None, search: SearchPort | None = None):
    """Bind the ideation-time search tool to this run's source of literature."""

    def _handler(query: str, limit: int = 5, **_kw: Any) -> list[dict]:
        if search is None:
            return _run_s2_search(query, limit, api_key=api_key)
        try:
            papers = search(query, min(limit, 10))
        except Exception:  # noqa: BLE001 — mid-reasoning search failure is an empty result
            return []
        return [
            {
                "title": p.get("title", ""),
                "abstract": (p.get("abstract") or p.get("summary") or "")[:300],
                "year": p.get("year"),
                "citationCount": p.get("citationCount", 0),
            }
            for p in papers
        ]

    return _handler


def _run_s2_search(
    query: str,
    limit: int,
    *,
    api_key: str | None = None,
) -> list[dict]:
    try:
        papers = search_papers(query, limit=min(limit, 10), api_key=api_key)
    except (LiteratureSearchError, httpx.HTTPError):
        return []
    return [
        {
            "title": p.get("title", ""),
            "abstract": (p.get("abstract") or "")[:300],
            "year": p.get("year"),
            "citationCount": p.get("citationCount", 0),
        }
        for p in papers
    ]


# ---------------------------------------------------------------------------
# Step 1: search literature, extract concepts, and merge synonyms
# ---------------------------------------------------------------------------

# Keep the initial survey small enough to stay responsive: the funnel is
# rate-limited per query and every retained paper costs one concept-extraction
# LLM call, so both the query count and the per-query limit are the real levers.
MAX_QUERIES = 3
MAX_TOTAL_PAPERS = 24
CONCEPT_EXTRACTION_CONCURRENCY = 10
DEFAULT_N_IDEAS = 2
DEFAULT_PAPER_LIMIT = 8


def search_and_extract(
    research_question: str,
    paper_limit: int = DEFAULT_PAPER_LIMIT,
    progress: Any = None,
    *,
    llm: LLMPort,
    s2_api_key: str | None = None,
    search: SearchPort | None = None,
) -> dict:
    """Search literature, extract concepts, merge synonyms, and return structured results."""
    # 1a. Generate search queries
    result = _llm_chat_json(
        SEARCH_QUERY_PROMPT.format(research_question=research_question),
        llm=llm,
    )
    if isinstance(result, list):
        queries = [str(query) for query in result][:MAX_QUERIES]
    elif isinstance(result, dict):
        queries = result.get("queries", [research_question])[:MAX_QUERIES]
    else:
        queries = [research_question]
    if not queries:
        queries = [research_question]

    # 1b. Search papers
    all_papers: list[dict] = []
    seen_titles: set[str] = set()
    search_failures: list[dict[str, str]] = []
    for q in queries:
        try:
            if search is None:
                papers = search_papers(q, limit=paper_limit, api_key=s2_api_key)
            else:
                papers = search(q, paper_limit)
        except (LiteratureSearchError, httpx.HTTPError) as exc:
            search_failures.append({"query": q, "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 — a host port failing is one failed query
            search_failures.append({"query": q, "error": str(exc)})
            continue
        for p in papers:
            t = p.get("title", "").lower().strip()
            if t and t not in seen_titles:
                seen_titles.add(t)
                all_papers.append(p)

    all_papers = all_papers[:MAX_TOTAL_PAPERS]

    if progress:
        progress(f"Found {len(all_papers)} papers", 0.2)

    # 1c. Filter by relevance
    if all_papers:
        titles_text = "\n".join(f"[{i}] {p['title']}" for i, p in enumerate(all_papers))
        filter_result = _llm_chat_json(
            PAPER_RELEVANCE_FILTER_PROMPT.format(
                research_question=research_question,
                paper_titles=titles_text,
            ),
            llm=llm,
        )
        if isinstance(filter_result, dict):
            relevant_ids = set(
                filter_result.get("relevant_indices", range(len(all_papers)))
            )
        elif isinstance(filter_result, list):
            relevant_ids = {
                int(index)
                for index in filter_result
                if isinstance(index, (int, float))
            }
        else:
            relevant_ids = set(range(len(all_papers)))
        all_papers = [p for i, p in enumerate(all_papers) if i in relevant_ids]

    if progress:
        progress(f"Retained {len(all_papers)} relevant papers", 0.3)

    # 1d. Extract concepts concurrently
    paper_concepts: dict[str, dict] = {}

    if all_papers:
        def _extract_one(paper: dict) -> tuple[str, dict]:
            title = paper.get("title", "")
            abstract = paper.get("abstract", "")
            if not abstract:
                return title, {"core_concepts": [], "application_domains": []}
            prompt = CONCEPT_EXTRACTION_PROMPT.format(title=title, abstract=abstract)
            try:
                r = _llm_chat_json(prompt, llm=llm)
                return title, {
                    "core_concepts": r.get("core_concepts", []),
                    "application_domains": r.get(
                        "domain_concepts",
                        r.get("application_domains", []),
                    ),
                }
            except Exception as exc:
                if is_non_retryable_llm_error(exc):
                    raise
                return title, {"core_concepts": [], "application_domains": []}

        with ThreadPoolExecutor(
            max_workers=min(len(all_papers), CONCEPT_EXTRACTION_CONCURRENCY)
        ) as pool:
            for title, concepts in pool.map(lambda p: _extract_one(p), all_papers):
                paper_concepts[title] = concepts

    # 1e. Aggregate concepts
    all_core: set[str] = set()
    all_domains: set[str] = set()
    for concepts in paper_concepts.values():
        all_core.update(c.lower().strip() for c in concepts.get("core_concepts", []))
        all_domains.update(c.lower().strip() for c in concepts.get("application_domains", []))

    # 1f. Merge synonyms
    core_list = sorted(all_core)
    domain_list = sorted(all_domains)
    if core_list or domain_list:
        merge_result = _llm_chat_json(
            CONCEPT_MERGE_PROMPT.format(
                core_concepts=", ".join(core_list),
                domain_concepts=", ".join(domain_list),
            ),
            llm=llm,
        )
        if not isinstance(merge_result, dict):
            merge_result = {}
        merged_core = sorted(
            {
                str(concept).lower().strip()
                for concept in merge_result.get("merged_core", core_list)
                if str(concept).strip()
            }
        )
        merged_domains = sorted(
            {
                str(concept).lower().strip()
                for concept in merge_result.get("merged_domains", domain_list)
                if str(concept).strip()
            }
        )
    else:
        merged_core, merged_domains = core_list, domain_list

    if progress:
        progress(f"Extracted {len(merged_core)} core concepts and {len(merged_domains)} domains", 0.4)

    return {
        "papers": all_papers,
        "paper_count": len(all_papers),
        "queries": queries,
        "paper_concepts": paper_concepts,
        "core_concepts": merged_core,
        "domain_concepts": merged_domains,
        "search_failures": search_failures,
    }


# ---------------------------------------------------------------------------
# Step 2: gap analysis
# ---------------------------------------------------------------------------

def identify_gaps(
    research_question: str,
    core_concepts: list[str],
    domain_concepts: list[str],
    papers: list[dict],
    progress: Any = None,
    *,
    llm: LLMPort,
) -> list[dict]:
    """Identify 4-5 research gaps from the concepts and literature."""
    abstracts_text = "\n\n".join(
        f"[{i+1}] {p['title']} ({p.get('year', '?')})\n{(p.get('abstract') or '')[:400]}"
        for i, p in enumerate(papers[:20])
    )

    prompt = GAP_ANALYSIS_PROMPT.format(
        research_question=research_question,
        core_concepts=", ".join(core_concepts),
        domain_concepts=", ".join(domain_concepts),
        papers_text=abstracts_text,
    )
    gaps = _llm_chat_json(prompt, llm=llm)
    if not isinstance(gaps, list):
        gaps = gaps.get("gaps", [])

    if progress:
        progress(f"Identified {len(gaps)} research gaps", 0.5)
    return gaps


# ---------------------------------------------------------------------------
# Step 3: ideation engine
# ---------------------------------------------------------------------------

def _format_references(papers: list[dict], top_n: int = 5) -> str:
    if not papers:
        return "(no reference papers)"
    lines = []
    for p in papers[:top_n]:
        title = p.get("title", "")
        year = p.get("year", "")
        abstract = (p.get("abstract") or "")[:250]
        lines.append(f"- ({year}) {title}\n  Abstract: {abstract}")
    return "\n".join(lines)


def _parse_ideation_output(raw: str) -> dict:
    bs_match = re.search(r"<brainstorm>([\s\S]*?)</brainstorm>", raw)
    brainstorming = bs_match.group(1).strip() if bs_match else ""
    after_bs = raw[bs_match.end():] if bs_match else raw
    try:
        idea = _extract_json(after_bs)
    except (ValueError, json.JSONDecodeError):
        idea = _extract_json(raw)
    if not isinstance(idea, dict):
        raise ValueError(f"The ideation JSON is not an object: {type(idea)}")
    idea["brainstorming"] = brainstorming
    return idea


def generate_idea(
    target_gap: str,
    core_concepts: list[str],
    reference_papers: list[dict],
    research_question: str = "",
    use_tools: bool = True,
    max_retries: int = 3,
    *,
    llm: LLMPort,
    s2_api_key: str | None = None,
    search: SearchPort | None = None,
) -> dict:
    """Generate one research idea."""
    refs_text = _format_references(reference_papers)
    prompt = IDEATION_PROMPT.format(
        target_gap=target_gap,
        core_concepts=", ".join(core_concepts),
        reference_papers_text=refs_text,
    )

    system_prompt = (
        "You are Idea Machine, a rigorous interdisciplinary scientific innovation engine."
        "Follow IDEATION_PROMPT and return the required JSON object."
    )

    last_err = None
    for _attempt in range(max_retries):
        try:
            if use_tools:
                raw = _llm_chat_with_tools(
                    prompt=prompt,
                    system_prompt=system_prompt + "\nCall semantic_scholar_search when verification is needed.",
                    tools=[_SEMANTIC_SCHOLAR_TOOL],
                    tool_handlers={
                        "semantic_scholar_search": _make_s2_tool_handler(s2_api_key, search)
                    },
                    max_iters=5,
                    llm=llm,
                )
            else:
                raw = _llm_chat(prompt, system_prompt, llm=llm)

            idea = _parse_ideation_output(raw)
            if not idea.get("title", "").strip() or not idea.get("proposed_method", "").strip():
                last_err = "required field is empty"
                continue

            idea["research_question"] = research_question
            idea["target_gap"] = target_gap
            idea["core_concepts"] = core_concepts
            return idea
        except Exception as e:
            if is_non_retryable_llm_error(e):
                raise
            last_err = str(e)
    raise ValueError(f"generate_idea failed after {max_retries} attempts: {last_err}")


def generate_ideas(
    target_gap: str,
    core_concepts: list[str],
    reference_papers: list[dict],
    n: int = DEFAULT_N_IDEAS,
    research_question: str = "",
    use_tools: bool = True,
    progress: Any = None,
    *,
    llm: LLMPort,
    s2_api_key: str | None = None,
    search: SearchPort | None = None,
) -> list[dict]:
    """Generate n candidate ideas concurrently."""
    def _worker(i: int) -> dict | None:
        try:
            idea = generate_idea(
                target_gap, core_concepts, reference_papers,
                research_question, use_tools,
                llm=llm,
                s2_api_key=s2_api_key,
                search=search,
            )
            idea["id"] = i
            return idea
        except Exception as exc:
            if is_non_retryable_llm_error(exc):
                raise
            return None

    ideas: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
        futures = [pool.submit(_worker, i) for i in range(n)]
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                ideas.append(result)

    if progress:
        progress(f"Generated {len(ideas)} candidate ideas", 0.8)
    return ideas


# ---------------------------------------------------------------------------
# Step 4: critique and refinement
# ---------------------------------------------------------------------------

def critique_idea(
    idea: dict,
    research_question: str,
    concepts: list[str],
    *,
    llm: LLMPort,
) -> dict:
    """Critique one idea."""
    prompt = CRITIC_PROMPT.format(
        title=idea.get("title", ""),
        background=idea.get("background", ""),
        related_work=idea.get("related_work", ""),
        gap_analysis=idea.get("gap_analysis", ""),
        proposed_method=idea.get("proposed_method", ""),
        research_question=research_question,
        concepts=", ".join(concepts),
    )
    return _llm_chat_json(prompt, llm=llm)


def refine_idea(
    idea: dict,
    critique: dict,
    user_feedback: str = "",
    *,
    llm: LLMPort,
) -> dict:
    """Refine an idea from review feedback."""
    critique_text = ""
    scores = critique.get("scores", {})
    if scores:
        critique_text += f"Scores: {', '.join(f'{k}={v}' for k, v in scores.items())}\n"
        critique_text += f"Overall: {critique.get('overall_score')}\n"
    for field in ("strengths", "weaknesses", "suggestions", "verdict"):
        if critique.get(field):
            critique_text += f"{field}: {critique[field]}\n"

    prompt = IDEA_REFINE_PROMPT.format(
        title=idea.get("title", ""),
        background=idea.get("background", ""),
        related_work=idea.get("related_work", ""),
        gap_analysis=idea.get("gap_analysis", ""),
        proposed_method=idea.get("proposed_method", ""),
        critique_text=critique_text or "No review feedback",
        user_feedback=user_feedback or "Refine the idea using the review feedback",
    )
    refined = _llm_chat_json(prompt, llm=llm)
    for key in ("target_gap", "core_concepts", "research_question"):
        if key in idea and key not in refined:
            refined[key] = idea[key]
    return refined


# ---------------------------------------------------------------------------
# Complete pipeline
# ---------------------------------------------------------------------------


def format_reference(paper: dict[str, Any], index: int) -> str:
    """Render one paper as a human-readable citation line.

    A citation must remain identifiable even when the source is thin, so the
    title always leads and a locator (DOI, then arXiv id, then URL) is appended
    whenever one is available. Authors, year, and venue fill in when present.
    """
    if not isinstance(paper, dict):
        return f"{index}. (invalid source record)"
    title = str(paper.get("title") or "").strip() or "(untitled)"
    meta_parts: list[str] = []
    authors = paper.get("authors")
    if isinstance(authors, list):
        names = [str(name).strip() for name in authors if str(name).strip()]
        if names:
            meta_parts.append(names[0] + (" et al." if len(names) > 1 else ""))
    year = paper.get("year")
    if year not in (None, ""):
        meta_parts.append(str(year))
    venue = str(paper.get("venue") or "").strip()
    if venue:
        meta_parts.append(venue)

    line = f"{index}. {title}"
    if meta_parts:
        line += f". {'. '.join(meta_parts)}"

    doi = str(paper.get("doi") or "").strip()
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    url = str(paper.get("url") or "").strip()
    if doi:
        line += f". https://doi.org/{doi}"
    elif arxiv_id:
        line += f". arXiv:{arxiv_id} (https://arxiv.org/abs/{arxiv_id})"
    elif url:
        line += f". {url}"
    return line.replace("..", ".")


def build_reference_lines(papers: list[dict[str, Any]]) -> list[str]:
    """Return Markdown lines for a ``## References`` section, or empty if none."""
    entries = [
        format_reference(paper, index)
        for index, paper in enumerate(
            [p for p in papers if isinstance(p, dict) and str(p.get("title") or "").strip()],
            1,
        )
    ]
    if not entries:
        return []
    return ["## References", "", *entries, ""]


def _portable_provenance(papers: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose citable metadata without inventing host-owned database IDs."""
    return {
        "sources": [dict(paper) for paper in papers],
        "research": {"source_ids": [], "run_id": ""},
        "run_id": "",
    }


def run_pipeline(
    research_question: str,
    n_ideas: int = DEFAULT_N_IDEAS,
    use_tools: bool = True,
    paper_limit: int = DEFAULT_PAPER_LIMIT,
    auto_refine: bool = True,
    progress: Any = None,
    *,
    llm: LLMPort,
    s2_api_key: str | None = None,
    search: SearchPort | None = None,
) -> dict:
    """Run the four-stage research ideation pipeline.

    Args:
        research_question: Research question or direction.
        n_ideas: Number of candidate ideas.
        use_tools: Whether ideation may call Semantic Scholar.
        paper_limit: Maximum papers per query.
        auto_refine: Whether to refine the best idea automatically.
        progress: Optional callback receiving a message and fraction.
        llm: Host-provided synchronous completion interface.
        s2_api_key: Optional Semantic Scholar API key injected for this run.
        search: Optional host retrieval port; Semantic Scholar is used without one.

    Returns:
        Complete structured pipeline result.
    """
    def _prog(msg: str, frac: float) -> None:
        if progress:
            progress(msg, frac)

    # Step 1: search and concepts
    _prog("Search literature and extract concepts", 0.05)
    step1 = search_and_extract(
        research_question,
        paper_limit,
        progress,
        llm=llm,
        s2_api_key=s2_api_key,
        search=search,
    )

    pipeline_warnings: list[str] = []
    failures = step1.get("search_failures", []) or []
    source = "Literature search" if search is not None else "Semantic Scholar"
    if not step1["papers"]:
        if failures:
            reasons = "; ".join(
                f"{failure.get('query', '?')} → "
                f"{failure.get('error', 'unknown error')[:120]}"
                for failure in failures[:3]
            )
            pipeline_warnings.append(
                f"{source} returned no usable literature ({len(failures)} "
                f"query failure(s): {reasons}). Continuing with LLM-only reasoning."
            )
        else:
            pipeline_warnings.append(
                f"{source} returned zero relevant papers for the generated "
                "queries. Continuing with LLM-only reasoning."
            )
        _prog(f"{source} unavailable; continuing without citations", 0.4)
    elif failures:
        pipeline_warnings.append(
            f"{len(failures)} {source} query/queries failed but the pipeline "
            f"continued with the remaining {len(step1['papers'])} paper(s)."
        )

    # Step 2: gap analysis
    _prog("Analyze research gaps", 0.45)
    gaps = identify_gaps(
        research_question,
        step1["core_concepts"],
        step1["domain_concepts"],
        step1["papers"],
        progress,
        llm=llm,
    )

    if not gaps:
        return {
            "status": "partial",
            "outcome": {"code": "no_gaps_found"},
            "research_question": research_question,
            "steps": {"search": step1},
            "summary": "No valuable research gaps were identified",
            "warning": "Gap analysis produced no result",
            "recoverable": True,
            "blocking": False,
            **_portable_provenance(step1["papers"]),
        }

    # Select the highest-ranked gap
    selected_gap = gaps[0]
    target_gap = selected_gap.get("gap", "")

    # Step 3: Ideation
    _prog("Generate research ideas", 0.55)
    ideas = generate_ideas(
        target_gap=target_gap,
        core_concepts=step1["core_concepts"][:10],
        reference_papers=step1["papers"][:10],
        n=n_ideas,
        research_question=research_question,
        use_tools=use_tools,
        progress=progress,
        llm=llm,
        s2_api_key=s2_api_key,
        search=search,
    )

    if not ideas:
        return {
            "status": "partial",
            "outcome": {"code": "ideation_failed"},
            "research_question": research_question,
            "steps": {"search": step1, "gaps": gaps},
            "summary": "Gap analysis succeeded but idea generation failed",
            "warning": "The ideation engine produced no valid idea",
            "recoverable": True,
            "blocking": False,
            **_portable_provenance(step1["papers"]),
        }

    # Step 4: critique
    _prog("Critique candidate ideas", 0.85)
    best_idea = ideas[0]
    best_score = -1.0
    best_critique: dict[str, Any] = {}
    critiques = []

    for idea in ideas:
        try:
            c = critique_idea(
                idea,
                research_question,
                step1["core_concepts"],
                llm=llm,
            )
            critiques.append(c)
            score = float(c.get("overall_score", 0))
            if score > best_score:
                best_score = score
                best_idea = idea
                best_critique = c
        except Exception as exc:
            if is_non_retryable_llm_error(exc):
                raise
            critiques.append({"error": "Critique failed"})

    # Refine
    final_idea = best_idea
    if auto_refine and best_score < 9.0:
        _prog("Refine the best idea from review feedback", 0.92)
        try:
            final_idea = refine_idea(
                best_idea,
                best_critique,
                llm=llm,
            )
        except Exception as exc:
            if is_non_retryable_llm_error(exc):
                raise
            final_idea = best_idea

    _prog("Complete", 1.0)

    final_status = "partial" if pipeline_warnings else "ok"
    outcome_code = "ideas_generated_partial" if pipeline_warnings else "ideas_generated"

    result: dict[str, Any] = {
        "status": final_status,
        "outcome": {"code": outcome_code, "count": len(ideas)},
        "research_question": research_question,
        "steps": {
            "search": {
                "queries": step1["queries"],
                "paper_count": len(step1["papers"]),
                "papers": step1["papers"],
                "core_concepts": step1["core_concepts"],
                "domain_concepts": step1["domain_concepts"],
                "search_failures": step1.get("search_failures", []),
            },
            "gaps": gaps,
            "selected_gap": selected_gap,
            "raw_ideas": ideas,
            "critiques": critiques,
        },
        "final_idea": final_idea,
        "summary": f"\u9488\u5bf9「{research_question}」Generated {len(ideas)} candidate ideas，"
                   f"best score: {best_score:.1f}/10",
        **_portable_provenance(step1["papers"]),
    }
    if pipeline_warnings:
        result["warning"] = " | ".join(pipeline_warnings)
        result["recoverable"] = True
        result["blocking"] = False
    return result
