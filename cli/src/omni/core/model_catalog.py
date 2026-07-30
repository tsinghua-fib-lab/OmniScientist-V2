"""What each model can actually take in and give back, in one table.

Two limits decide whether a turn works, and both are properties of the model
rather than of OmniScientist: how much conversation it will accept, and how long
a single response may be. They are kept together because they are read together
and go stale together — a catalog that knew a model's context window while a
separate constant guessed its output cap is how ``deepseek-v4-pro`` ended up
holding a million tokens of context and being asked for 4096 tokens of output.

The output cap is the one that bites first and the one that is easiest to get
wrong, because exceeding it is not an error. The provider simply stops
mid-sentence and sets ``finish_reason: "length"``; if the response was writing a
tool call, its arguments arrive unterminated and unusable. Sizing this from the
model instead of from a single global default is what keeps a long write from
being cut in half.

The input figure is a budget, not a specification. ``omni.config.settings``
turns it into the ceiling a growing transcript is compacted against, so what
belongs here is the largest input the provider will actually accept — which is
not always the number it advertises as a context window.

Every row records where its numbers came from. Three rounds of corrections were
each found by somebody happening to read one row, and a hand-maintained table
drifts whether or not anyone is reading it; naming the reference per row is what
turns re-checking from research into a lookup, and naming the rows that have no
reference is what stops a guess from being mistaken for a finding.

Pure stdlib, no omni imports: both ``omni.config`` and ``omni.core.llm`` read it.
"""

from __future__ import annotations

from dataclasses import dataclass

# One call is allowed to be long, but not unbounded. A model that advertises a
# 384k output cap will happily spend it, and a single runaway response would eat
# a whole run's token budget before any loop bound could react. This ceiling is
# far above any legitimate tool call (roughly 100k characters of prose, enough
# for a full paper in one write) while keeping one response affordable.
REQUEST_OUTPUT_CEILING = 32_768
# Modern chat models all accept at least this much output; sizing an unknown
# model below it is what causes silent truncation.
_DEFAULT_MAX_OUTPUT_TOKENS = 8_192
_DEFAULT_MAX_INPUT_TOKENS = 32_768


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """What one request may carry in each direction, in tokens.

    ``max_input_tokens`` is the largest prompt the provider will accept, which
    is the quantity compaction measures a transcript against — deliberately not
    the advertised context window, because for some models the two differ. A
    vendor that sells a combined window and separately refuses input above a
    lower figure (OpenAI's GPT-5 line: a 400k window, input rejected past 272k)
    contributes the lower figure here, since the higher one would let a
    transcript grow into a request that cannot be sent at all.

    Where a window is genuinely shared, the response still has to fit beside the
    prompt inside it. That reservation depends on the output we ask for rather
    than on the model, so it belongs to whoever sizes the request, not here.
    """

    max_input_tokens: int
    max_output_tokens: int


# When the whole table was last read against the sources below. One date for the
# sweep rather than one per row: the rows do not go stale independently, vendors
# do, and a column of dates would be forty-odd numbers nobody updates honestly.
LAST_SWEPT = "2026-08-07"

# Three vendors serve their reference as markdown, which is what lets a sweep
# re-read these rows unattended: OpenAI one file per model id at
# developers.openai.com/api/docs/models/<id>.md, Moonshot and Zhipu one per page
# under platform.kimi.ai/docs/ and docs.z.ai/, both indexed by their llms.txt.
_OPENAI = "openai/model-reference"
# platform.claude.com/docs/en/about-claude/models/overview. The Models API
# returns the same max_input_tokens / max_tokens behind a key.
_ANTHROPIC = "anthropic/model-overview"
# ai.google.dev/gemini-api/docs/models, restated per generation in the Firebase
# AI Logic table; models.list returns inputTokenLimit / outputTokenLimit.
_GOOGLE = "google/gemini-models"
# api-docs.deepseek.com, models and pricing.
_DEEPSEEK = "deepseek/models-and-pricing"
# platform.kimi.ai/docs/models and /docs/api/models-overview for the windows,
# /docs/api/chat for the output parameter's range.
_MOONSHOT = "moonshot/model-list"
# docs.z.ai/guides/llm/<model>, one page per release, each stating a context
# length and a maximum output.
_ZHIPU = "zhipu/model-guides"
# Not a vendor model. omni-mock is ours and its limits are a fixture, so there
# is nothing to verify and nothing that can drift.
_INTERNAL = "omni/mock"
# No first-party figure, for one of two reasons. Alibaba states a context
# length only in prose and keeps the per-model table behind interactive tabs a
# fetch cannot reach, and publishes no output cap at all. The rest are open
# weights, where the question is malformed: the limits belong to whoever is
# serving the model, not to the model, so no vendor page could settle them.
#
# These numbers are therefore deliberate under-estimates and not findings —
# safe in the window, but capable of truncating a long write if the true output
# cap is higher. Correcting one needs a source, not a guess.
_UNVERIFIED = "unverified"

