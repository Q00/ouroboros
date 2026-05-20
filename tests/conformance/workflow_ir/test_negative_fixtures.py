"""Negative Workflow IR conformance fixtures.

Each fixture in this file MUST be rejected by ``validate_workflow`` with a
specific, named error code from the locked #956 vocabulary. Conformance is
proved by:

  1. Asserting ``validate_workflow(spec).ok is False``.
  2. Asserting the expected error code appears in the error list.
  3. Asserting the error message is unambiguous: it names the failing
     identifier (node id, edge id, or schema field) so debuggers can map
     a validator verdict back to its root cause without re-running it.

These tests are offline-deterministic: no network, no model providers, no
subprocess, no plugin dispatch. Refs #1131, #956.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.workflow_ir import (
    WorkflowValidationError,
    validate_workflow,
)
from tests.conformance.workflow_ir.fixtures import (
    dangling_edge_spec,
    duplicate_node_id_spec,
    illegal_transition_spec,
    missing_schema_ref_spec,
    unreachable_terminal_spec,
)


def _error_codes(errors: tuple[WorkflowValidationError, ...]) -> tuple[str, ...]:
    return tuple(error.code for error in errors)


def _find_error(errors: tuple[WorkflowValidationError, ...], code: str) -> WorkflowValidationError:
    matches = tuple(error for error in errors if error.code == code)
    assert matches, f"expected validator error code {code!r}; got {_error_codes(errors)!r}"
    return matches[0]


class TestDanglingEdgeFixture:
    """Edges that reference unknown node ids must be flagged dangling."""

    def test_validator_rejects(self) -> None:
        result = validate_workflow(dangling_edge_spec())
        assert result.ok is False, (
            f"dangling edge fixture must fail validation; got {_error_codes(result.errors)!r}"
        )

    def test_emits_dangling_edge_code(self) -> None:
        result = validate_workflow(dangling_edge_spec())
        error = _find_error(result.errors, "dangling_edge")
        # The unambiguous-message contract: the failing identifier is named.
        assert "ghost" in error.message, (
            f"dangling_edge message must name the unresolved node id; got {error.message!r}"
        )
        assert error.edge_id == "edge_agent_ghost"
        assert error.node_id == "ghost"


class TestDuplicateNodeIdFixture:
    """Two nodes with the same canonical id must be rejected."""

    def test_validator_rejects(self) -> None:
        result = validate_workflow(duplicate_node_id_spec())
        assert result.ok is False

    def test_emits_duplicate_node_id_code(self) -> None:
        result = validate_workflow(duplicate_node_id_spec())
        error = _find_error(result.errors, "duplicate_node_id")
        # The duplicate id is named in the message — not a generic phrase.
        assert "task_agent" in error.message
        assert error.node_id == "task_agent"


class TestUnreachableTerminalFixture:
    """A terminal node that no execution path can reach must be rejected."""

    def test_validator_rejects(self) -> None:
        result = validate_workflow(unreachable_terminal_spec())
        assert result.ok is False

    def test_emits_unreachable_terminal_code(self) -> None:
        result = validate_workflow(unreachable_terminal_spec())
        error = _find_error(result.errors, "unreachable_terminal")
        # The unreachable terminal id is named in the message.
        assert "terminal_unreachable" in error.message
        assert error.node_id == "terminal_unreachable"


class TestMissingSchemaRefFixture:
    """Agent nodes missing input/evidence schema refs must be rejected."""

    def test_validator_rejects(self) -> None:
        result = validate_workflow(missing_schema_ref_spec())
        assert result.ok is False

    def test_emits_missing_schema_codes(self) -> None:
        result = validate_workflow(missing_schema_ref_spec())
        codes = set(_error_codes(result.errors))
        # The fixture tampers the agent node to drop BOTH refs, so the
        # validator must surface BOTH missing-schema codes for the same
        # node id. This locks down the validator's separate-rule semantics.
        assert "missing_evidence_schema" in codes
        assert "missing_input_schema" in codes
        for code in ("missing_evidence_schema", "missing_input_schema"):
            error = _find_error(result.errors, code)
            assert error.node_id == "task_agent"
            # Message names the node id so callers can locate the failure.
            assert "task_agent" in error.message


class TestIllegalTransitionFixture:
    """Self-loop transitions are illegal in v1 and must be rejected."""

    def test_validator_rejects(self) -> None:
        result = validate_workflow(illegal_transition_spec())
        assert result.ok is False

    def test_emits_self_loop_code(self) -> None:
        result = validate_workflow(illegal_transition_spec())
        error = _find_error(result.errors, "self_loop")
        # The offending edge AND node are both surfaced for debugging.
        assert error.edge_id == "edge_self_loop"
        assert error.node_id == "task_agent"
        assert "task_agent" in error.message


@pytest.mark.parametrize(
    ("builder", "expected_code"),
    [
        (dangling_edge_spec, "dangling_edge"),
        (duplicate_node_id_spec, "duplicate_node_id"),
        (unreachable_terminal_spec, "unreachable_terminal"),
        (missing_schema_ref_spec, "missing_evidence_schema"),
        (illegal_transition_spec, "self_loop"),
    ],
    ids=[
        "dangling_edge",
        "duplicate_node_id",
        "unreachable_terminal",
        "missing_schema_ref",
        "illegal_transition",
    ],
)
def test_negative_fixture_emits_expected_code(builder, expected_code: str) -> None:
    """Each negative fixture surfaces its expected validator code.

    This single parametrized assertion is the canonical 5-fixture coverage
    proof required by issue #1131.
    """
    result = validate_workflow(builder())
    assert result.ok is False, (
        f"{builder.__name__} expected to fail validation, "
        f"got {result.ok=}, errors={_error_codes(result.errors)!r}"
    )
    assert expected_code in _error_codes(result.errors), (
        f"{builder.__name__} expected error code {expected_code!r}; "
        f"got {_error_codes(result.errors)!r}"
    )
