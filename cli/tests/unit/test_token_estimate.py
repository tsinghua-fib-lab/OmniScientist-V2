"""P1-D′a: token counting uses a real tokenizer when available, heuristic else.

Offline (no ``tiktoken`` installed, or ``OMNI_DISABLE_TIKTOKEN=1``) it must fall
back to the deterministic heuristic and never crash — including on text that
contains tokenizer "special" markers.

``tiktoken`` is the ``tokens`` extra and is not installed by default, so the
heuristic is not a fallback in practice — it is the estimator that ships. That
makes its *units* load-bearing rather than merely approximate. Everything it is
compared against is a real token count: ``cost.max_total_tokens`` is enforced on
provider-reported usage, and a context window is the provider's own count. An
estimator running two-thirds high therefore does not just mis-report, it fires
every compaction threshold two-thirds early — discarding context to protect a
budget that was never in danger.

So the samples below record what a real tokenizer counts for the text a turn
actually sends, and the tests pin the estimator to those counts rather than to
each other.

The drift went unnoticed because the one test exercising the real tokenizer
skipped wherever the extra was absent, which was every documented developer
setup while CI installed it. Both now install it, ``conftest`` pins the suite to
the shipped estimator so nothing measures in two units by accident, and the
tests below opt back into the tokenizer to hold the two within a stated band of
each other. A skip here now means the vocabulary could not be loaded at all,
which is the only precondition these tests genuinely have.
"""

from __future__ import annotations

import os

import pytest

from omni.memory.compaction import _heuristic_tokens, estimate_tokens

# Measured with tiktoken 0.13.0, cl100k_base, on 2026-08-07. The samples are cut
# from the real thing: the abstract and the search envelope from the vendored
# ICLR corpus under skills/paper-review, the schemas from the default tool
# surface, the prompt from an assembled turn, and the Chinese prose from this
# repository's own design notes. cl100k is not every provider's tokenizer — the
# shipped default is a DeepSeek model with its own — but it is a real BPE
# vocabulary rather than a byte count, which is what the comparison needs.
#
# The counts are measurements, not targets. Re-cutting a sample changes them, and
# the number has to be re-measured rather than adjusted until the test passes.

# an abstract, as a literature tool hands one back
_PAPER_ABSTRACT = (
    'Accurate prediction of road user movement is increasingly required by many applications ranging from advanced driver assistance systems to autonomous driving, and especially crucial for road safety. Even though most traffic accident facilities account to bicycles, they have received little attention, as previous work focused mainly on pedestrians and motorized vehicles. In this work, we present the Great GATsBi, a domain-knowledge-based, hybrid, multimodal trajectory prediction framework for bicycles. The model inc'
)

# a search-result envelope: json wrapped around prose
_SEARCH_RESULT = (
    '{"status": "ok", "count": 2, "results": [{"paper_id": "FOx2BWIQyF", "title": "DUPS: Dynamic upsampling for efficient semantic segmentation", "abstract": "We present \\\\textbf{DUPS}, a coarse-to-fine vision transformer for semantic segmentation. Unlike models that begin with dense high-resolution tokens, DUPS starts at low resolution a"}, {"paper_id": "N5YcOxEcV8", "title": "Benchmarking MLLMs on Topological Reasoning of Chemical Reaction Diagrams", "abstract": "Chemical reaction diagrams are visual representations of complex process graphs, where understa'
)

# two schemas from the default tool surface
_TOOL_SCHEMAS = (
    '[{"type": "function", "function": {"name": "write_file", "description": "Write a file under an allowed root. To write a document longer than a few thousand words, send the first part with append omitted, then the rest in further calls with append=true — one call carrying the whole document can exceed the response limit and be cut off.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "A bare filename is stored in the workspace\'s artifacts directory; give a path with a directory to write somewhere specific."}, "co'
)

# the head of an assembled system prompt
_SYSTEM_PROMPT = (
    'You are OmniScientist, a local-first personal research agent.\n'
    '\n'
    'Your role is to help researchers with literature review, close reading and peer review, research\n'
    'ideation, scientific figures, deep research, reproducible experiments, and manuscript synthesis.\n'
    "You also act as a capable local agent for the user's working directory. You run on the user's own\n"
    'machine and keep project data local.\n'
    '\n'
    'Operating principles:\n'
    "- Infer the user's actual goal before acting. Plan internally when a request is broad or multi-step.\n"
    '- Us'
)