# Matched by model-name substring, most specific first. Erring low on the input
# budget is cheap — compaction simply fires earlier than it had to — while
# erring high lets a transcript grow past what the provider accepts, so an
# approximation here should round down. The output cap should not exceed what
# the provider accepts either, since some reject an oversized request outright
# rather than clamping it.
_CATALOG: tuple[tuple[str, ModelLimits, str], ...] = (
    # DeepSeek V4 (2026-04): 1M context, 384k output on both tiers. The legacy
    # deepseek-chat / deepseek-reasoner ids alias to v4-flash and were retired
    # 2026-07-24, so they are listed with the same limits rather than V3's 8k.
    ("deepseek-v4", ModelLimits(1_000_000, 384_000), _DEEPSEEK),
    ("deepseek-chat", ModelLimits(1_000_000, 384_000), _DEEPSEEK),
    ("deepseek-reasoner", ModelLimits(1_000_000, 384_000), _DEEPSEEK),
    ("deepseek", ModelLimits(128_000, 8_192), _UNVERIFIED),
    # The GPT-5 generations are the reason this column holds an input cap rather
    # than a context window: OpenAI publishes both, and enforces the smaller one
    # on its own. Sol, Terra and Luna share 1,050,000/922,000; gpt-5, -mini and
    # -nano share 400,000/272,000. Recording the advertised window instead would
    # put the expensive compaction tier at 360k of prompt against a hard 272k
    # limit — a request refused before compaction could help.
    # "gpt-5" is a prefix of "gpt-5.6", so 5.6 has to be matched first. Neither
    # needle collides with the gpt-4 / gpt-3.5 ones.
    ("gpt-5.6", ModelLimits(922_000, 128_000), _OPENAI),
    ("gpt-5", ModelLimits(272_000, 128_000), _OPENAI),
    # 4.1's window is really 1,047,576; rounded down per the note above.
    ("gpt-4.1", ModelLimits(1_000_000, 32_768), _OPENAI),
    ("gpt-4o", ModelLimits(128_000, 16_384), _OPENAI),
    ("gpt-4-turbo", ModelLimits(128_000, 4_096), _OPENAI),
    # gpt-4's window and its output cap are both 8,192 — it is one of the few
    # models that may spend its whole context on the reply. The 4,096 here for
    # three releases was gpt-4-turbo's cap, applied to the wrong row.
    ("gpt-4", ModelLimits(8_192, 8_192), _OPENAI),
    ("gpt-3.5", ModelLimits(16_385, 4_096), _OPENAI),
    ("o1", ModelLimits(200_000, 100_000), _OPENAI),
    ("o3", ModelLimits(200_000, 100_000), _OPENAI),
    ("o4", ModelLimits(200_000, 100_000), _OPENAI),
    # Claude 3.5's 8k output needs a beta header; without it the provider caps
    # at 4k. 3.7's 64k likewise doubles behind a header. Both rows hold the
    # documented default ceiling, which is what an unheadered request gets.
    ("claude-3-5", ModelLimits(200_000, 8_192), _ANTHROPIC),
    ("claude-3.5", ModelLimits(200_000, 8_192), _ANTHROPIC),
    ("claude-3-7", ModelLimits(200_000, 64_000), _ANTHROPIC),
    ("claude-3.7", ModelLimits(200_000, 64_000), _ANTHROPIC),
    # Anthropic sizes by generation rather than by tier: everything from the 4.6
    # generation on carries a 1M window and 128k output, while 4.5 and earlier
    # stayed at 200k. So the needles are one per generation, not one per
    # release — a point release inside a named generation (``opus-5-1``) is
    # already covered, and only a new generation needs an entry.
    #
    # A generation nobody has named yet falls through to the tier needle, which
    # is deliberately the oldest generation's numbers. That is the safe
    # direction: an under-sized window compacts earlier than it had to, whereas
    # an over-sized one lets the transcript grow into a request the model
    # refuses. Being wrong about an unreleased model should cost money, not the
    # run.
    ("opus-4-5", ModelLimits(200_000, 64_000), _ANTHROPIC),
    ("opus-4.5", ModelLimits(200_000, 64_000), _ANTHROPIC),
    ("opus-4-6", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("opus-4-7", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("opus-4-8", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("opus-5", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("opus", ModelLimits(200_000, 32_000), _ANTHROPIC),
    ("sonnet-4-6", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("sonnet-5", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("sonnet", ModelLimits(200_000, 64_000), _ANTHROPIC),
    # Haiku 4.5 allows 64k output. The 3.5 generation's 8k cap still holds for
    # claude-3-5-haiku, which the claude-3-5 needle above catches first.
    ("haiku", ModelLimits(200_000, 64_000), _ANTHROPIC),
    # The Mythos-class tier above Opus; Mythos shares Fable's limits.
    ("fable", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("mythos", ModelLimits(1_000_000, 128_000), _ANTHROPIC),
    ("claude", ModelLimits(200_000, 8_192), _ANTHROPIC),
    ("qwen2.5", ModelLimits(131_072, 8_192), _UNVERIFIED),
    ("qwen-long", ModelLimits(1_000_000, 8_192), _UNVERIFIED),
    ("qwen", ModelLimits(32_768, 8_192), _UNVERIFIED),
    # 1.5 Pro is really 2M and 1.5 Flash 1,048,576; the smaller, rounded-down
    # figure covers both.
    ("gemini-1.5", ModelLimits(1_000_000, 8_192), _GOOGLE),
    # Google widened output at 2.5 and kept it there: 2.5 and 3.x allow 64k,
    # while 2.0 is still an 8k model. So the needles run per generation, and
    # anything Google has not shipped yet lands on the trailing needle's 8k —
    # the direction that costs a second call rather than a refused request.
    ("gemini-2.5", ModelLimits(1_000_000, 65_536), _GOOGLE),
    ("gemini-2", ModelLimits(1_000_000, 8_192), _GOOGLE),
    # The image-generation variants of the 3.x line hold 64k–128k rather than
    # 1M and are over-sized by this needle. They are not chat models and cannot
    # drive a turn; anyone running one has to pin the window in config.
    ("gemini-3", ModelLimits(1_000_000, 65_536), _GOOGLE),
    ("gemini", ModelLimits(1_000_000, 8_192), _GOOGLE),
    # Zhipu publishes a context length and a maximum output per release, and
    # the two move independently: 4.5 is 128k/96k, 4.6 through 5.1 are all
    # 200k/128k. GLM-5.2 is the one row where the vendor and its resellers
    # disagree — Zhipu says 1M, Alibaba's hosted copy says 198k — so it is left
    # on the 200k needle rather than given one of its own, which is right for
    # the reseller and merely early-compacting for the vendor.
    ("glm-4.5", ModelLimits(128_000, 96_000), _ZHIPU),
    ("glm-4.6", ModelLimits(200_000, 128_000), _ZHIPU),
    ("glm-4.7", ModelLimits(200_000, 128_000), _ZHIPU),
    ("glm-5", ModelLimits(200_000, 128_000), _ZHIPU),
    ("glm", ModelLimits(128_000, 16_000), _ZHIPU),
    # Moonshot has no output cap of its own: max_completion_tokens may be set
    # anywhere up to the window, and the API refuses the call when input plus
    # that figure exceeds it. So the output column is the window, and the
    # reservation between them is the caller's, as it is for gpt-4.
    #
    # The moonshot-v1 line sizes purely by suffix, and the bare needle carrying
    # 128k meant moonshot-v1-8k was sized at sixteen times what it holds — the
    # direction that loses the run. The trailing needle is now the 8k floor.
    ("moonshot-v1-128k", ModelLimits(128_000, 128_000), _MOONSHOT),
    ("moonshot-v1-32k", ModelLimits(32_768, 32_768), _MOONSHOT),
    ("moonshot", ModelLimits(8_192, 8_192), _MOONSHOT),
    ("kimi-k3", ModelLimits(1_000_000, 1_000_000), _MOONSHOT),
    ("kimi", ModelLimits(262_144, 262_144), _MOONSHOT),
    ("llama-3", ModelLimits(128_000, 8_192), _UNVERIFIED),
    ("llama", ModelLimits(8_192, 4_096), _UNVERIFIED),
    ("mixtral", ModelLimits(32_768, 8_192), _UNVERIFIED),
    ("mistral", ModelLimits(32_768, 8_192), _UNVERIFIED),
    ("omni-mock", ModelLimits(32_768, 8_192), _INTERNAL),
)

_FALLBACK = ModelLimits(_DEFAULT_MAX_INPUT_TOKENS, _DEFAULT_MAX_OUTPUT_TOKENS)


def _catalog_entry(model: str) -> tuple[str, ModelLimits, str] | None:
    """Return the first catalog row whose needle appears in ``model``."""
    name = (model or "").strip().lower()
    for needle, limits, source in _CATALOG:
        if needle in name:
            return needle, limits, source
    return None


def limits_for(model: str) -> ModelLimits:
    """Catalog entry for ``model``, or a conservative fallback for an unknown one."""
    entry = _catalog_entry(model)
    return entry[1] if entry else _FALLBACK


def source_for(model: str) -> str:
    """Provenance tag for ``model``, or ``unverified`` when the name is unknown."""
    entry = _catalog_entry(model)
    return entry[2] if entry else _UNVERIFIED


def max_input_tokens_for(model: str) -> int:
    """Prompt tokens ``model`` accepts — the ceiling a transcript is kept under.

    Named for what it returns rather than for the number a vendor advertises.
    They coincide for most models and diverge exactly where it matters: an
    accessor called ``context_window_for`` returning the GPT-5 line's 272k input
    cap reads like a bug against a published 400k window, and the obvious repair
    is to restore the advertised figure — which puts the expensive compaction
    tier at 360k of prompt against a hard limit that refuses it.
    """
    return limits_for(model).max_input_tokens


def max_output_tokens_for(model: str) -> int:
    """Output tokens to request for ``model``: what it allows, within our ceiling."""
    return min(limits_for(model).max_output_tokens, REQUEST_OUTPUT_CEILING)


__all__ = [
    "LAST_SWEPT",
    "REQUEST_OUTPUT_CEILING",
    "ModelLimits",
    "limits_for",
    "max_input_tokens_for",
    "max_output_tokens_for",
    "source_for",
]
