"""Tests for the derived pre-Seed requirement candidate contract."""

from pydantic import ValidationError as PydanticValidationError
import pytest

from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    PromotionDisposition,
    RequirementCandidate,
    RequirementDistillation,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
    compute_requirement_input_fingerprint,
    evaluate_promotion,
    validate_candidate_lineage,
)


def _fingerprint() -> str:
    return compute_requirement_input_fingerprint({"transcript": "Q: goal\nA: build it"})


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
        "text": "Reviewers can approve a change.",
        "content_source": CandidateContentSource.USER_STATED,
        "resolution": CandidateResolution.CONFIRMED,
        "confirmation_authority": ConfirmationAuthority.USER,
        "evidence_ids": ("user-1",),
        "required": True,
    }
    values.update(overrides)
    return RequirementCandidate.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_source", "not-a-source"),
        ("resolution", "not-a-resolution"),
        ("confirmation_authority", "not-an-authority"),
        ("section", "not-a-section"),
    ],
)
def test_candidate_enums_reject_unknown_values(field: str, value: str) -> None:
    values = _candidate().model_dump()
    values[field] = value
    with pytest.raises(PydanticValidationError):
        RequirementCandidate.model_validate(values)


@pytest.mark.parametrize(
    ("candidate", "prefix"),
    [
        (_candidate(), "[USER_STATED]"),
        (
            _candidate(
                content_source=CandidateContentSource.REFERENCE_DERIVED,
                reference_ids=("reference-1",),
            ),
            "[REFERENCE_DERIVED]",
        ),
        (
            _candidate(content_source=CandidateContentSource.MODEL_INFERRED),
            "[MODEL_INFERRED]",
        ),
        (
            _candidate(
                resolution=CandidateResolution.UNKNOWN,
                confirmation_authority=ConfirmationAuthority.NONE,
            ),
            "[UNKNOWN]",
        ),
        (
            _candidate(
                resolution=CandidateResolution.CONFLICTING,
                confirmation_authority=ConfirmationAuthority.NONE,
            ),
            "[CONFLICT]",
        ),
    ],
)
def test_display_prefix_renders_from_structured_axes(
    candidate: RequirementCandidate,
    prefix: str,
) -> None:
    assert candidate.display_prefix == prefix
    assert candidate.render().startswith(prefix)


def test_user_confirmation_preserves_reference_source() -> None:
    candidate = _candidate(
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=("reference-1",),
    )

    confirmed = candidate.confirm_by_user()

    assert confirmed.content_source is CandidateContentSource.REFERENCE_DERIVED
    assert confirmed.resolution is CandidateResolution.CONFIRMED
    assert confirmed.confirmation_authority is ConfirmationAuthority.USER


def test_lineage_overrides_model_self_label_for_reference_evidence() -> None:
    candidate = _candidate(content_source=CandidateContentSource.USER_STATED)
    evidence = (
        _evidence(
            "user-1",
            RequirementEvidenceKind.REFERENCE_CONTRAST,
            reference_id="reference-1",
        ),
    )

    lineage = validate_candidate_lineage(candidate, evidence)

    assert lineage.valid
    assert lineage.candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
    assert lineage.candidate.reference_ids == ("reference-1",)


