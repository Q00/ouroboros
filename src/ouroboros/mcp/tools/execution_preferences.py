"""Read immutable persisted preferences at the MCP execution boundary."""

from collections.abc import Mapping
from typing import Any

from ouroboros.core.execution_preferences import (
    ResolvedExecutionPreferences,
    execution_preferences_from_contract,
    resolve_execution_preferences,
)


def persisted_execution_preferences(progress: Mapping[str, Any]) -> ResolvedExecutionPreferences:
    """Read the immutable preference contract used by a resumed session."""
    raw_contract = progress.get("execution_contract")
    if raw_contract is None:
        return resolve_execution_preferences(None, None)
    if not isinstance(raw_contract, Mapping):
        raise ValueError("Persisted execution_contract is invalid")
    raw_preferences = raw_contract.get("execution_preferences")
    preferences = execution_preferences_from_contract(raw_preferences)
    if preferences is None:
        if raw_preferences is None:
            return resolve_execution_preferences(None, None)
        raise ValueError("Persisted execution preferences are invalid")
    return preferences
