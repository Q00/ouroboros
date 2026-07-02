"""Tests for the cross-harness redispatch decision + taxonomy (PR-X X1)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator import cross_harness_redispatch as chr
from ouroboros.orchestrator.failure_taxonomy import (
    FailureClass,
    RecoveryAction,
    alt_harness_policy,
)


class TestAltHarnessPolicy:
    def test_fabrication_maps_to_alt_harness(self) -> None:
        policy = alt_harness_policy(FailureClass.FABRICATION_SUSPECTED)
        assert policy is not None
        assert policy.action is RecoveryAction.REDISPATCH_ALT_HARNESS

    def test_stall_needs_exhaustion(self) -> None:
        assert alt_harness_policy(FailureClass.STALL) is None
        policy = alt_harness_policy(FailureClass.STALL, stall_retries_exhausted=True)
        assert policy is not None
        assert policy.action is RecoveryAction.REDISPATCH_ALT_HARNESS

    def test_transient_exhaustion_maps_regardless_of_class(self) -> None:
        policy = alt_harness_policy(None, transient_exhausted=True)
        assert policy is not None
        assert policy.action is RecoveryAction.REDISPATCH_ALT_HARNESS

    def test_unrelated_class_yields_none(self) -> None:
        assert alt_harness_policy(FailureClass.EVIDENCE_MISSING) is None
        assert alt_harness_policy(FailureClass.BLOCKED) is None


class TestTransientDetection:
    @pytest.mark.parametrize(
        "error",
        [
            "Anthropic 529 overloaded_error after 3 retries",
            "HTTP 429 Too Many Requests",
            "provider rate limit exceeded",
        ],
    )
    def test_positive_markers(self, error: str) -> None:
        assert chr.looks_transient_exhausted(error) is True

    def test_negative_and_none(self) -> None:
        assert chr.looks_transient_exhausted(None) is False
        assert chr.looks_transient_exhausted("Stalled (no activity for 90s)") is False


class TestDecision:
    def _weights(self) -> dict[str, float]:
        return {}

    def test_disabled_by_config(self) -> None:
        decision = chr.decide_alt_harness_redispatch(
            enabled=False,
            from_backend="claude",
            failure=FailureClass.FABRICATION_SUSPECTED,
            already_redispatched=False,
        )
        assert decision.should_redispatch is False
        assert decision.reason == "disabled_by_config"

    def test_cap_blocks_second_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")
        decision = chr.decide_alt_harness_redispatch(
            enabled=True,
            from_backend="claude",
            failure=FailureClass.FABRICATION_SUSPECTED,
            already_redispatched=True,
        )
        assert decision.should_redispatch is False
        assert decision.reason == "alt_harness_cap_reached"

    def test_ineligible_failure_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")
        decision = chr.decide_alt_harness_redispatch(
            enabled=True,
            from_backend="claude",
            failure=FailureClass.EVIDENCE_MISSING,
            already_redispatched=False,
        )
        assert decision.should_redispatch is False
        assert decision.reason == "failure_class_not_eligible"

    def test_no_alternative_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: None)
        decision = chr.decide_alt_harness_redispatch(
            enabled=True,
            from_backend="claude",
            failure=FailureClass.FABRICATION_SUSPECTED,
            already_redispatched=False,
        )
        assert decision.should_redispatch is False
        assert decision.reason == "no_alternative_runtime"

    def test_positive_decision_selects_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _fake_pick(failed: str, *, exclude=None, weights=None):  # type: ignore[no-untyped-def]
            captured["failed"] = failed
            captured["exclude"] = exclude
            return "codex"

        monkeypatch.setattr(chr, "pick_alternative_runtime", _fake_pick)
        decision = chr.decide_alt_harness_redispatch(
            enabled=True,
            from_backend="claude",
            failure=FailureClass.STALL,
            already_redispatched=False,
            stall_retries_exhausted=True,
        )
        assert decision.should_redispatch is True
        assert decision.to_backend == "codex"
        assert decision.from_backend == "claude"
        assert decision.failure_action is RecoveryAction.REDISPATCH_ALT_HARNESS
        # The failed backend is always excluded from its own replacement search.
        assert "claude" in (captured["exclude"] or set())


class TestEvent:
    def test_event_records_from_and_to(self) -> None:
        decision = chr.AltHarnessDecision(
            should_redispatch=True,
            from_backend="claude",
            to_backend="codex",
            policy=alt_harness_policy(FailureClass.FABRICATION_SUSPECTED),
            reason="alt_harness_redispatch_selected",
        )
        event = chr.create_alt_harness_redispatch_event(
            session_id="s1",
            ac_index=2,
            ac_id="ac-2",
            execution_id="e1",
            decision=decision,
            failure_class="FABRICATION_SUSPECTED",
        )
        assert event.type == chr.ALT_HARNESS_REDISPATCH_EVENT
        assert event.data["from_backend"] == "claude"
        assert event.data["to_backend"] == "codex"
        assert event.data["failure_class"] == "FABRICATION_SUSPECTED"
        assert event.data["recovery_action"] == RecoveryAction.REDISPATCH_ALT_HARNESS.value
