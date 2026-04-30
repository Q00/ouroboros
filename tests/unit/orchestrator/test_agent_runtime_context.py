"""Unit tests for :class:`AgentRuntimeContext`.

Issue: #515 / #474 Q1. The context is intentionally narrow: it must
remain frozen, slot-based, and only carry the minimal fields the
maintainer alignment in #476 Q1 specified.
"""

from __future__ import annotations

import dataclasses

import pytest

from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.orchestrator.control_bus import ControlBus
from ouroboros.persistence.event_store import EventStore


def test_context_is_frozen_and_slotted() -> None:
    """The dataclass must reject attribute mutation post-construction."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    context = AgentRuntimeContext(event_store=store)

    assert dataclasses.is_dataclass(context)
    assert context.runtime_backend is None
    assert context.llm_backend is None
    assert context.mcp_bridge is None
    assert context.control is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.runtime_backend = "codex_cli"  # type: ignore[misc]


def test_context_carries_optional_control_bus() -> None:
    """The optional ControlBus slot is the reactive surface for #515."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    bus = ControlBus()
    context = AgentRuntimeContext(event_store=store, control=bus)

    assert context.control is bus
    # Existing handlers that do not opt into the bus still see the
    # default ``None`` and remain valid.
    no_bus = AgentRuntimeContext(event_store=store)
    assert no_bus.control is None


def test_context_membership_is_narrow() -> None:
    """Adding a field requires a deliberate PR (no service-locator drift).

    This regression test pins the field set so reviewers see explicit
    diffs when the membership grows. If this assertion fails because a
    field was added, the PR must include a justification line per the
    #476 Q1 narrow-membership commitment.
    """
    expected = {
        "event_store",
        "runtime_backend",
        "llm_backend",
        "mcp_bridge",
        "control",
    }
    actual = {f.name for f in dataclasses.fields(AgentRuntimeContext)}
    assert actual == expected
