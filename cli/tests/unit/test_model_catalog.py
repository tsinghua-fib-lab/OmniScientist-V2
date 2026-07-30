"""Both ends of a request are sized from the model, not from one number for all.

Incident 599a725b ran ``deepseek-v4-pro`` — a model that accepts 384k output
tokens — and asked it for 4096, because that was ``ModelCfg.max_tokens``'s
default. Any tool call carrying a document was cut off mid-argument.

The other end of the table is a budget rather than a description: it becomes the
ceiling ``omni.config.settings`` compacts a transcript against, so a row holding
an advertised context window instead of the input the provider will accept sends
a request that is refused before compaction can help, and a row holding a fifth
of the real window pays for compaction four times over.

Three rounds of corrections all began with somebody reading a single row, so the
last tests here guard the table's provenance rather than its numbers: what a row
claims as its source, and which rows admit to having none.
"""

from __future__ import annotations

import pytest

from omni.config import load_settings
from omni.config import settings as omni_settings
from omni.config.settings import (
    ModelCfg,
    infer_max_input_tokens,
    resolve_max_output_tokens,
    session_compact_token_budget,
)
from omni.core import model_catalog
from omni.core.model_catalog import (
    _CATALOG,
    _FALLBACK,
    _UNVERIFIED,
    REQUEST_OUTPUT_CEILING,
    limits_for,
    max_input_tokens_for,
    max_output_tokens_for,
)

# OpenAI's model reference publishes a context window and, for the GPT-5
# generations only, a smaller maximum input it enforces separately.
_GPT5_MAX_INPUT_TOKENS = 272_000
_GPT5_6_MAX_INPUT_TOKENS = 922_000


def test_a_pinned_max_tokens_beats_the_catalog() -> None:
    """An owner who wrote a number keeps that number; the catalog only fills a gap."""
    assert resolve_max_output_tokens(ModelCfg(model="deepseek-v4-pro", max_tokens=4096)) == 4096


def test_an_unset_max_tokens_follows_the_model() -> None:
    unset = ModelCfg(model="deepseek-v4-pro")
    assert unset.max_tokens == 0
    assert resolve_max_output_tokens(unset) > 4096


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"])
def test_deepseek_v4_is_not_sized_like_its_predecessor(model: str) -> None:
    """V4 carries a 1M window and a 384k output cap; V3's 8k/64k numbers would
    truncate a long write and mis-scale every compaction threshold."""
    assert max_input_tokens_for(model) == 1_000_000
    assert limits_for(model).max_output_tokens == 384_000


def test_one_response_is_capped_below_what_a_run_can_afford() -> None:
    """A model may allow far more output than a single call should ever spend:
    384k output tokens is several times the whole run's token budget, so one
    runaway response would end the turn before any loop bound could react."""
    assert limits_for("deepseek-v4-pro").max_output_tokens > REQUEST_OUTPUT_CEILING
    assert max_output_tokens_for("deepseek-v4-pro") == REQUEST_OUTPUT_CEILING


def test_an_unknown_model_gets_room_for_more_than_a_short_reply() -> None:
    """The fallback is what an unrecognised model gets, so it has to be a size
    that does not silently truncate ordinary work."""
    assert max_output_tokens_for("some-model-we-have-never-seen") >= 8_192
    assert max_input_tokens_for("some-model-we-have-never-seen") > 0


def test_the_window_and_the_output_cap_come_from_the_same_table() -> None:
    """They were separate before, and only one of them was kept current."""
    assert infer_max_input_tokens(ModelCfg(model="gpt-4o")) == max_input_tokens_for("gpt-4o")


def test_nothing_here_is_named_for_a_number_it_does_not_return() -> None:
    """The table stores the largest prompt a provider accepts, not the window it
    advertises, and for the GPT-5 generations those differ by 128,000 tokens.

    An accessor named for the window while returning the input cap does not just
    read oddly, it invites its own reversal: the next person to compare 272,000
    against a published 400,000 window sees a stale row and corrects it upward,
    and the expensive compaction tier lands at 360,000 tokens of prompt against a
    limit that refuses the request. The rename is the guard, so the name is what
    this asserts.
    """
    misnamed = sorted(
        f"{module.__name__.rsplit('.', 1)[-1]}.{name}"
        for module in (model_catalog, omni_settings)
        for name, value in vars(module).items()
        if callable(value) and "context_window" in name
    )

    assert misnamed == []


def test_no_catalog_entry_promises_more_output_than_it_accepts_input() -> None:
    """Read from the table itself, not from a hand-picked sample: the invariant
    has to hold for the row somebody adds tomorrow, which is the only row a
    fixed list of names can never reach."""
    for needle, limits, _source in _CATALOG:
        assert limits.max_output_tokens <= limits.max_input_tokens, needle


