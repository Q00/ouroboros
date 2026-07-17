"""Tests for the auto bridge from requirement candidates to SeedDraftLedger."""

from __future__ import annotations

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.reference_candidate_bridge import (
    REFERENCE_CONFIRMATION_REQUIRED,
    apply_requirement_distillation_to_ledger,
)
from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    RequirementCandidate,
    RequirementDistillation,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
    compute_requirement_input_fingerprint,
)


def _fingerprint() -> str:
    return compute_requirement_input_fingerprint({"transcript": "reference bridge fixture"})


def _evidence(
    evidence_id: str,
    kind: RequirementEvidenceKind,
    *,
    reference_id: str | None = None,
) -> RequirementEvidence:
    return RequirementEvidence(
        evidence_id=evidence_id,
        kind=kind,
        text="evidence",
        reference_id=reference_id,
    )


def _candidate(**overrides: object) -> RequirementCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "section": RequirementSection.ACCEPTANCE_CRITERION,
        "text": "Keyboard navigation works for the command menu.",
        "content_source": CandidateContentSource.USER_STATED,
        "resolution": CandidateResolution.CONFIRMED,
        "confirmation_authority": ConfirmationAuthority.USER,
        "evidence_ids": ("user-1",),
        "required": True,
    }
    values.update(overrides)
    return RequirementCandidate.model_validate(values)


def _distillation(
    candidate: RequirementCandidate,
    evidence: tuple[RequirementEvidence, ...],
) -> RequirementDistillation:
    return RequirementDistillation(
        candidates=(candidate,),
        evidence=evidence,
        input_fingerprint=_fingerprint(),
    )


def _entries(ledger: SeedDraftLedger) -> list[LedgerEntry]:
    return [entry for section in ledger.sections.values() for entry in section.entries]


def test_confirmed_user_authorized_reference_maps_with_truthful_authority() -> None:
    ledger = SeedDraftLedger()
    candidate = _candidate(
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        reference_ids=("linear",),
    )
    distillation = _distillation(
        candidate,
        (
            _evidence(
                "user-1",
                RequirementEvidenceKind.REFERENCE_CONTRAST,
                reference_id="linear",
            ),
        ),
    )

    result = apply_requirement_distillation_to_ledger(distillation, ledger)

    assert result.applied_candidate_ids == ("candidate-1",)
    assert result.omitted_candidate_ids == ()
    assert result.blockers == ()
    entry = ledger.sections["acceptance_criteria"].entries[0]
    assert entry.value == "Keyboard navigation works for the command menu."
    assert entry.source is LedgerSource.USER_PREFERENCE
    assert entry.provenance is DecisionProvenance.USER_CONFIRMED
    assert "reference:linear" in entry.evidence
    assert "source=reference_derived" in entry.rationale


def test_unconfirmed_reference_candidate_is_not_mapped_to_repo_or_convention() -> None:
    ledger = SeedDraftLedger()
    candidate = _candidate(
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=("linear",),
        required=False,
    )
    distillation = _distillation(
        candidate,
        (
            _evidence(
                "user-1",
                RequirementEvidenceKind.REFERENCE_CUE,
                reference_id="linear",
            ),
        ),
    )

    result = apply_requirement_distillation_to_ledger(distillation, ledger)

    assert result.applied_candidate_ids == ()
    assert result.omitted_candidate_ids == ("candidate-1",)
    assert result.omitted_candidates[0].content_source == "reference_derived"
    assert result.omitted_candidates[0].reference_ids == ("linear",)
    assert not _entries(ledger)
    assert all(
        entry.source not in {LedgerSource.REPO_FACT, LedgerSource.EXISTING_CONVENTION}
        for entry in _entries(ledger)
    )


def test_required_reference_only_contract_material_blocks_for_confirmation() -> None:
    ledger = SeedDraftLedger()
    candidate = _candidate(
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=("linear",),
        required=True,
    )
    distillation = _distillation(
        candidate,
        (
            _evidence(
                "user-1",
                RequirementEvidenceKind.REFERENCE_CONTRAST,
                reference_id="linear",
            ),
        ),
    )

    result = apply_requirement_distillation_to_ledger(distillation, ledger)

    assert result.applied_candidate_ids == ()
    assert result.omitted_candidate_ids == ("candidate-1",)
    assert result.blockers[0].code == REFERENCE_CONFIRMATION_REQUIRED
    assert result.blockers[0].reference_ids == ("linear",)
    assert not _entries(ledger)


def test_optional_reference_candidate_stays_outside_contract_metadata_only() -> None:
    ledger = SeedDraftLedger()
    candidate = _candidate(
        candidate_id="optional-reference",
        section=RequirementSection.CONTEXT,
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=("linear",),
        required=False,
    )
    distillation = _distillation(
        candidate,
        (
            _evidence(
                "user-1",
                RequirementEvidenceKind.REFERENCE_CUE,
                reference_id="linear",
            ),
        ),
    )

    result = apply_requirement_distillation_to_ledger(distillation, ledger)

    assert result.is_ready
    assert result.omitted_candidate_ids == ("optional-reference",)
    assert result.omitted_candidates[0].required is False
    assert result.omitted_candidates[0].reason == "optional_unconfirmed"
    assert not _entries(ledger)


def test_existing_clean_model_inferred_ledger_behavior_is_unchanged() -> None:
    ledger = SeedDraftLedger()
    ledger.add_entry(
        "acceptance_criteria",
        LedgerEntry(
            key="acceptance.clean_model_inferred",
            value="CLI exits with code 0 and prints stable output.",
            source=LedgerSource.INFERENCE,
            confidence=0.7,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    candidate = _candidate(
        candidate_id="optional-model",
        content_source=CandidateContentSource.MODEL_INFERRED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        evidence_ids=("model-1",),
        required=False,
    )
    distillation = _distillation(
        candidate,
        (_evidence("model-1", RequirementEvidenceKind.MODEL_INFERENCE),),
    )

    result = apply_requirement_distillation_to_ledger(distillation, ledger)

    assert result.is_ready
    assert result.omitted_candidate_ids == ("optional-model",)
    [existing_entry] = ledger.sections["acceptance_criteria"].entries
    assert existing_entry.source is LedgerSource.INFERENCE
    assert existing_entry.provenance is None
    assert existing_entry.effective_provenance is DecisionProvenance.MODEL_INFERRED
