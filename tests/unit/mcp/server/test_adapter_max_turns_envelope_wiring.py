"""Wiring lock for ``mcp/server/adapter.py`` stage turn budgets.

The server composition root must not force interview/seed/reflect stage
adapters to ``max_turns=1``. Even an empty tool envelope can still leave
Claude Code without a second turn to emit final text after a tool-use stop,
which regressed ``ouroboros_interview`` in Q00/ouroboros#1530.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.mcp.server.adapter as adapter_module
from tests._envelope_wiring import find_max_turns_one_calls

ADAPTER_SOURCE = Path(adapter_module.__file__)


@pytest.fixture(scope="module")
def adapter_source() -> str:
    return ADAPTER_SOURCE.read_text(encoding="utf-8")


def test_adapter_module_does_not_force_stage_adapters_to_one_turn(adapter_source: str) -> None:
    calls = find_max_turns_one_calls(adapter_source)
    assert not calls, (
        f"Found {len(calls)} ``max_turns=1`` call site(s) in {ADAPTER_SOURCE.name}. "
        "MCP stage adapters must honor orchestrator.default_max_turns."
    )
