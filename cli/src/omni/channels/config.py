"""Per-channel config/secrets helpers."""

from __future__ import annotations

import tomllib
from typing import Any

from omni.channels.credentials import resolve_secret_ref
from omni.config.settings import OmniSettings


def load_channel_config(settings: OmniSettings, name: str) -> dict[str, Any]:
    paths = settings.paths
    if paths is None:
        return {}
    cfg_path = paths.channels_dir / f"{name}.toml"
    cfg = _read(cfg_path)
    secrets = _read(paths.secrets_file).get("channels", {})
    secret_cfg = secrets.get(name, {}) if isinstance(secrets, dict) else {}
    out = dict(cfg)
    if isinstance(secret_cfg, dict):
        out.update(secret_cfg)
    refs = out.get("credential_refs")
    if isinstance(refs, dict):
        for key, ref in refs.items():
            if not isinstance(key, str) or not isinstance(ref, str):
                continue
            value = resolve_secret_ref(ref)
            if value:
                out[key] = value
    return out


def _read(path) -> dict[str, Any]:  # noqa: ANN001
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
