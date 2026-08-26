"""``/config test`` reports optional VLM / S2 / embeddings, not just the main model."""

from __future__ import annotations

import pytest
import tomli_w

from omni.config import load_settings
from omni.config.paths import get_paths
from omni.config.user_edits import collect_config_health


@pytest.mark.asyncio
async def test_collect_config_health_skips_unset_optional_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.config import user_edits

    async def fake_model(settings):  # noqa: ANN001
        return True, "main ok"

    started: list[str] = []
    monkeypatch.setattr(user_edits, "test_model_connectivity", fake_model)
    items = await collect_config_health(load_settings(), on_start=started.append)
    by_name = {item.name: item for item in items}
    assert by_name["model"].status == "passed"
    assert by_name["vlm"].status == "skipped"
    assert "not configured" in by_name["vlm"].detail
    assert by_name["semantic_scholar"].status == "skipped"
    assert by_name["embeddings"].status == "skipped"
    assert started and "Testing" in started[0]


@pytest.mark.asyncio
async def test_collect_config_health_flags_incomplete_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omni.config import user_edits

    paths = get_paths()
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.config_file.open("wb") as fh:
        tomli_w.dump({"memory": {"embeddings_enabled": True, "embedding_provider": "openai_compatible"}}, fh)

    async def fake_model(settings):  # noqa: ANN001
        return True, "main ok"

    monkeypatch.setattr(user_edits, "test_model_connectivity", fake_model)
    items = await collect_config_health(load_settings())
    embeddings = next(item for item in items if item.name == "embeddings")
    assert embeddings.status == "failed"
    assert "base_url" in embeddings.detail
