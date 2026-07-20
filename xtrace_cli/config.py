"""Config resolution for the ``xmem`` CLI.

Precedence (highest first):
    1. explicit flags on a command (handled by the CLI layer, not here)
    2. environment variables (``XTRACE_*``)
    3. the config file (``~/.config/xtrace-cli/config.yaml``)
    4. built-in defaults

The file holds the API key, so it is written ``0600``. ``org_id`` is NOT a
config knob — the server derives the org from the key (ENG-485); the deprecated
``X-Org-Id`` header is intentionally never sent.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

# Staging memory API. Override with ``XTRACE_BASE_URL`` or ``xmem config set``.
DEFAULT_BASE_URL = "https://api.staging.xtrace.ai"

# field name -> environment variable that overrides it
_ENV = {
    "base_url": "XTRACE_BASE_URL",
    "api_key": "XTRACE_API_KEY",
    "user_id": "XTRACE_USER_ID",
    "agent_id": "XTRACE_AGENT_ID",
    "app_id": "XTRACE_APP_ID",
    "namespace": "XTRACE_NAMESPACE",
}


def config_path() -> Path:
    """Location of the config file, honoring ``XDG_CONFIG_HOME``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "xtrace-cli" / "config.yaml"


@dataclass
class Config:
    """Resolved CLI configuration. All fields optional except ``base_url``."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    # Default scope axes, applied when a command doesn't pass its own.
    user_id: str | None = None
    agent_id: str | None = None
    app_id: str | None = None
    namespace: str | None = None

    @classmethod
    def _field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def load(cls) -> "Config":
        """File values overlaid by environment variables."""
        data: dict = {}
        path = config_path()
        if path.is_file():
            loaded = yaml.safe_load(path.read_text()) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{path} is not a YAML mapping")
            data = {k: v for k, v in loaded.items() if k in cls._field_names()}

        for name, env in _ENV.items():
            val = os.environ.get(env)
            if val is not None and val != "":
                data[name] = val

        data.setdefault("base_url", DEFAULT_BASE_URL)
        return cls(**data)

    def save(self) -> Path:
        """Persist to the config file (``0600``), skipping null values."""
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {k: v for k, v in asdict(self).items() if v is not None}
        path.write_text(yaml.safe_dump(body, sort_keys=True, default_flow_style=False))
        path.chmod(0o600)
        return path

    def redacted(self) -> dict:
        """A dict for display — the API key is masked, source hints included."""
        out = asdict(self)
        if out.get("api_key"):
            key = out["api_key"]
            out["api_key"] = f"{key[:7]}…{key[-4:]}" if len(key) > 12 else "set"
        return out

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "No API key configured. Run `xmem config set --api-key <xtk_…>` "
                "or set XTRACE_API_KEY."
            )
        return self.api_key


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""