# omni's own source, as read_file returns it
_SOURCE_CODE = (
    'dow token budget (``token_budget``), keeping the most recent turns.\n'
    '_COMPACT_THRESHOLD = 30  # visible user/assistant/tool-result messages\n'
    '_COMPACT_KEEP_LAST = 8\n'
    '\n'
    '\n'
    'class SessionCompactor:\n'
    '    """Threshold-driven transcript folding with a fact flush."""\n'
    '\n'
    '    def __init__(\n'
    '        self,\n'
    '        *,\n'
    '        store: ConversationStore,\n'
    '        memory: MemoryService,\n'
    '        llm: LLMClient,\n'
    '        settings: OmniSettings,\n'
    '        tasks: TaskRecorder,\n'
    '    ) -> None:\n'
    '        self._store = store\n'
    '        self._memory = memory\n'
)

# Chinese documentation, headings and anchors included
_CHINESE_PROSE = (
    '谱方案](#存储知识图谱方案为什么是-json--索引)\n'
    '\n'
    '## 三层体系：L3 哲学立场 → L2 思维模式 → L1 科学事实\n'
    '\n'
    '---\n'
    '\n'
    '## 图结构\n'
    '\n'
    '```\n'
    'L3  人格内核 (固定 4 节点)\n'
    '    P01 科学价值排序    P02 核心信念    P03 自我认知    P04 语气\n'
    '     P01-P03 仅通过归纳边连到 L2；P04 不连任何 L2\n'
    '\n'
    'L2  思维模式 ('
)

# label, what cl100k_base really counts, sample
_CALIBRATION_SAMPLES: tuple[tuple[str, int, str], ...] = (
    ("abstract", 90, _PAPER_ABSTRACT),
    ("observation", 140, _SEARCH_RESULT),
    ("tool schemas", 125, _TOOL_SCHEMAS),
    ("system prompt", 104, _SYSTEM_PROMPT),
    ("source code", 117, _SOURCE_CODE),
    ("chinese prose", 160, _CHINESE_PROSE),
)

# The band the estimator has to stay inside. The floor is 1.0 rather than
# something below it because the two directions are not symmetric: an estimate
# above the real count compacts a little earlier than it had to, while one below
# lets a transcript grow into a request the provider refuses. The ceiling is
# where measurement put a script- and structure-aware byte census, with a little
# room for a sample cut differently.
_NEVER_BELOW_REAL = 1.0
_NEVER_MORE_THAN = 1.30