@pytest.mark.parametrize(
    ("model", "max_input_tokens", "max_output_tokens"),
    [
        # Anthropic model overview, 2026-08: Haiku 4.5 is 200k/64k and Opus 4.5
        # is 200k/64k, not the 8k and 32k this table used to hand them.
        ("claude-haiku-4-5", 200_000, 64_000),
        ("claude-opus-4-5-20251101", 200_000, 64_000),
        # OpenAI model reference, 2026-08.
        ("gpt-5", _GPT5_MAX_INPUT_TOKENS, 128_000),
        ("gpt-5-mini", _GPT5_MAX_INPUT_TOKENS, 128_000),
        ("gpt-5-nano", _GPT5_MAX_INPUT_TOKENS, 128_000),
        ("gpt-5.6-sol", _GPT5_6_MAX_INPUT_TOKENS, 128_000),
        # Gemini API model reference, 2026-08: 2.5 Pro allows 64k output, and
        # the 3.x line kept that ceiling rather than returning to 2.0's 8k.
        ("gemini-2.5-pro", 1_000_000, 65_536),
        ("gemini-3.1-pro-preview", 1_000_000, 65_536),
        ("gemini-3.6-flash", 1_000_000, 65_536),
        # OpenAI model reference, 2026-08: gpt-4 may spend its whole 8k window
        # on the reply. The 4,096 it used to carry was gpt-4-turbo's cap.
        ("gpt-4", 8_192, 8_192),
        # Zhipu's per-release guides, 2026-08. Every GLM was sized at 8k of
        # output, from six times low on the oldest to sixteen times on GLM-5.
        ("glm-4.5", 128_000, 96_000),
        ("glm-4.6", 200_000, 128_000),
        ("glm-5", 200_000, 128_000),
        ("glm-5-turbo", 200_000, 128_000),
        # Moonshot's model list, 2026-08: K3 holds 1M and the K2.x line 256k,
        # against the 128k this table gave every one of them.
        ("kimi-k3", 1_000_000, 1_000_000),
        ("kimi-k2.6", 262_144, 262_144),
        ("kimi-k2.7-code", 262_144, 262_144),
    ],
)
def test_a_named_model_resolves_to_the_limits_its_vendor_publishes(
    model: str,
    max_input_tokens: int,
    max_output_tokens: int,
) -> None:
    """These are the rows a review found understated. Pinning the resolution —
    not just the table row — is what makes a needle reordered into the wrong
    place fail here instead of in production."""
    assert max_input_tokens_for(model) == max_input_tokens
    assert limits_for(model).max_output_tokens == max_output_tokens


@pytest.mark.parametrize(
    ("model", "max_input_tokens"),
    [
        ("gpt-5", _GPT5_MAX_INPUT_TOKENS),
        ("gpt-5-mini", _GPT5_MAX_INPUT_TOKENS),
        ("gpt-5-nano", _GPT5_MAX_INPUT_TOKENS),
        ("gpt-5.6", _GPT5_6_MAX_INPUT_TOKENS),
        ("gpt-5.6-terra", _GPT5_6_MAX_INPUT_TOKENS),
    ],
)
def test_a_split_window_is_recorded_as_the_input_half_the_provider_enforces(
    model: str,
    max_input_tokens: int,
) -> None:
    """The GPT-5 generations advertise a window that is input plus output and
    refuse input above a lower figure of their own. The table stores what a
    prompt may weigh, so it is the lower figure that belongs in it."""
    assert limits_for(model).max_input_tokens == max_input_tokens


def test_the_expensive_tier_stays_inside_what_the_provider_will_accept() -> None:
    """The consuming chain is what makes this a defect rather than a typo: the
    input figure becomes the transcript ceiling and the expensive compaction
    tier sits at 90% of it. Recording GPT-5's advertised 400,000 put that tier
    at 360,000 tokens of prompt against a hard 272,000 limit, so the request was
    refused before compaction had any chance to shrink it.

    Cost tracking is disabled because the budget-derived ceiling normally binds
    first; with it off the window governs alone, which is the deployment where a
    wrong window is fully exposed.
    """
    settings = load_settings()
    settings.model.model = "gpt-5"
    settings.cost.enabled = False

    assert 0 < session_compact_token_budget(settings) <= _GPT5_MAX_INPUT_TOKENS


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-6",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-fable-5",
    ],
)
def test_a_million_token_claude_is_not_compacted_like_a_200k_one(model: str) -> None:
    """The Anthropic tier needles carried the oldest generation's 200k window,
    so every current flagship compacted at a fifth of the transcript it could
    have held — paying for four summaries it did not need. It hides under the
    shipped defaults, where the run budget is the tighter ceiling, and surfaces
    the moment cost tracking is off or the budget is unset."""
    assert limits_for(model).max_input_tokens == 1_000_000


def test_an_unnamed_generation_degrades_to_the_smaller_window_not_the_larger() -> None:
    """One needle per generation rather than per release: a point release inside
    a named generation is sized correctly with no edit, while a generation
    nobody has named yet keeps the tier's oldest numbers.

    Only the first assertion fails against the previous table; the other two pin
    the direction of the guess rather than a corrected value. Guessing downward
    is the safe direction — too small a window compacts earlier than it had to,
    too large a one grows a transcript into a request the model refuses.
    """
    assert limits_for("claude-opus-5-1").max_input_tokens == 1_000_000
    assert limits_for("claude-opus-6").max_input_tokens == 200_000
    assert limits_for("claude-sonnet-7").max_input_tokens == 200_000


