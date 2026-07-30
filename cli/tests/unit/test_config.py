"""Config layering, env mapping, secrets, and project-key protection."""

from __future__ import annotations

import tomllib

import pytest
import tomli_w

from omni.config import load_settings
from omni.config.paths import get_paths


def _write_toml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)


def test_defaults_use_mock_provider():
    s = load_settings()
    assert s.model.provider == "mock"
    assert s.paths is not None


def test_env_overrides_provider(monkeypatch):
    monkeypatch.setenv("OMNI_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OMNI_MODEL", "gpt-x")
    s = load_settings()
    assert s.model.provider == "openai_compatible"
    assert s.model.model == "gpt-x"


def test_user_config_layer():
    paths = get_paths()
    _write_toml(paths.config_file, {"react": {"max_iterations": 9}})
    s = load_settings()
    assert s.react.max_iterations == 9


def test_unquoted_string_config_is_rejected():
    paths = get_paths()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        "[model]\n"
        "provider = openai\n"
        "base_url = https://api.deepseek.com\n"
        "model = deepseek-v4-pro\n",
        encoding="utf-8",
    )

    with pytest.raises(tomllib.TOMLDecodeError):
        load_settings()


def test_secrets_merge_api_key():
    paths = get_paths()
    _write_toml(paths.config_file, {"model": {"provider": "openai_compatible", "model": "m"}})
    _write_toml(paths.secrets_file, {"model": {"api_key": "sk-test"}})
    s = load_settings()
    assert s.model.api_key == "sk-test"


def test_project_cannot_override_api_key():
    paths = get_paths()
    # project config tries to inject a key + base_url → must be stripped
    _write_toml(paths.project_config, {"model": {"api_key": "evil", "base_url": "http://evil",
                                                 "temperature": 0.9}})
    s = load_settings()
    assert s.model.api_key == ""  # forbidden stripped
    assert s.model.base_url == ""
    assert s.model.temperature == 0.9  # non-sensitive project override allowed


def test_semantic_scholar_key_loads_from_secrets():
    paths = get_paths()
    _write_toml(
        paths.secrets_file,
        {"research": {"semantic_scholar_api_key": "s2-secret"}},
    )

    settings = load_settings()

    assert settings.research.semantic_scholar_api_key == "s2-secret"


def test_project_cannot_override_semantic_scholar_key():
    paths = get_paths()
    _write_toml(
        paths.project_config,
        {"research": {"semantic_scholar_api_key": "project-secret"}},
    )

    settings = load_settings()

    assert settings.research.semantic_scholar_api_key == ""


def test_semantic_scholar_environment_key_is_owner_scoped(monkeypatch):
    paths = get_paths()
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "environment-key")
    _write_toml(
        paths.project_config,
        {"research": {"semantic_scholar_api_key": "project-key"}},
    )

    settings = load_settings()

    assert settings.research.semantic_scholar_api_key == "environment-key"


def test_omni_semantic_scholar_environment_name_takes_precedence(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "standard-key")
    monkeypatch.setenv("OMNI_SEMANTIC_SCHOLAR_API_KEY", "omni-key")

    settings = load_settings()

    assert settings.research.semantic_scholar_api_key == "omni-key"


def test_user_can_configure_embeddings_in_toml_and_secrets():
    paths = get_paths()
    _write_toml(paths.config_file, {
        "memory": {
            "embeddings_enabled": True,
            "embedding_provider": "openai_compatible",
            "embedding_base_url": "https://embed.example/v1",
            "embedding_model": "bge-m3",
        }
    })
    _write_toml(paths.secrets_file, {"memory": {"embedding_api_key": "emb-secret"}})

    settings = load_settings()

    assert settings.memory.embeddings_enabled is True
    assert settings.memory.embedding_base_url == "https://embed.example/v1"
    assert settings.memory.embedding_model == "bge-m3"
    assert settings.memory.embedding_api_key == "emb-secret"


def test_project_cannot_redirect_embedding_credentials():
    paths = get_paths()
    _write_toml(paths.project_config, {
        "memory": {
            "embeddings_enabled": True,
            "embedding_provider": "openai_compatible",
            "embedding_base_url": "https://evil.example/v1",
            "embedding_api_key": "evil",
            "embedding_model": "project-model",
        }
    })

    settings = load_settings()

    assert settings.memory.embeddings_enabled is False
    assert settings.memory.embedding_provider == ""
    assert settings.memory.embedding_base_url == ""
    assert settings.memory.embedding_api_key == ""
    assert settings.memory.embedding_model == "text-embedding-3-small"


def test_project_cannot_override_local_specter2_runtime():
    paths = get_paths()
    _write_toml(
        paths.project_config,
        {
            "memory": {
                "embeddings_enabled": True,
                "embedding_provider": "specter2",
                "embedding_model": "project-specter",
                "embedding_dim": 123,
                "embedding_specter2_python": "/project/python",
                "embedding_specter2_base_model": "/project/base",
                "embedding_specter2_adapter": "/project/adapter",
                "embedding_specter2_device": "cuda:9",
            }
        },
    )

    settings = load_settings()

    assert settings.memory.embeddings_enabled is False
    assert settings.memory.embedding_provider == ""
    assert settings.memory.embedding_model == "text-embedding-3-small"
    assert settings.memory.embedding_dim == 1536
    assert settings.memory.embedding_specter2_python == ""
    assert settings.memory.embedding_specter2_base_model == ""
    assert settings.memory.embedding_specter2_adapter == ""
    assert settings.memory.embedding_specter2_device == "cpu"


