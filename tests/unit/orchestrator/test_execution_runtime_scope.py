"""Tests for execution runtime scope naming helpers."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
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
            aggregate_id="ac_4",
            state_path="execution.acceptance_criteria.ac_4.implementation_session",
        )
        assert scope.retry_attempt == 0
        assert scope.attempt_number == 1

    def test_root_ac_scope_is_execution_scoped_when_context_provided(self) -> None:
        scope = build_ac_runtime_scope(3, execution_context_id="workflow:alpha/beta")

        assert scope == ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id="workflow_alpha_beta_ac_4",
            state_path=(
                "execution.workflows.workflow_alpha_beta."
                "acceptance_criteria.ac_4.implementation_session"
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
            aggregate_id="sub_ac_6_3",
            state_path=(
                "execution.acceptance_criteria.ac_6.sub_acs.sub_ac_3.implementation_session"
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
            aggregate_id="workflow_alpha_beta_sub_ac_6_3",
            state_path=(
                "execution.workflows.workflow_alpha_beta.acceptance_criteria."
                "ac_6.sub_acs.sub_ac_3.implementation_session"
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

    def test_node_identity_is_compact_while_display_path_is_1_based(self) -> None:
        root = ExecutionNodeIdentity.root(
            execution_context_id="exec_scope",
            ac_index=0,
        )
        child = root.child(0)
        grandchild = child.child(1)
        sibling_root = ExecutionNodeIdentity.root(
            execution_context_id="exec_scope",
            ac_index=1,
        )
        same_root = ExecutionNodeIdentity.root(
            execution_context_id="exec_scope",
            ac_index=0,
        )
        other_execution_root = ExecutionNodeIdentity.root(
            execution_context_id="other_exec_scope",
            ac_index=0,
        )

        assert root.node_id.startswith("node_")
        assert root.node_id == same_root.node_id
        assert root.node_id != other_execution_root.node_id
        assert root.legacy_node_id == "ac_0"
        assert root.legacy_node_aliases == ("ac_1",)
        assert root.parent_node_id is None
        assert root.display_path == "1"
        assert child.node_id.startswith("node_")
        assert child.parent_node_id == root.node_id
        assert child.legacy_parent_node_id == root.legacy_node_id
        assert "ac_1" in child.legacy_parent_node_aliases
        assert child.legacy_node_aliases == ("ac_1_1",)
        assert child.display_path == "1.1"
        assert grandchild.node_id.startswith("node_")
        assert grandchild.parent_node_id == child.node_id
        assert grandchild.display_path == "1.1.2"
        assert grandchild.root_ac_index == 0
        assert grandchild.node_id != sibling_root.node_id

    def test_node_identity_runtime_scope_avoids_synthetic_ac_index_collisions(self) -> None:
        root = ExecutionNodeIdentity.root(
            execution_context_id="workflow:alpha/beta",
            ac_index=0,
        )
        grandchild = root.child(0).child(1)
        synthetic_top_level_collision = ExecutionNodeIdentity.root(
            execution_context_id="workflow:alpha/beta",
            ac_index=10000,
        )

        grandchild_scope = build_ac_runtime_scope(
            10000,
            execution_context_id="workflow:alpha/beta",
            is_sub_ac=True,
            node_id=grandchild.node_id,
            node_path=grandchild.path,
        )
        top_level_scope = build_ac_runtime_scope(
            10000,
            execution_context_id="workflow:alpha/beta",
            node_id=synthetic_top_level_collision.node_id,
            node_path=synthetic_top_level_collision.path,
        )

        assert grandchild_scope.aggregate_id != top_level_scope.aggregate_id
        assert ".nodes." in grandchild_scope.state_path
        assert ".nodes." in top_level_scope.state_path
        top_level_identity = build_ac_runtime_identity(
            10000,
            execution_context_id="workflow:alpha/beta",
            node_identity=synthetic_top_level_collision,
        )
        assert "workflow_alpha_beta_ac_10001" in top_level_identity.legacy_session_scope_ids
        assert "workflow_alpha_beta_ac_10000" in top_level_identity.legacy_session_scope_ids


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
                aggregate_id="workflow_alpha_beta_ac_4",
                state_path=(
                    "execution.workflows.workflow_alpha_beta."
                    "acceptance_criteria.ac_4.implementation_session"
                ),
            ),
            ac_index=3,
        )
        assert identity.ac_id == "workflow_alpha_beta_ac_4"
        assert identity.session_scope_id == "workflow_alpha_beta_ac_4"
        assert identity.session_attempt_id == "workflow_alpha_beta_ac_4_attempt_1"
        assert identity.cache_key == identity.session_attempt_id
        assert identity.to_metadata() == {
            "ac_id": "workflow_alpha_beta_ac_4",
            "scope": "ac",
            "session_role": "implementation",
            "retry_attempt": 0,
            "attempt_number": 1,
            "session_scope_id": "workflow_alpha_beta_ac_4",
            "session_attempt_id": "workflow_alpha_beta_ac_4_attempt_1",
            "session_state_path": (
                "execution.workflows.workflow_alpha_beta."
                "acceptance_criteria.ac_4.implementation_session"
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
        assert first_attempt.session_attempt_id == "ac_4_attempt_1"
        assert retry_attempt.session_attempt_id == "ac_4_attempt_2"

    def test_node_aware_identity_uses_opaque_node_id_with_legacy_aliases(self) -> None:
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="workflow:alpha/beta",
            ac_index=0,
        )
        identity = build_ac_runtime_identity(
            0,
            execution_context_id="workflow:alpha/beta",
            node_identity=node_identity,
        )

        assert node_identity.node_id.startswith("node_")
        assert node_identity.legacy_node_id == "ac_0"
        assert identity.session_scope_id == f"workflow_alpha_beta_{node_identity.node_id}"
        assert (
            identity.session_state_path == f"execution.workflows.workflow_alpha_beta.nodes."
            f"{node_identity.node_id}.implementation_session"
        )
        assert "workflow_alpha_beta_ac_1" in identity.legacy_session_scope_ids
        assert "workflow_alpha_beta_ac_0" in identity.legacy_session_scope_ids
        assert identity.to_metadata()["node_id"] == node_identity.node_id
        assert identity.to_metadata()["legacy_node_id"] == "ac_0"
        assert identity.to_metadata()["legacy_node_aliases"] == ["ac_1"]
        assert identity.to_metadata()["node_kind"] == "ac"
        assert identity.to_metadata()["execution_id"] == "workflow:alpha/beta"

    def test_deep_sub_ac_identity_skips_unrepresentable_legacy_index_scope(self) -> None:
        grandchild_identity = (
            ExecutionNodeIdentity.root(
                execution_context_id="workflow:alpha/beta",
                ac_index=0,
            )
            .child(0)
            .child(1)
        )

        identity = build_ac_runtime_identity(
            10000,
            execution_context_id="workflow:alpha/beta",
            is_sub_ac=True,
            node_identity=grandchild_identity,
        )

        assert identity.session_scope_id == f"workflow_alpha_beta_{grandchild_identity.node_id}"
        assert identity.parent_ac_index is None
        assert identity.sub_ac_index is None
        assert identity.legacy_session_scope_ids == ()
        assert identity.legacy_session_state_paths == ()
        assert identity.to_metadata()["legacy_node_id"] == grandchild_identity.legacy_node_id

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
        assert identity.session_scope_id == "workflow_alpha_beta_sub_ac_6_3"
        assert identity.session_attempt_id == "workflow_alpha_beta_sub_ac_6_3_attempt_1"
        assert identity.to_metadata() == {
            "ac_id": "workflow_alpha_beta_sub_ac_6_3",
            "scope": "ac",
            "session_role": "implementation",
            "retry_attempt": 0,
            "attempt_number": 1,
            "session_scope_id": "workflow_alpha_beta_sub_ac_6_3",
            "session_attempt_id": "workflow_alpha_beta_sub_ac_6_3_attempt_1",
            "session_state_path": (
                "execution.workflows.workflow_alpha_beta.acceptance_criteria."
                "ac_6.sub_acs.sub_ac_3.implementation_session"
            ),
            "parent_ac_index": 5,
            "sub_ac_index": 2,
        }
