"""Unit tests for ChecklistVerifyHandler (#366 part 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ouroboros.core.types import Result
from ouroboros.mcp.tools.evaluation_handlers import (
    ChecklistVerifyHandler,
    EvaluateHandler,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

VALID_SEED_WITH_MULTI_AC = """
goal: Add a payment module
constraints:
  - Must integrate with Stripe
acceptance_criteria:
  - Charge succeeds
  - Webhook signature verified
  - Refund path covered
ontology_schema:
  name: Payment
  description: Payment module
  fields:
    - name: provider
      field_type: string
      description: Provider name
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-pay-1
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.15
  interview_id: null
"""


VALID_SEED_NO_AC = """
goal: Empty goal
constraints: []
acceptance_criteria: []
ontology_schema:
  name: Empty
  description: empty
  fields:
    - name: x
      field_type: string
      description: x
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-empty
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""


class TestChecklistVerifyDefinition:
    """Tool definition exposes the right parameters."""

    def test_name_and_required_parameters(self) -> None:
        handler = ChecklistVerifyHandler()
        defn = handler.definition
        assert defn.name == "ouroboros_checklist_verify"

        names = {p.name for p in defn.parameters}
        assert {"session_id", "seed_content", "artifact"} <= names

        required = {p.name for p in defn.parameters if p.required}
        assert required == {"session_id", "seed_content", "artifact"}


class TestChecklistVerifyArgumentValidation:
    """Missing required arguments produce actionable errors."""

    async def test_missing_session_id(self) -> None:
        handler = ChecklistVerifyHandler()
        result = await handler.handle(
            {
                "seed_content": VALID_SEED_WITH_MULTI_AC,
                "artifact": "x",
            }
        )
        assert result.is_err
        assert "session_id is required" in str(result.error)

    async def test_missing_seed_content(self) -> None:
        handler = ChecklistVerifyHandler()
        result = await handler.handle(
            {
                "session_id": "s1",
                "artifact": "x",
            }
        )
        assert result.is_err
        assert "seed_content is required" in str(result.error)

    async def test_missing_artifact(self) -> None:
        handler = ChecklistVerifyHandler()
        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_WITH_MULTI_AC,
            }
        )
        assert result.is_err
        assert "artifact is required" in str(result.error)

    async def test_invalid_yaml(self) -> None:
        handler = ChecklistVerifyHandler()
        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": "not: valid: yaml: : :",
                "artifact": "x",
            }
        )
        assert result.is_err
        # Accept either YAML parse error or seed validation error —
        # both are surfaced as MCP errors, which is the contract.
        assert any(
            needle in str(result.error)
            for needle in ("Failed to parse seed YAML", "Seed validation failed")
        )

    async def test_seed_with_no_acceptance_criteria(self) -> None:
        handler = ChecklistVerifyHandler()
        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_NO_AC,
                "artifact": "x",
            }
        )
        assert result.is_err
        assert "no acceptance_criteria" in str(result.error)


class TestChecklistVerifyDelegation:
    """Delegates to EvaluateHandler with the seed's AC list."""

    async def test_delegates_with_full_ac_list(self) -> None:
        """ChecklistVerify forwards every AC to EvaluateHandler.handle()."""
        mock_evaluate = MagicMock(spec=EvaluateHandler)
        mock_evaluate.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Acceptance Criteria Checklist [ALL PASSED]",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "multi_ac": True,
                        "final_approved": True,
                        "passed_count": 3,
                        "ac_count": 3,
                        "pass_rate": 1.0,
                    },
                )
            )
        )
        handler = ChecklistVerifyHandler(evaluate_handler=mock_evaluate)

        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_WITH_MULTI_AC,
                "artifact": "def pay(): ...",
            }
        )

        assert result.is_ok
        # Verify the inner call received all ACs.
        mock_evaluate.handle.assert_awaited_once()
        call_args = mock_evaluate.handle.await_args.args[0]
        assert call_args["session_id"] == "s1"
        assert call_args["artifact"] == "def pay(): ..."
        assert call_args["acceptance_criteria"] == [
            "Charge succeeds",
            "Webhook signature verified",
            "Refund path covered",
        ]
        # seed_content must be forwarded so EvaluateHandler can pull goal/constraints
        assert call_args["seed_content"] == VALID_SEED_WITH_MULTI_AC

    async def test_augments_meta_with_verify_flag(self) -> None:
        """Response meta gets checklist_verify=True and seed_goal."""
        mock_evaluate = MagicMock(spec=EvaluateHandler)
        mock_evaluate.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                    is_error=False,
                    meta={"multi_ac": True, "final_approved": True},
                )
            )
        )
        handler = ChecklistVerifyHandler(evaluate_handler=mock_evaluate)

        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_WITH_MULTI_AC,
                "artifact": "x",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["checklist_verify"] is True
        assert meta["seed_goal"] == "Add a payment module"
        # Underlying evaluate meta still present
        assert meta["multi_ac"] is True

    async def test_evaluate_failure_propagates(self) -> None:
        """When EvaluateHandler returns Err, ChecklistVerify returns the same err."""
        mock_evaluate = MagicMock(spec=EvaluateHandler)
        mock_evaluate.handle = AsyncMock(return_value=Result.err(ValueError("pipeline exploded")))
        handler = ChecklistVerifyHandler(evaluate_handler=mock_evaluate)

        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_WITH_MULTI_AC,
                "artifact": "x",
            }
        )
        assert result.is_err

    async def test_forwards_working_dir_when_provided(self) -> None:
        """Optional working_dir is forwarded to the evaluator."""
        mock_evaluate = MagicMock(spec=EvaluateHandler)
        mock_evaluate.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                    is_error=False,
                    meta={"multi_ac": True, "final_approved": True},
                )
            )
        )
        handler = ChecklistVerifyHandler(evaluate_handler=mock_evaluate)

        result = await handler.handle(
            {
                "session_id": "s1",
                "seed_content": VALID_SEED_WITH_MULTI_AC,
                "artifact": "x",
                "working_dir": "/tmp/proj",
            }
        )

        assert result.is_ok
        call_args = mock_evaluate.handle.await_args.args[0]
        assert call_args["working_dir"] == "/tmp/proj"


class TestChecklistVerifyRegistered:
    """The handler is wired into the default tool set."""

    def test_registered_in_ouroboros_tools(self) -> None:
        from ouroboros.mcp.tools.definitions import OUROBOROS_TOOLS

        names = {h.definition.name for h in OUROBOROS_TOOLS}
        assert "ouroboros_checklist_verify" in names

    def test_factory_returns_handler(self) -> None:
        from ouroboros.mcp.tools.definitions import checklist_verify_handler

        handler = checklist_verify_handler(llm_backend="litellm")
        assert isinstance(handler, ChecklistVerifyHandler)
        assert handler.llm_backend == "litellm"
