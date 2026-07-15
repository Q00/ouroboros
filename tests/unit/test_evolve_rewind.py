"""Unit tests for EvolveRewindHandler."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.core.lineage import LineageStatus
from ouroboros.core.types import Result
from ouroboros.evolution.rewind import CommittedRewindResult
from ouroboros.mcp.tools.definitions import EvolveRewindHandler
from ouroboros.mcp.types import ToolInputType


class TestEvolveRewindHandlerDefinition:
    """Test the tool definition metadata."""

    def test_definition_name(self) -> None:
        handler = EvolveRewindHandler()
        assert handler.definition.name == "ouroboros_evolve_rewind"

    def test_definition_has_lineage_id_param(self) -> None:
        handler = EvolveRewindHandler()
        params = handler.definition.parameters
        lineage_param = next(p for p in params if p.name == "lineage_id")
        assert lineage_param.type == ToolInputType.STRING
        assert lineage_param.required is True

    def test_definition_has_to_generation_param(self) -> None:
        handler = EvolveRewindHandler()
        params = handler.definition.parameters
        gen_param = next(p for p in params if p.name == "to_generation")
        assert gen_param.type == ToolInputType.INTEGER
        assert gen_param.required is True

    def test_definition_param_count(self) -> None:
        handler = EvolveRewindHandler()
        assert len(handler.definition.parameters) == 2


class TestEvolveRewindHandlerErrors:
    """Test error handling in the handle method."""

    @pytest.mark.asyncio
    async def test_missing_lineage_id(self) -> None:
        handler = EvolveRewindHandler()
        result = await handler.handle({"to_generation": 1})
        assert result.is_err
        assert "lineage_id is required" in str(result.error)

    @pytest.mark.asyncio
    async def test_empty_lineage_id(self) -> None:
        handler = EvolveRewindHandler()
        result = await handler.handle({"lineage_id": "", "to_generation": 1})
        assert result.is_err
        assert "lineage_id is required" in str(result.error)

    @pytest.mark.asyncio
    async def test_missing_to_generation(self) -> None:
        handler = EvolveRewindHandler()
        result = await handler.handle({"lineage_id": "lin_test"})
        assert result.is_err
        assert "to_generation is required" in str(result.error)

    @pytest.mark.asyncio
    async def test_no_evolutionary_loop(self) -> None:
        handler = EvolveRewindHandler(evolutionary_loop=None)
        result = await handler.handle({"lineage_id": "lin_test", "to_generation": 1})
        assert result.is_err
        assert "EvolutionaryLoop not configured" in str(result.error)


@pytest.mark.asyncio
async def test_success_metadata_uses_committed_event_identity(monkeypatch) -> None:
    from tests.unit.tui.test_lineage_viewer import make_lineage

    lineage = make_lineage()
    rewound = lineage.rewind_to(1).with_status(LineageStatus.ACTIVE)
    occurred_at = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
    committed = CommittedRewindResult(
        lineage=rewound,
        lineage_id=lineage.lineage_id,
        from_generation=2,
        to_generation=1,
        rewind_event_id="rewind-event-1",
        rewind_occurred_at=occurred_at,
    )

    class _Store:
        async def initialize(self) -> None:
            return None

        async def replay_lineage(self, lineage_id: str):  # noqa: ARG002
            return [object()]

    class _Loop:
        def __init__(self) -> None:
            self.event_store = _Store()

        async def rewind_to(self, current, target):  # noqa: ARG002
            return Result.ok(committed)

    class _Projector:
        def project(self, events):  # noqa: ARG002
            return lineage

    monkeypatch.setattr("ouroboros.evolution.projector.LineageProjector", _Projector)
    handler = EvolveRewindHandler(evolutionary_loop=_Loop())

    result = await handler.handle({"lineage_id": lineage.lineage_id, "to_generation": 1})

    assert result.is_ok
    assert result.value.meta == {
        "lineage_id": lineage.lineage_id,
        "from_generation": 2,
        "to_generation": 1,
        "rewind_event_id": "rewind-event-1",
        "rewind_occurred_at": "2026-07-13T06:00:00Z",
    }