def test_missing_evidence_lineage_blocks_required_candidate() -> None:
    distillation = RequirementDistillation(
        candidates=(_candidate(evidence_ids=("missing",)),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert not result.is_ready_for_seed
    assert result.blockers[0].reason == "invalid_evidence_lineage"


def test_user_stated_confirmed_candidate_promotes() -> None:
    distillation = RequirementDistillation(
        candidates=(_candidate(),),
        evidence=(_evidence("user-1", RequirementEvidenceKind.USER_STATEMENT),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert result.is_ready_for_seed
    assert result.promoted[0].candidate_id == "candidate-1"


@pytest.mark.parametrize(
    "source_kind",
    [
        RequirementEvidenceKind.REFERENCE_CUE,
        RequirementEvidenceKind.MODEL_INFERENCE,
    ],
)
def test_reference_and_model_candidates_require_user_confirmation(
    source_kind: RequirementEvidenceKind,
) -> None:
    reference_id = "reference-1" if source_kind is RequirementEvidenceKind.REFERENCE_CUE else None
    candidate = _candidate(
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(_evidence("user-1", source_kind, reference_id=reference_id),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert not result.is_ready_for_seed
    assert result.blockers[0].reason == "confirmation_required"


def test_confirmed_reference_candidate_promotes_without_source_rewrite() -> None:
    candidate = _candidate(
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        reference_ids=("reference-1",),
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(
            _evidence(
                "user-1",
                RequirementEvidenceKind.REFERENCE_CONTRAST,
                reference_id="reference-1",
            ),
        ),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert result.is_ready_for_seed
    assert result.promoted[0].content_source is CandidateContentSource.REFERENCE_DERIVED


def test_repo_observed_context_promotes_with_repo_evidence_authority() -> None:
    candidate = _candidate(
        section=RequirementSection.EXISTING_CONSTRAINT,
        content_source=CandidateContentSource.REPO_OBSERVED,
        confirmation_authority=ConfirmationAuthority.REPO_EVIDENCE,
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(_evidence("user-1", RequirementEvidenceKind.REPO_EVIDENCE),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert result.is_ready_for_seed
    assert result.promoted == (result.decisions[0].candidate,)


def test_repo_observed_acceptance_criterion_requires_user_authority() -> None:
    candidate = _candidate(
        content_source=CandidateContentSource.REPO_OBSERVED,
        confirmation_authority=ConfirmationAuthority.REPO_EVIDENCE,
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(_evidence("user-1", RequirementEvidenceKind.REPO_EVIDENCE),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert not result.is_ready_for_seed
    assert result.blockers[0].reason == "user_confirmation_required"


def test_required_unknown_and_conflict_block_seed() -> None:
    candidates = (
        _candidate(
            candidate_id="unknown",
            resolution=CandidateResolution.UNKNOWN,
            confirmation_authority=ConfirmationAuthority.NONE,
        ),
        _candidate(
            candidate_id="conflict",
            resolution=CandidateResolution.CONFLICTING,
            confirmation_authority=ConfirmationAuthority.NONE,
        ),
    )
    distillation = RequirementDistillation(
        candidates=candidates,
        evidence=(_evidence("user-1", RequirementEvidenceKind.USER_STATEMENT),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert {decision.reason for decision in result.blockers} == {
        "required_unknown",
        "conflict_requires_tradeoff",
    }


def test_optional_unconfirmed_candidate_is_omitted() -> None:
    candidate = _candidate(
        required=False,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
    )
    distillation = RequirementDistillation(
        candidates=(candidate,),
        evidence=(_evidence("user-1", RequirementEvidenceKind.MODEL_INFERENCE),),
        input_fingerprint=_fingerprint(),
    )

    result = evaluate_promotion(distillation)

    assert result.is_ready_for_seed
    assert result.decisions[0].disposition is PromotionDisposition.OMIT


def test_fingerprint_is_deterministic_for_equivalent_mapping_order() -> None:
    left = compute_requirement_input_fingerprint(
        {"transcript": "same", "references": [{"id": "r1", "label": "Linear"}]}
    )
    right = compute_requirement_input_fingerprint(
        {"references": [{"label": "Linear", "id": "r1"}], "transcript": "same"}
    )

    assert left == right


def test_distillation_cache_validity_checks_schema_revision_and_fingerprint() -> None:
    fingerprint = _fingerprint()
    distillation = RequirementDistillation(
        input_revision=2,
        input_fingerprint=fingerprint,
    )

    assert distillation.is_current(input_revision=2, input_fingerprint=fingerprint)
    assert not distillation.is_current(input_revision=3, input_fingerprint=fingerprint)
    assert not distillation.is_current(input_revision=2, input_fingerprint="0" * 64)


def test_candidate_and_distillation_are_frozen() -> None:
    candidate = _candidate()
    distillation = RequirementDistillation(input_fingerprint=_fingerprint())

    with pytest.raises(PydanticValidationError):
        candidate.text = "changed"  # type: ignore[misc]
    with pytest.raises(PydanticValidationError):
        distillation.input_revision = 5  # type: ignore[misc]
