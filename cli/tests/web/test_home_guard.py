"""Web configuration writes stay inside the Home this process started with."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omni.web.home_guard import (
    RESTART_REQUIRED_CODE,
    home_has_drifted,
    refuse_if_home_drifted,
    resolved_home,
)
from omni.web.protocol import RpcError


def _settings(home: str) -> SimpleNamespace:
    return SimpleNamespace(paths=SimpleNamespace(home=home))


def test_resolved_home_normalizes_the_owner_directory(tmp_path) -> None:
    home = tmp_path / "omni-home"
    home.mkdir()
    assert resolved_home(_settings(str(home / "."))) == str(home.resolve())


def test_reads_stay_available_after_home_drift() -> None:
    app = SimpleNamespace(state=SimpleNamespace(web_home="/process/home"))
    settings = _settings("/other/home")
    assert home_has_drifted(app, settings) is True
    refuse_if_home_drifted(app, "channel.describe", settings)
    refuse_if_home_drifted(app, "config.describe", settings)
    refuse_if_home_drifted(app, "channel.wechat.cancel", settings)
    refuse_if_home_drifted(app, "channel.wechat.status", settings)
    refuse_if_home_drifted(app, "skill.list", settings)
    refuse_if_home_drifted(app, "skill.info", settings)


def test_writes_and_channel_connect_freeze_after_home_drift() -> None:
    app = SimpleNamespace(state=SimpleNamespace(web_home="/process/home"))
    settings = _settings("/other/home")
    with pytest.raises(RpcError) as caught:
        refuse_if_home_drifted(app, "channel.configure", settings)
    assert caught.value.code == RESTART_REQUIRED_CODE
    with pytest.raises(RpcError) as caught:
        refuse_if_home_drifted(app, "channel.wechat.start", settings)
    assert caught.value.code == RESTART_REQUIRED_CODE
    with pytest.raises(RpcError) as caught:
        refuse_if_home_drifted(app, "config.set", settings)
    assert caught.value.code == RESTART_REQUIRED_CODE
    with pytest.raises(RpcError) as caught:
        refuse_if_home_drifted(app, "skill.add", settings)
    assert caught.value.code == RESTART_REQUIRED_CODE
    with pytest.raises(RpcError) as caught:
        refuse_if_home_drifted(app, "skill.remove", settings)
    assert caught.value.code == RESTART_REQUIRED_CODE