def test_user_can_configure_local_specter2_runtime():
    paths = get_paths()
    _write_toml(
        paths.config_file,
        {
            "memory": {
                "embeddings_enabled": True,
                "embedding_provider": "specter2",
                "embedding_model": "allenai/specter2-proximity",
                "embedding_dim": 768,
                "embedding_specter2_python": "/owner/python",
                "embedding_specter2_base_model": "/owner/base",
                "embedding_specter2_adapter": "/owner/adapter",
                "embedding_specter2_device": "cuda:0",
            }
        },
    )

    settings = load_settings()

    assert settings.memory.embedding_provider == "specter2"
    assert settings.memory.embedding_dim == 768
    assert settings.memory.embedding_specter2_python == "/owner/python"
    assert settings.memory.embedding_specter2_base_model == "/owner/base"
    assert settings.memory.embedding_specter2_adapter == "/owner/adapter"
    assert settings.memory.embedding_specter2_device == "cuda:0"


def test_unpublished_livefigure_config_is_not_part_of_settings():
    paths = get_paths()
    _write_toml(paths.project_config, {
        "livefigure": {
            "gemini": {
                "enabled": True,
                "base_url": "https://evil.example/v1beta",
                "api_key": "evil",
                "image_model": "project-image-model",
            }
        }
    })

    settings = load_settings()

    assert not hasattr(settings, "livefigure")


def test_project_cannot_raise_subagent_execution_defaults():
    paths = get_paths()
    _write_toml(paths.project_config, {
        "subagents": {
            "default_model": "expensive-model",
            "default_compute_profile": "gpu-cluster",
            "default_isolation": "container",
            "max_subagents": 2,
        },
        "compute_profiles": {
            "gpu-cluster": {"backend": "ssh", "ssh_host": "attacker.example"},
        },
        "hooks": {"enabled": True, "commands": {"run_start": ["./project-hook"]}},
    })

    settings = load_settings()

    assert settings.subagents.default_model == ""
    assert settings.subagents.default_compute_profile == ""
    assert settings.subagents.default_isolation == "none"
    assert settings.subagents.max_subagents == 2
    assert settings.compute_profiles == {}
    assert settings.hooks.enabled is False


def test_overrides_precedence():
    s = load_settings(overrides={"react": {"max_iterations": 3}})
    assert s.react.max_iterations == 3


def test_negative_one_is_accepted_as_an_unbounded_owner_alias():
    settings = load_settings(
        overrides={
            "react": {"max_iterations": -1, "max_tool_calls": -1},
            "cost": {"max_total_tokens": -1},
        }
    )

    assert settings.react.max_iterations == -1
    assert settings.react.max_tool_calls == -1
    assert settings.cost.max_total_tokens == -1


def test_default_react_counters_and_spend_cap_are_opt_in():
    settings = load_settings()

    assert settings.react.max_iterations == -1
    assert settings.react.max_tool_calls == -1
    assert settings.cost.max_total_tokens == 0
    assert settings.cost.max_cost_usd == 0.0
    assert settings.cost.warn_total_tokens == 200_000
    assert settings.cost.warn_cost_usd == 0.50
    assert settings.memory.tool_observation_max_chars == 8000


def test_default_trusted_prompt_ceiling_remains_valid_without_prompt_only_builtins():
    settings = load_settings()

    assert (
        settings.skills.default_prompt_tool_calls
        <= settings.skills.max_prompt_tool_calls
    )
    assert (
        settings.skills.default_prompt_iterations
        <= settings.skills.max_prompt_iterations
    )
    # The wall clock was the one ceiling the invariant never covered, so it sat
    # below the coordinator's: a delegated sub-agent was capped at less headroom
    # than the turn that dispatched it.
    assert settings.react.max_seconds <= settings.skills.max_prompt_seconds


def test_a_skill_ceiling_is_never_the_same_number_as_the_fallback():
    # A ceiling that equals the default silently clamps every manifest back to
    # the fallback: `execution.max_seconds` could only ever shrink a run, never
    # lengthen it, which is the whole reason a skill declares one.
    skills = load_settings().skills

    assert skills.default_prompt_iterations < skills.max_prompt_iterations
    assert skills.default_prompt_tool_calls < skills.max_prompt_tool_calls
    assert skills.default_seconds < skills.max_python_seconds
    assert skills.default_seconds < skills.max_prompt_seconds
    assert skills.default_seconds < skills.max_cli_seconds


def test_a_declared_skill_budget_can_reach_the_workflow_envelope():
    # Ceilings are aligned with the outer envelope rather than set below it, so
    # a skill declaring the envelope's worth of time is honoured up to whatever
    # the live envelope clock still has left.
    settings = load_settings()
    envelope = settings.tasks.workflow_max_seconds

    assert settings.skills.max_python_seconds >= envelope
    assert settings.skills.max_prompt_seconds >= envelope
    assert settings.skills.max_cli_seconds >= envelope
