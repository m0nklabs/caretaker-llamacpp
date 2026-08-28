"""Configuration loading for caretaker.

Each caretaker reads *its own* host-local ``models.local.settings.yaml`` (the
shared settings source; see PLAN.md §0 / AGENTS.md). This module only locates
the file and surfaces its ``models`` + ``aliases`` blocks — there is no
lifecycle logic here yet.

The file path is resolved via ``CARETAKER_MODELS_FILE`` (env var); when unset
it defaults to a relative ``models.local.settings.yaml`` in the working
directory. No absolute, deployment-specific paths are hardcoded in this repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict

import yaml

MODELS_FILE_ENV = "CARETAKER_MODELS_FILE"
# Relative default; the systemd unit (deploy/systemd) sets CARETAKER_MODELS_FILE
# to the operator's chosen location. Never hardcode absolute paths here.
DEFAULT_MODELS_FILE = "models.local.settings.yaml"


class ModelsConfig(TypedDict):
    models: dict[str, dict[str, Any]]
    aliases: dict[str, str]


class ModelsConfigError(RuntimeError):
    """Raised when models.local.settings.yaml cannot be located or parsed."""


def models_file_path() -> Path:
    """Resolve the models config path from ``CARETAKER_MODELS_FILE``.

    Falls back to the relative ``DEFAULT_MODELS_FILE`` in the current working
    directory when the env var is unset. The result is *not* validated for
    existence here — see :func:`load_models_config`.
    """
    raw = os.environ.get(MODELS_FILE_ENV, DEFAULT_MODELS_FILE)
    return Path(raw)


def load_models_config() -> ModelsConfig:
    """Load ``models.local.settings.yaml`` and return its models/aliases.

    Returns a :class:`ModelsConfig` typed dict with ``models`` (name → config)
    and ``aliases`` (alias → canonical model). Raises
    :class:`ModelsConfigError` when the file is missing or unparseable.
    """
    path = models_file_path()
    if not path.is_file():
        raise ModelsConfigError(
            f"models config file not found at {path!s}; "
            f"set {MODELS_FILE_ENV} to point at this host's "
            "models.local.settings.yaml"
        )
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ModelsConfigError(f"failed to parse {path!s}: {exc}") from exc
    return ModelsConfig(
        models=data.get("models") or {},
        aliases=data.get("aliases") or {},
    )