"""Tests for ouroboros.orchestrator.decomposition_policy (#1400)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ouroboros.orchestrator.decomposition_policy import (
    BounceCause,
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionProposal,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
    legacy_unverified_split_decision,
    parse_decomposition_proposal,
    redact_and_truncate_text,
    structural_decision_from_proposal,
    summarize_decomposition_trace,
    validate_decomposition_proposal,
)


def _proposal_payload() -> dict[str, object]:
    return {
        "children": [
            {
                "description": "Implement parser for persisted policy records",
                "coverage_claims": ["record schema is parsed"],
                "verification_hint": "unit tests cover round trip",
                "verify_command": "python -m pytest tests/unit/test_policy_parser.py -q",
            },
            {
                "description": "Implement invariant checks for trusted split decisions",
                "coverage_claims": ["trust invariant is enforced"],
                "verification_hint": "unit tests cover invalid trust",
                "expected_artifacts": ["reports/trust-invariants.json"],
            },
        ],
        "covers_parent": True,
        "rationale": "The parser and invariant logic are independently testable.",
    }


def _children() -> tuple[DecompositionChild, DecompositionChild]:
    return (
        DecompositionChild(
            description="Implement parser for persisted policy records",
            coverage_claims=("record schema is parsed",),
            verification_hint="unit tests cover round trip",
            verify_command="python -m pytest tests/unit/test_policy_parser.py -q",
        ),
        DecompositionChild(
            description="Implement invariant checks for trusted split decisions",
            coverage_claims=("trust invariant is enforced",),
            verification_hint="unit tests cover invalid trust",
            expected_artifacts=("reports/trust-invariants.json",),
        ),
    )


class TestEnumsAndSerialization:
    def test_direct_construction_rejects_string_enum_values(self) -> None:
        with pytest.raises(ValueError, match="source"):
            DecompositionDecisionRecord(
                node_id="n1",
                source="preflight",  # type: ignore[arg-type]
                disposition=DecompositionDisposition.UNKNOWN,
            )

    def test_from_dict_rejects_unknown_enum(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        ).to_dict()
        record["source"] = "runtime"
        assert DecompositionDecisionRecord.from_dict(record) is None

    def test_round_trip_preserves_versioned_record(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.BOUNCE,
            disposition=DecompositionDisposition.SPLIT,
            cause=BounceCause.TOO_BIG,
            reasons=("parent too broad",),
            evidence_refs=("trace:1",),
            children=_children(),
            structural_status=StructuralCheckStatus.PASSED,
            semantic_status=SemanticAttestationStatus.ESTABLISHED,
            repair_count=1,
            trustworthy=True,
        )
        restored = DecompositionDecisionRecord.from_dict(record.to_dict())
        assert restored == record
        assert record.to_dict()["schema_version"] == 1

    def test_unknown_schema_version_fails_closed(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        ).to_dict()
        record["schema_version"] = 2
        assert DecompositionDecisionRecord.from_dict(record) is None

    def test_boolean_schema_version_fails_closed(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        ).to_dict()
        record["schema_version"] = True
        assert DecompositionDecisionRecord.from_dict(record) is None

    def test_truthy_string_boolean_fails_closed(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        ).to_dict()
        record["trustworthy"] = "true"
        assert DecompositionDecisionRecord.from_dict(record) is None

    def test_records_are_frozen(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        )
        with pytest.raises(FrozenInstanceError):
            record.node_id = "n2"  # type: ignore[misc]


class TestTrustInvariant:
    def test_trust_requires_split_statuses_children_and_low_repair_count(self) -> None:
        with pytest.raises(ValueError, match="trustworthy split"):
            DecompositionDecisionRecord(
                node_id="n1",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=_children(),
                structural_status=StructuralCheckStatus.PASSED,
                semantic_status=SemanticAttestationStatus.ESTABLISHED,
                repair_count=2,
                trustworthy=True,
            )

    def test_trusted_split_accepts_repair_count_one(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.SPLIT,
            children=_children(),
            structural_status=StructuralCheckStatus.PASSED,
            semantic_status=SemanticAttestationStatus.ESTABLISHED,
            repair_count=1,
            trustworthy=True,
        )
        assert record.trustworthy is True

    def test_trusted_split_requires_child_coverage_and_verification(self) -> None:
        with pytest.raises(ValueError, match="trustworthy split"):
            DecompositionDecisionRecord(
                node_id="n1",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=(
                    DecompositionChild("child one"),
                    DecompositionChild("child two"),
                ),
                structural_status=StructuralCheckStatus.PASSED,
                semantic_status=SemanticAttestationStatus.ESTABLISHED,
                trustworthy=True,
            )

    def test_split_child_count_is_bounded_even_when_untrusted(self) -> None:
        with pytest.raises(ValueError, match="split decisions"):
            DecompositionDecisionRecord(
                node_id="n1",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=(DecompositionChild("only child"),),
            )

    def test_non_split_decision_rejects_children(self) -> None:
        with pytest.raises(ValueError, match="non-split"):
            DecompositionDecisionRecord(
                node_id="n1",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.ATOMIC,
                children=_children(),
            )

    def test_from_dict_fails_closed_for_invalid_trust(self) -> None:
        record = DecompositionDecisionRecord(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.UNKNOWN,
        ).to_dict()
        record["trustworthy"] = True
        assert DecompositionDecisionRecord.from_dict(record) is None


class TestProposalValidation:
    def test_valid_structural_proposal_parses_without_semantic_trust(self) -> None:
        proposal = parse_decomposition_proposal(
            _proposal_payload(),
            parent_text="Implement policy record parsing and trust invariants",
        )
        assert isinstance(proposal, DecompositionProposal)
        assert proposal.covers_parent is True

        decision = structural_decision_from_proposal(
            node_id="n1",
            source=DecompositionSource.PREFLIGHT,
            proposal=proposal,
        )
        assert decision.structural_status is StructuralCheckStatus.PASSED
        assert decision.semantic_status is SemanticAttestationStatus.NOT_RUN
        assert decision.trustworthy is False

    def test_rejects_duplicate_normalized_children(self) -> None:
        payload = _proposal_payload()
        children = payload["children"]
        assert isinstance(children, list)
        children[1] = {
            "description": "Implement  parser for persisted-policy records!",
            "coverage_claims": ["other coverage"],
            "verification_hint": "different hint",
        }
        assert parse_decomposition_proposal(payload) is None
        assert "duplicates another child" in " ".join(validate_decomposition_proposal(payload))

    def test_rejects_parent_echo(self) -> None:
        payload = _proposal_payload()
        parent = "Implement parser for persisted policy records"
        children = payload["children"]
        assert isinstance(children, list)
        children[0] = {
            "description": parent,
            "coverage_claims": ["record schema is parsed"],
            "verification_hint": "unit tests cover round trip",
        }
        assert parse_decomposition_proposal(payload, parent_text=parent) is None

    def test_rejects_duplicate_coverage_claims_across_children(self) -> None:
        payload = _proposal_payload()
        children = payload["children"]
        assert isinstance(children, list)
        second = children[1]
        assert isinstance(second, dict)
        second["coverage_claims"] = ["record schema is parsed"]
        assert parse_decomposition_proposal(payload) is None

    def test_rejects_missing_coverage_and_verification_hint(self) -> None:
        payload = _proposal_payload()
        children = payload["children"]
        assert isinstance(children, list)
        first = children[0]
        assert isinstance(first, dict)
        first["coverage_claims"] = []
        first["verification_hint"] = ""

        errors = validate_decomposition_proposal(payload)

        assert "child 0 must declare at least one coverage claim" in errors
        assert "child 0 verification_hint must be non-empty" in errors

    def test_child_executable_verify_contract_round_trips(self) -> None:
        proposal = parse_decomposition_proposal(_proposal_payload())
        assert isinstance(proposal, DecompositionProposal)

        first, second = proposal.children
        assert first.verify_command == "python -m pytest tests/unit/test_policy_parser.py -q"
        assert first.expected_artifacts == ()
        assert second.verify_command is None
        assert second.expected_artifacts == ("reports/trust-invariants.json",)
        assert DecompositionProposal.from_dict(proposal.to_dict()) == proposal

    def test_rejects_invalid_child_executable_contract_fields(self) -> None:
        payload = _proposal_payload()
        children = payload["children"]
        assert isinstance(children, list)
        first = children[0]
        assert isinstance(first, dict)
        first["verify_command"] = ""
        first["expected_artifacts"] = ["", "../" * 120]

        errors = validate_decomposition_proposal(payload)

        assert "child 0 verify_command must be non-empty" in errors
        assert "child 0 expected artifact 0 must be a non-empty string" in errors
        assert "child 0 expected artifact 1 is too large" in errors

    @pytest.mark.parametrize(
        ("children", "covers_parent"),
        [
            ([], True),
            (
                [
                    {
                        "description": f"Child {index}",
                        "coverage_claims": [f"claim {index}"],
                        "verification_hint": "hint",
                    }
                    for index in range(6)
                ],
                True,
            ),
            (_proposal_payload()["children"], False),
        ],
    )
    def test_rejects_bounds_and_explicit_uncovered_parent(
        self,
        children: object,
        covers_parent: bool,
    ) -> None:
        payload = {
            "children": children,
            "covers_parent": covers_parent,
            "rationale": "Split rationale",
        }
        assert parse_decomposition_proposal(payload) is None

    def test_rejects_oversized_child_and_payload_fields(self) -> None:
        payload = _proposal_payload()
        payload["rationale"] = "x" * 1_001
        children = payload["children"]
        assert isinstance(children, list)
        first = children[0]
        assert isinstance(first, dict)
        first["description"] = "x" * 501
        assert parse_decomposition_proposal(payload) is None


class TestLegacyAndTraceHelpers:
    def test_legacy_unverified_split_is_never_trustworthy(self) -> None:
        record = legacy_unverified_split_decision(
            node_id="n1",
            source=DecompositionSource.BOUNCE,
            child_descriptions=("first child", "second child"),
            cause=BounceCause.MODEL,
        )
        assert record.disposition is DecompositionDisposition.SPLIT
        assert record.structural_status is StructuralCheckStatus.NOT_RUN
        assert record.semantic_status is SemanticAttestationStatus.NOT_RUN
        assert record.trustworthy is False

    def test_secret_redaction_and_truncation(self) -> None:
        text = "token=abc123 " + ("x" * 80)
        redacted = redact_and_truncate_text(text, max_chars=40)
        assert "abc123" not in redacted
        assert "token=[REDACTED]" in redacted
        assert redacted.endswith("...[truncated]")
        assert len(redacted) == 40

    def test_secret_redaction_handles_json_and_bearer_values(self) -> None:
        redacted = redact_and_truncate_text(
            'failure={"token": "json-secret"} Authorization: Bearer bearer-secret',
            max_chars=200,
        )
        assert "json-secret" not in redacted
        assert "bearer-secret" not in redacted
        assert redacted.count("[REDACTED]") >= 2

    def test_trace_summary_bounds_and_redacts_evidence_refs(self) -> None:
        trace = summarize_decomposition_trace(
            "api_key=secret-value " + ("x" * 2_000),
            evidence_refs=("password=hunter2",),
            max_chars=60,
        )
        assert "secret-value" not in trace.summary
        assert trace.summary.endswith("...[truncated]")
        assert trace.evidence_refs == ("password=[REDACTED]",)

    def test_trace_summary_truncates_oversized_evidence_ref_sets(self) -> None:
        trace = summarize_decomposition_trace(
            "attempted_tools=Read",
            evidence_refs=tuple(f"ref-{index}: token={'x' * 300}" for index in range(12)),
        )
        assert len(trace.evidence_refs) == 8
        assert all(len(ref) <= 160 for ref in trace.evidence_refs)
        assert all("x" * 20 not in ref for ref in trace.evidence_refs)
