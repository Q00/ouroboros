"""Behavioral examples for session-local calibration inference."""

from ouroboros.interview_calibration import infer_interview_calibration


def test_cannot_explain_pkce_infers_foundational() -> None:
    """'cannot explain PKCE' must trigger foundational level.

    Public SKILL.md example:
      ooo idk OAuth is familiar enough to implement, but I cannot explain PKCE.
    """
    calibration = infer_interview_calibration(
        "OAuth is familiar enough to implement, but I cannot explain PKCE"
    )
    assert calibration.level == "foundational"
    # PKCE should be extracted as an unknown term
    assert any("PKCE" in term for term in calibration.unknown_terms)


def test_cant_explain_shortform_infers_foundational() -> None:
    """'can't explain' contraction also triggers foundational level."""
    calibration = infer_interview_calibration("I can't explain how OAuth refresh tokens work")
    assert calibration.level == "foundational"
    assert any("OAuth" in term or "refresh" in term for term in calibration.unknown_terms)


def test_networking_and_operators_unfamiliar_infers_foundational() -> None:
    """'networking and operators are unfamiliar' must trigger foundational.

    Public SKILL.md example:
      ooo idk Kubernetes: deployed a tutorial once; networking and operators are unfamiliar.
    """
    calibration = infer_interview_calibration(
        "Kubernetes: deployed a tutorial once; networking and operators are unfamiliar"
    )
    assert calibration.level == "foundational"
    # The terms should include networking and/or operators
    terms_lower = [t.casefold() for t in calibration.unknown_terms]
    assert any("networking" in t for t in terms_lower) or any(
        "operators" in t for t in terms_lower
    ), f"Expected networking/operators in unknown_terms, got: {calibration.unknown_terms}"


def test_unfamiliar_with_still_works() -> None:
    """'unfamiliar with X' pattern (pre-existing) must continue to work."""
    calibration = infer_interview_calibration("I am unfamiliar with event sourcing and CQRS")
    assert calibration.level == "foundational"
    assert any("event sourcing" in t for t in calibration.unknown_terms) or any(
        "CQRS" in t for t in calibration.unknown_terms
    )


def test_cannot_explain_extracts_term() -> None:
    """'cannot explain X' should extract X as an unknown term."""
    calibration = infer_interview_calibration("I cannot explain PKCE")
    assert "PKCE" in calibration.unknown_terms


def test_mixed_known_unknown_high_confidence() -> None:
    """Mixed known + unknown evidence results in high confidence.

    Public example: 'I do not know idempotency or event sourcing. I have built REST APIs.'
    """
    calibration = infer_interview_calibration(
        "I do not know idempotency or event sourcing. I have built REST APIs."
    )
    assert calibration.level == "foundational"
    assert calibration.confidence == "high"
    assert "idempotency" in calibration.unknown_terms or any(
        "idempotency" in t for t in calibration.unknown_terms
    )


def test_calibration_meta_is_serializable_for_relay() -> None:
    """The calibration object serializes cleanly for meta transport."""
    calibration = infer_interview_calibration(
        "Kubernetes: deployed a tutorial once; networking and operators are unfamiliar"
    )
    dumped = calibration.model_dump(mode="json")
    assert isinstance(dumped, dict)
    assert dumped["level"] == "foundational"
    assert isinstance(dumped["unknown_terms"], list)
    # Verify round-trip via normalize
    from ouroboros.interview_calibration import normalize_interview_calibration

    restored = normalize_interview_calibration(dumped)
    assert restored is not None
    assert restored.level == calibration.level
    assert restored.unknown_terms == calibration.unknown_terms
