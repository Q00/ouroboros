"""Tests for execution runtime scope naming helpers."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionRuntimeScope,
    build_ac_runtime_identity,
    build_ac_runtime_scope,
    build_level_coordinator_runtime_scope,
)


class TestBuildACRuntimeScope:
    """Tests for AC-scoped runtime storage naming."""

    def test_root_ac_scope(self) -> None:
        scope = build_ac_runtime_scope(3)

        assert scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="ac_3",
            state_path="execution.acceptance_criteria.ac_3.implementation_session",
        )
        assert scope.retry_attempt == 0
        assert scope.attempt_number == 1

    def test_root_ac_scope_is_execution_scoped_when_context_provided(self) -> None:
        scope = build_ac_runtime_scope(3, execution_context_id="workflow:alpha/beta")

        assert scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="workflow_alpha_beta_ac_3",
            state_path=(
                "execution.workflows.workflow_alpha_beta."
                "acceptance_criteria.ac_3.implementation_session"
            ),
        )

    def test_sub_ac_scope(self) -> None:
        scope = build_ac_runtime_scope(
            500,
            is_sub_ac=True,
            parent_ac_index=5,
            sub_ac_index=2,
        )

        assert scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="sub_ac_5_2",
            state_path=(
                "execution.acceptance_criteria.ac_5.sub_acs.sub_ac_2.implementation_session"
            ),
        )

    def test_sub_ac_scope_is_execution_scoped_when_context_provided(self) -> None:
        scope = build_ac_runtime_scope(
            500,
            execution_context_id="workflow:alpha/beta",
            is_sub_ac=True,
            parent_ac_index=5,
            sub_ac_index=2,
        )

        assert scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="workflow_alpha_beta_sub_ac_5_2",
            state_path=(
                "execution.workflows.workflow_alpha_beta.acceptance_criteria."
                "ac_5.sub_acs.sub_ac_2.implementation_session"
            ),
        )

    def test_retry_attempt_keeps_same_scope_identity(self) -> None:
        first_attempt = build_ac_runtime_scope(3, retry_attempt=0)
        retry_attempt = build_ac_runtime_scope(3, retry_attempt=2)

        assert retry_attempt.aggregate_id == first_attempt.aggregate_id
        assert retry_attempt.state_path == first_attempt.state_path
        assert retry_attempt.retry_attempt == 2
        assert retry_attempt.attempt_number == 3

    def test_negative_retry_attempt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="retry_attempt must be >= 0"):
            build_ac_runtime_scope(1, retry_attempt=-1)


class TestBuildLevelCoordinatorRuntimeScope:
    """Tests for level-scoped coordinator runtime storage naming."""

    def test_level_coordinator_scope_is_separate_from_ac_scope(self) -> None:
        ac_scope = build_ac_runtime_scope(1)
        coordinator_scope = build_level_coordinator_runtime_scope("exec_abc123", 2)

        assert coordinator_scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="exec_abc123_level_2_coordinator_reconciliation",
            state_path=(
                "execution.workflows.exec_abc123.levels.level_2.coordinator_reconciliation_session"
            ),
        )
        assert coordinator_scope.aggregate_id != ac_scope.aggregate_id
        assert coordinator_scope.state_path != ac_scope.state_path

    def test_level_coordinator_scope_normalizes_workflow_key(self) -> None:
        scope = build_level_coordinator_runtime_scope("workflow:alpha/beta", 1)

        assert scope.aggregate_id == "workflow_alpha_beta_level_1_coordinator_reconciliation"
        assert (
            scope.state_path == "execution.workflows.workflow_alpha_beta.levels.level_1."
            "coordinator_reconciliation_session"
        )


class TestBuildACRuntimeIdentity:
    """Tests for AC-scoped OpenCode session identity."""

    def test_root_ac_identity_distinguishes_scope_from_attempt(self) -> None:
        identity = build_ac_runtime_identity(3, execution_context_id="workflow:alpha/beta")

        assert identity == ACRuntimeIdentity(
            runtime_scope=ExecutionRuntimeScope(
                aggregate_type="execution",
                aggregate_id="workflow_alpha_beta_ac_3",
                state_path=(
                    "execution.workflows.workflow_alpha_beta."
                    "acceptance_criteria.ac_3.implementation_session"
                ),
            ),
            ac_index=3,
        )
        assert identity.ac_id == "workflow_alpha_beta_ac_3"
        assert identity.session_scope_id == "workflow_alpha_beta_ac_3"
        assert identity.session_attempt_id == "workflow_alpha_beta_ac_3_attempt_1"
        assert identity.cache_key == identity.session_attempt_id
        assert identity.to_metadata() == {
            "ac_id": "workflow_alpha_beta_ac_3",
            "scope": "ac",
            "session_role": "implementation",
            "retry_attempt": 0,
            "attempt_number": 1,
            "session_scope_id": "workflow_alpha_beta_ac_3",
            "session_attempt_id": "workflow_alpha_beta_ac_3_attempt_1",
            "session_state_path": (
                "execution.workflows.workflow_alpha_beta."
                "acceptance_criteria.ac_3.implementation_session"
            ),
            "ac_index": 3,
        }

    def test_retry_attempt_gets_fresh_session_attempt_identity(self) -> None:
        first_attempt = build_ac_runtime_identity(3, retry_attempt=0)
        retry_attempt = build_ac_runtime_identity(3, retry_attempt=1)

        assert retry_attempt.ac_id == first_attempt.ac_id
        assert retry_attempt.session_scope_id == first_attempt.session_scope_id
        assert retry_attempt.session_state_path == first_attempt.session_state_path
        assert retry_attempt.session_attempt_id != first_attempt.session_attempt_id
        assert first_attempt.session_attempt_id == "ac_3_attempt_1"
        assert retry_attempt.session_attempt_id == "ac_3_attempt_2"

    def test_sub_ac_identity_is_tied_only_to_that_sub_ac(self) -> None:
        identity = build_ac_runtime_identity(
            500,
            execution_context_id="workflow:alpha/beta",
            is_sub_ac=True,
            parent_ac_index=5,
            sub_ac_index=2,
        )

        assert identity.ac_index is None
        assert identity.parent_ac_index == 5
        assert identity.sub_ac_index == 2
        assert identity.session_scope_id == "workflow_alpha_beta_sub_ac_5_2"
        assert identity.session_attempt_id == "workflow_alpha_beta_sub_ac_5_2_attempt_1"
        assert identity.to_metadata() == {
            "ac_id": "workflow_alpha_beta_sub_ac_5_2",
            "scope": "ac",
            "session_role": "implementation",
            "retry_attempt": 0,
            "attempt_number": 1,
            "session_scope_id": "workflow_alpha_beta_sub_ac_5_2",
            "session_attempt_id": "workflow_alpha_beta_sub_ac_5_2_attempt_1",
            "session_state_path": (
                "execution.workflows.workflow_alpha_beta.acceptance_criteria."
                "ac_5.sub_acs.sub_ac_2.implementation_session"
            ),
            "parent_ac_index": 5,
            "sub_ac_index": 2,
        }
