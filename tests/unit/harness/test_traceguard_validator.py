"""Tests for the deterministic in-repo TraceGuard validator (#978).

Two layers are exercised:

* Unit: :func:`validate_evidence_claims` classifies claims against a manifest by
  exact set membership, using the reason vocabulary ``deliver_routing`` routes on.
* End-to-end: the real :func:`evaluate_deliver_claim` gate is driven with the
  real validator injected, proving drop-in compatibility with the Protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ouroboros.harness.deliver_gate import (
    DeliverEvidenceClaim,
    DeliverEvidenceFact,
    TraceGuardEvidenceInput,
    evaluate_deliver_claim,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind, EvidenceManifest
from ouroboros.harness.traceguard_validator import (
    TraceGuardValidationResult,
    validate_evidence_claims,
)


def _manifest_input(
    *, fact_id: str, chunk_id: str, text: str = "evidence text"
) -> TraceGuardEvidenceInput:
    return TraceGuardEvidenceInput(fact_id=fact_id, chunk_id=chunk_id, text=text)


def _synthesis(*facts: dict[str, str]) -> dict[str, object]:
    return {"result": {"observed_facts": list(facts)}}


def _fact(fact_id: str, chunk_id: str, statement: str = "") -> dict[str, str]:
    return {"fact_id": fact_id, "chunk_id": chunk_id, "statement": statement}


class TestValidateEvidenceClaims:
    def test_supported_pair_is_accepted(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis=_synthesis(_fact("f1", "h1")),
        )

        assert result.accepted is True
        assert result.unsupported_claim_rate == 0.0
        assert result.rejected_claims == ()
        assert [(c.fact_id, c.chunk_id) for c in result.accepted_claims] == [("f1", "h1")]
        assert result.allowed_fact_ids == ("f1",)
        assert result.allowed_chunk_ids == ("h1",)

    def test_unsupported_fact_id_is_rejected(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis=_synthesis(_fact("ghost", "h1")),
        )

        assert result.accepted is False
        assert result.unsupported_claim_rate == 1.0
        assert len(result.rejected_claims) == 1
        rejection = result.rejected_claims[0]
        assert rejection.reason == "unsupported_fact_id"
        assert rejection.claim.fact_id == "ghost"
        assert rejection.claim.chunk_id == "h1"

    def test_wrong_handle_is_handle_mismatch(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis=_synthesis(_fact("f1", "h_wrong")),
        )

        assert result.accepted is False
        assert result.rejected_claims[0].reason == "evidence_handle_mismatch"

    def test_same_fact_under_two_handles_splits_accept_and_reject(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis=_synthesis(_fact("f1", "h1"), _fact("f1", "h2")),
        )

        assert result.accepted is False
        assert result.unsupported_claim_rate == 0.5
        assert [(c.fact_id, c.chunk_id) for c in result.accepted_claims] == [("f1", "h1")]
        assert result.rejected_claims[0].reason == "evidence_handle_mismatch"
        assert result.rejected_claims[0].claim.chunk_id == "h2"

    def test_chunk_handle_without_fact(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis={"result": {"observed_facts": [{"chunk_id": "h1"}]}},
        )

        assert result.accepted is False
        assert result.rejected_claims[0].reason == "chunk_handle_without_fact"
        assert result.rejected_claims[0].claim.fact_id is None
        assert result.rejected_claims[0].claim.chunk_id == "h1"

    def test_malformed_claim_missing_identifiers(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis={
                "result": {
                    "observed_facts": [
                        {"fact_id": "  "},
                        {"chunk_id": "unknown_chunk"},
                        {},
                        "not-a-dict",
                    ]
                }
            },
        )

        assert result.accepted is False
        assert result.unsupported_claim_rate == 1.0
        assert {rej.reason for rej in result.rejected_claims} == {"malformed_evidence_claim"}
        assert len(result.rejected_claims) == 4

    def test_no_claim_bearing_surface(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis={"result": {"observed_facts": []}},
        )

        assert result.accepted is False
        assert result.unsupported_claim_rate == 0.0
        assert len(result.rejected_claims) == 1
        assert result.rejected_claims[0].reason == "no_claim_bearing_surface"
        # Provenance still surfaces the manifest ids for gate fallbacks.
        assert result.allowed_fact_ids == ("f1",)
        assert result.allowed_chunk_ids == ("h1",)

    def test_missing_result_key_is_no_surface(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(),
            parent_synthesis={},
        )

        assert result.accepted is False
        assert result.rejected_claims[0].reason == "no_claim_bearing_surface"

    def test_empty_manifest_rejects_all_facts_as_unsupported(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(),
            parent_synthesis=_synthesis(_fact("f1", "h1")),
        )

        assert result.accepted is False
        assert result.unsupported_claim_rate == 1.0
        assert result.rejected_claims[0].reason == "unsupported_fact_id"
        assert result.allowed_fact_ids == ()
        assert result.allowed_chunk_ids == ()

    def test_duplicate_manifest_entries_are_deduped_in_provenance(self) -> None:
        entry = _manifest_input(fact_id="f1", chunk_id="h1")
        result = validate_evidence_claims(
            evidence_manifest=(entry, entry, _manifest_input(fact_id="f2", chunk_id="h2")),
            parent_synthesis=_synthesis(_fact("f1", "h1"), _fact("f2", "h2")),
        )

        assert result.accepted is True
        assert result.allowed_fact_ids == ("f1", "f2")
        assert result.allowed_chunk_ids == ("h1", "h2")

    def test_result_satisfies_result_like_surface(self) -> None:
        result = validate_evidence_claims(
            evidence_manifest=(_manifest_input(fact_id="f1", chunk_id="h1"),),
            parent_synthesis=_synthesis(_fact("f1", "h1")),
        )

        assert isinstance(result, TraceGuardValidationResult)
        # Surface the deliver gate reads off the result object.
        assert isinstance(result.accepted, bool)
        assert isinstance(result.unsupported_claim_rate, float)
        assert hasattr(result, "accepted_claims")
        assert hasattr(result, "rejected_claims")


def _entry(*, handle: str, ok: bool, source_event_ids: tuple[str, ...]) -> EvidenceEntry:
    return EvidenceEntry(
        handle=handle,
        kind=EvidenceKind.COMMAND_EXECUTED,
        ok=ok,
        started_at=datetime.now(UTC),
        payload={"tool_name": "Bash", "result_preview": f"result for {handle}"},
        source_event_ids=source_event_ids,
    )


class TestEndToEndThroughRealGate:
    """Drive the real deliver gate with the real validator injected."""

    def test_supported_claim_yields_accepted_verdict(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_entry(handle="ev_pass", ok=True, source_event_ids=("evt_1", "evt_2")),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="fact_admin_check",
                    evidence_handle="ev_pass",
                    statement="The command succeeded.",
                ),
            ),
        )

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=validate_evidence_claims,
        )

        assert verdict.accepted is True
        assert verdict.unsupported_claim_rate == 0.0
        assert verdict.accepted_fact_ids == ("fact_admin_check",)
        assert verdict.rejected_fact_ids == ()
        assert verdict.evidence_event_ids == ("evt_1", "evt_2")

    def test_unsupported_fact_yields_rejected_verdict_with_positive_rate(self) -> None:
        manifest = EvidenceManifest(
            ac_id="AC-1",
            entries=(_entry(handle="ev_actual", ok=True, source_event_ids=("evt_1",)),),
        )
        claim = DeliverEvidenceClaim(
            ac_id="AC-1",
            facts=(
                DeliverEvidenceFact(
                    fact_id="fact_missing",
                    evidence_handle="ev_missing",
                    statement="Unsupported claim.",
                ),
            ),
        )

        verdict = evaluate_deliver_claim(
            manifest,
            claim,
            traceguard_validator=validate_evidence_claims,
        )

        assert verdict.accepted is False
        assert verdict.unsupported_claim_rate > 0.0
        assert verdict.accepted_fact_ids == ()
        assert verdict.rejected_fact_ids == ("fact_missing",)
        assert "unsupported_fact_id" in {
            reason.split(":", 1)[0] for reason in verdict.rejected_reasons
        }
        assert verdict.evidence_event_ids == ()
