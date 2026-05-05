"""Unit tests for the ControlContract schema."""

import pytest

from ouroboros.core import ControlContract as CoreControlContract
from ouroboros.core.control_contract import (
    CONTROL_CONTRACT_SCHEMA_VERSION,
    ControlContract,
)
from ouroboros.core.directive import Directive


class TestControlContractRequiredFields:
    """Required identity and audit fields are enforced before event serialization."""

    def test_minimal_contract_derives_terminality(self) -> None:
        contract = ControlContract(
            target_type="lineage",
            target_id="lin_1",
            emitted_by="evolver",
            directive=Directive.UNSTUCK,
            reason="Stagnation detected.",
        )

        assert contract.schema_version == CONTROL_CONTRACT_SCHEMA_VERSION
        assert contract.is_terminal is False
        assert contract.to_event_data()["is_terminal"] is False

    @pytest.mark.parametrize("field", ["target_type", "target_id", "emitted_by", "reason"])
    def test_blank_required_fields_are_rejected(self, field: str) -> None:
        kwargs = {
            "target_type": "execution",
            "target_id": "exec_1",
            "emitted_by": "evaluator",
            "directive": Directive.RETRY,
            "reason": "Retry budget remains.",
        }
        kwargs[field] = "  "

        with pytest.raises(ValueError, match=field):
            ControlContract(**kwargs)

    def test_schema_version_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            ControlContract(
                schema_version=0,
                target_type="execution",
                target_id="exec_1",
                emitted_by="evaluator",
                directive=Directive.RETRY,
                reason="Retry budget remains.",
            )

    def test_directive_must_be_directive_member(self) -> None:
        with pytest.raises(TypeError, match="Directive"):
            ControlContract(
                target_type="execution",
                target_id="exec_1",
                emitted_by="evaluator",
                directive="retry",  # type: ignore[arg-type]
                reason="Retry budget remains.",
            )


class TestControlContractSerialization:
    """Stable event payload semantics for projectors and future mesh delivery."""

    def test_terminality_cannot_drift_from_directive(self) -> None:
        cancel = ControlContract(
            target_type="execution",
            target_id="exec_1",
            emitted_by="job_manager",
            directive=Directive.CANCEL,
            reason="User requested cancellation.",
        )

        assert cancel.is_terminal is True
        assert cancel.to_event_data()["is_terminal"] is True

    def test_optional_fields_are_serialized_when_present(self) -> None:
        contract = ControlContract(
            target_type="lineage",
            target_id="lin_1",
            emitted_by="evolver",
            directive=Directive.RETRY,
            reason="Reflect failed; retry budget remains.",
            phase="reflecting",
            session_id="sess_1",
            execution_id="exec_1",
            lineage_id="lin_1",
            generation_number=2,
            context_snapshot_id="snap_1",
            parent_directive_id="parent_evt_1",
            idempotency_key="lin_1:gen2:reflect:retry1",
            extra={"retry_budget_remaining": 1},
        )

        data = contract.to_event_data()

        assert data["schema_version"] == 1
        assert data["phase"] == "reflecting"
        assert data["session_id"] == "sess_1"
        assert data["execution_id"] == "exec_1"
        assert data["lineage_id"] == "lin_1"
        assert data["generation_number"] == 2
        assert data["context_snapshot_id"] == "snap_1"
        assert data["parent_directive_id"] == "parent_evt_1"
        assert data["idempotency_key"] == "lin_1:gen2:reflect:retry1"
        assert data["extra"] == {"retry_budget_remaining": 1}

    def test_idempotency_key_builds_effective_decision_key(self) -> None:
        contract = ControlContract(
            target_type="execution",
            target_id="exec_1",
            emitted_by="evaluator",
            directive=Directive.RETRY,
            reason="Stage 1 failed.",
            idempotency_key="stage1:retry:1",
        )

        assert contract.effective_idempotency_key == (
            "execution",
            "exec_1",
            "retry",
            "stage1:retry:1",
        )

    def test_missing_idempotency_key_preserves_legacy_row_semantics(self) -> None:
        contract = ControlContract(
            target_type="execution",
            target_id="exec_1",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="Checks passed.",
        )

        assert contract.effective_idempotency_key is None
        assert "idempotency_key" not in contract.to_event_data()


class TestControlContractCoreReExport:
    """ControlContract is exposed at the core package boundary."""

    def test_control_contract_importable_from_core(self) -> None:
        assert CoreControlContract is ControlContract
