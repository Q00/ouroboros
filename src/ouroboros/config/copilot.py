"""Copilot transport selection within the existing runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

from ouroboros.config.loader import load_config
from ouroboros.config.models import OrchestratorConfig
from ouroboros.core.errors import ConfigError


def _orchestrator_config() -> OrchestratorConfig:
    try:
        return load_config().orchestrator
    except ConfigError as exc:
        # An unconfigured installation keeps CLI defaults. Invalid configuration
        # must not silently switch transport or authorize fallback.
        if exc.config_file is None or Path(exc.config_file).exists():
            raise
        return OrchestratorConfig()


def get_copilot_transport() -> str:
    """Resolve the trusted environment override, then YAML, then legacy CLI."""
    value = os.environ.get("OUROBOROS_COPILOT_TRANSPORT", "").strip().lower()
    if value:
        if value not in {"cli", "acp"}:
            raise ValueError("OUROBOROS_COPILOT_TRANSPORT must be cli or acp")
        return value
    return _orchestrator_config().copilot_transport


def get_copilot_acp_fallback() -> bool:
    """Allow safe pre-prompt fallback; never downgrade authentication failures."""
    value = os.environ.get("OUROBOROS_COPILOT_ACP_FALLBACK", "").strip().lower()
    if value:
        if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError("OUROBOROS_COPILOT_ACP_FALLBACK must be a boolean")
        return value in {"1", "true", "yes", "on"}
    return _orchestrator_config().copilot_acp_fallback


__all__ = ["get_copilot_acp_fallback", "get_copilot_transport"]