@pytest.mark.parametrize(
    "model",
    [
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-opus-4-5",
        "claude-opus-5",
        "claude-sonnet-4-5",
        "claude-sonnet-5",
        "deepseek-v4-pro",
        "gemini-2.5-pro",
        "gpt-4.1",
        "gpt-5",
        "gpt-5.6",
    ],
)
def test_a_model_family_we_support_is_in_the_table_rather_than_the_fallback(
    model: str,
) -> None:
    """A missing entry does not look like a bug: ``limits_for`` still answers,
    with the fallback's plausible 32k/8k. That is how GPT-5 — a model holding
    272k of prompt — went a whole release being sized like an unknown one."""
    assert limits_for(model) is not _FALLBACK


def test_a_newer_generation_needle_never_shadows_an_older_one() -> None:
    """Substring matching makes ordering load-bearing, and the failure is
    silent: a shadowed model keeps answering, with the wrong neighbour's
    numbers. Every family that gained a needle is checked against the family it
    could have swallowed."""
    # gpt-5 is a prefix of gpt-5.6, whose window is nearly four times larger.
    assert limits_for("gpt-5.6-sol").max_input_tokens == _GPT5_6_MAX_INPUT_TOKENS
    assert limits_for("gpt-4.1").max_input_tokens == 1_000_000
    assert limits_for("gpt-4o").max_output_tokens == 16_384
    assert limits_for("gpt-3.5-turbo").max_output_tokens == 4_096
    # The 3.5 generation of Haiku really is an 8k model; only 4.5 reaches 64k.
    assert limits_for("claude-3-5-haiku-20241022").max_output_tokens == 8_192
    # Opus releases the table does not name keep the conservative tier floor.
    assert limits_for("claude-opus-4-1").max_output_tokens == 32_000
    # The dated 4.5 snapshots must not be swallowed by the 4.6+ generation.
    assert limits_for("claude-opus-4-5-20251101").max_input_tokens == 200_000
    assert limits_for("claude-sonnet-4-5-20250929").max_input_tokens == 200_000
    # Gemini 2.0 is still an 8k model, so neither the 2.5 nor the 3.x needle
    # may reach it, and a 3.x name must not fall through to the trailing 8k.
    assert limits_for("gemini-2.0-flash").max_output_tokens == 8_192
    assert limits_for("gemini-3-flash-preview").max_output_tokens == 65_536
    assert limits_for("gemini-1.0-pro").max_output_tokens == 8_192
    # kimi-k3 holds four times what the K2.x needle behind it does.
    assert limits_for("kimi-k3").max_input_tokens == 1_000_000
    # The GLM releases the table does not name keep the oldest one's numbers.
    assert limits_for("glm-4-32b-0414-128k").max_output_tokens == 16_000
    assert limits_for("glm-6").max_input_tokens == 128_000


def test_a_suffix_sized_model_is_not_given_a_larger_models_window() -> None:
    """moonshot-v1 sizes purely by the suffix in its name, so one needle for
    the family handed moonshot-v1-8k the 128k of its largest sibling. An
    over-sized window is the failure that costs the run rather than a summary:
    the transcript grows to fill a context the model does not have, and the
    provider refuses the request instead of truncating it."""
    assert limits_for("moonshot-v1-8k").max_input_tokens == 8_192
    assert limits_for("moonshot-v1-32k").max_input_tokens == 32_768
    assert limits_for("moonshot-v1-128k").max_input_tokens == 128_000


def test_a_vendor_and_its_reseller_disagreeing_resolves_downward() -> None:
    """Zhipu gives GLM-5.2 a 1M window; Alibaba, hosting the same model, gives
    it 198k. Nothing in a model name says which one is serving it, so the row
    takes neither figure and stays on the generation's 200k — early compaction
    for whoever gets the larger, and a request that still fits for whoever
    gets the smaller."""
    assert limits_for("glm-5.2").max_input_tokens == 200_000


def test_every_row_says_where_its_numbers_came_from() -> None:
    """A row without a reference is a row nobody can re-check, and this table
    has been corrected three times by somebody happening to read one. Requiring
    a source makes the next row's provenance a condition of adding it rather
    than an archaeology exercise a year later."""
    for needle, _limits, source in _CATALOG:
        assert source.strip(), needle


def test_the_rows_nobody_could_verify_are_named_rather_than_assumed() -> None:
    """The honest output of a sweep is two lists, not one table. These vendors
    publish limits only through javascript-rendered docs, or ship open weights
    whose real limits belong to the host rather than the model, so their rows
    hold deliberate under-estimates.

    Pinning the list is what keeps the distinction alive: promoting a row out of
    it — or adding a new unverified one — has to be a decision somebody makes
    here, with a source, instead of a tag quietly changed in passing.
    """
    unverified = {needle for needle, _limits, source in _CATALOG if source == _UNVERIFIED}
    assert unverified == {
        "deepseek",
        "llama",
        "llama-3",
        "mistral",
        "mixtral",
        "qwen",
        "qwen-long",
        "qwen2.5",
    }