def _real_tokenizer_or_skip(monkeypatch):  # noqa: ANN001, ANN202
    """Undo the suite-wide pin and hand back the module with a live encoder.

    The precondition is that the *vocabulary loads*, not that the package is
    importable. ``tiktoken`` fetches its BPE vocabulary over the network the
    first time it is used, so an air-gapped machine has the import and no
    encoder; a guard reading ``find_spec`` called that machine a failure, for a
    reason having nothing to do with the change under review.
    """
    import omni.memory.compaction as comp

    monkeypatch.delenv("OMNI_DISABLE_TIKTOKEN", raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_ENC", None, raising=False)
    if comp._tiktoken_encoder() is None:
        pytest.skip(
            "tiktoken's BPE vocabulary could not be loaded — the package is "
            "absent, or its first-use fetch found no network"
        )
    return comp


def test_empty_is_zero():
    assert estimate_tokens("") == 0


def test_heuristic_counts_cjk_heavier_than_ascii():
    # 4 CJK chars ≈ 4 tokens; 4 ascii chars ≈ 1 token. CJK must cost more.
    assert _heuristic_tokens("研究智能体") > _heuristic_tokens("abcd")


@pytest.mark.parametrize(
    ("real_tokens", "sample"),
    [(real, text) for _label, real, text in _CALIBRATION_SAMPLES],
    ids=[label for label, _real, _text in _CALIBRATION_SAMPLES],
)
def test_the_offline_estimate_stays_in_the_units_a_provider_bills(
    real_tokens: int,
    sample: str,
) -> None:
    """One divisor over all bytes cannot hold this band, which is the whole point.

    A byte count that ignores what the bytes are is not uniformly wrong, it is
    wrong in opposite directions at once: 3 bytes per token over-counts English
    research prose by 94% and under-counts Chinese markdown by 24%. So no single
    divisor fixes it and no single scaling factor applied downstream fixes it
    either — the ratio is a property of the text, not a constant.
    """
    ratio = _heuristic_tokens(sample) / real_tokens

    assert _NEVER_BELOW_REAL <= ratio <= _NEVER_MORE_THAN


def test_no_sample_is_estimated_at_twice_another_ones_rate() -> None:
    """The band above is per-sample; this is the same claim about the spread.

    An estimator can sit inside a band on average and still rank two transcripts
    of equal real size very differently, which is what makes a shared threshold
    mean different things for an English run and a Chinese one.
    """
    ratios = [
        _heuristic_tokens(text) / real for _label, real, text in _CALIBRATION_SAMPLES
    ]

    assert max(ratios) / min(ratios) < 1.5


def test_disable_env_forces_heuristic(monkeypatch):
    import omni.memory.compaction as comp

    monkeypatch.setenv("OMNI_DISABLE_TIKTOKEN", "1")
    monkeypatch.setattr(comp, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_ENC", None, raising=False)
    assert comp._tiktoken_encoder() is None
    # With the encoder disabled the public estimate equals the heuristic.
    assert estimate_tokens("hello world 你好") == _heuristic_tokens("hello world 你好")


def test_never_crashes_on_special_tokens():
    # A naïve tiktoken call would raise on <|endoftext|>; ours must not.
    assert estimate_tokens("<|endoftext|> hi <|im_start|>") > 0


def test_real_tokenizer_is_used_when_available(monkeypatch):
    comp = _real_tokenizer_or_skip(monkeypatch)

    assert comp._tiktoken_encoder() is not None
    assert comp.estimate_tokens("hello world") > 0


@pytest.mark.skipif(
    not os.environ.get("CI"), reason="only a CI runner is required to have the vocabulary"
)
def test_a_ci_runner_is_not_allowed_to_skip_the_tokenizer_comparison(monkeypatch):  # noqa: ANN001
    """Skipping is right on an air-gapped machine and wrong on a build runner.

    The tests around this one skip when the vocabulary will not load, because a
    developer without a route out cannot be asked to prove anything about a
    tokenizer they cannot obtain. On CI the same skip means the shipped estimator
    went out having never been compared against a real one — the exact silence
    this file was rewritten to end — so there it is a failure instead.

    Two preconditions hide inside "has a real tokenizer", and only one of them
    is ours. CI installs every extra, so the package must import: if it does
    not, the install is wrong and that is a failure. The vocabulary is a
    separate matter — ``tiktoken`` ships none and fetches ``cl100k_base`` over
    the network on first use — so a runner that cannot reach the store has
    proved nothing about this repository, and failing the release for it once
    stopped a build over a blob-storage hiccup.
    """
    import omni.memory.compaction as comp

    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - only on a broken install
        pytest.fail(f"CI installs every extra, so tiktoken must import: {exc}")

    try:
        tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # noqa: BLE001 - any fetch failure is the runner's
        pytest.skip(f"this runner could not fetch the vocabulary: {exc}")

    monkeypatch.delenv("OMNI_DISABLE_TIKTOKEN", raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_TRIED", False, raising=False)
    monkeypatch.setattr(comp, "_TIKTOKEN_ENC", None, raising=False)

    assert comp._tiktoken_encoder() is not None


@pytest.mark.parametrize(
    ("real_tokens", "sample"),
    [(real, text) for _label, real, text in _CALIBRATION_SAMPLES],
    ids=[label for label, _real, _text in _CALIBRATION_SAMPLES],
)
def test_the_recorded_counts_are_what_a_real_tokenizer_returns(
    monkeypatch,  # noqa: ANN001
    real_tokens: int,
    sample: str,
) -> None:
    """The band above is only as good as the numbers it is measured against.

    Those numbers were recorded by hand from a tokenizer nothing in the suite
    ran, so a re-cut sample or a mistyped digit would quietly move the target
    the heuristic is held to. This is the one assertion that keeps them
    measurements rather than assertions about themselves.
    """
    comp = _real_tokenizer_or_skip(monkeypatch)
    encoder = comp._tiktoken_encoder()

    assert len(encoder.encode(sample, disallowed_special=())) == real_tokens


def test_both_estimators_report_the_same_units_on_a_whole_transcript(
    monkeypatch,  # noqa: ANN001
) -> None:
    """The two paths through ``estimate_tokens`` have to mean the same thing.

    Every ceiling the estimate is compared against is a real provider count, so
    which path a machine takes must not change when compaction fires. Holding
    them within one band is what makes the choice an implementation detail
    instead of a behavioural fork — and it is asserted on the concatenated
    corpus rather than per sample because a transcript is what the loop weighs.
    """
    comp = _real_tokenizer_or_skip(monkeypatch)
    transcript = "\n\n".join(text for _label, _real, text in _CALIBRATION_SAMPLES)

    ratio = comp._heuristic_tokens(transcript) / comp.estimate_tokens(transcript)

    assert _NEVER_BELOW_REAL <= ratio <= _NEVER_MORE_THAN
