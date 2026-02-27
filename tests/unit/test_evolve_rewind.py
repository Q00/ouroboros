"""Unit tests for EvolveRewindHandler."""

from __future__ import annotations

import pytest

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
