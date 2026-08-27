"""Unexpanded $VAR paths get a generic host hint, not a rewritten command."""

from __future__ import annotations

from omni.skills_runtime.exec_feedback import unexpanded_env_hint


def test_unexpanded_hint_resolves_live_env_vars() -> None:
    hint = unexpanded_env_hint(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'$OMNI_OUTPUT_DIR/RAG.pptx'",
        {"OMNI_OUTPUT_DIR": "/tmp/outbox", "TMPDIR": "/tmp/exec"},
    )
    assert hint.startswith("[unexpanded-env]")
    assert "$OMNI_OUTPUT_DIR" in hint
    assert "OMNI_OUTPUT_DIR=/tmp/outbox" in hint
    assert "os.environ" in hint
    assert "omni_io" in hint
    assert "single-quoted heredoc" in hint


def test_unexpanded_hint_ignores_ordinary_missing_paths() -> None:
    assert (
        unexpanded_env_hint(
            "FileNotFoundError: /tmp/outbox/missing.csv",
            {"OMNI_OUTPUT_DIR": "/tmp/outbox"},
        )
        == ""
    )


def test_unexpanded_hint_ignores_unknown_tokens() -> None:
    assert (
        unexpanded_env_hint(
            "No such file or directory: '$NOT_EXPORTED/x'",
            {"OMNI_OUTPUT_DIR": "/tmp/outbox"},
        )
        == ""
    )
