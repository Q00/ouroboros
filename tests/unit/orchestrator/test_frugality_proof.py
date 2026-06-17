"""The deterministic frugality-proof machine: assembly + the PASS/FAIL gate."""

from __future__ import annotations

from ouroboros.orchestrator.frugality_proof import (
    EVENT_DELIVER_VERDICT,
    EVENT_EFFORT_ROUTED,
    EVENT_SHADOW_REPLAY,
    EVENT_TOKEN_ATTRIBUTION,
    FrugalityTriadRow,
    ProofStatus,
    assemble_triads,
    evaluate_proof,
)


def _evt(etype: str, **data) -> dict:
    return {"type": etype, "data": data}


def _full_row(ac_id: str, *, run: str, token: float, baseline: float, regression: bool = False):
    return FrugalityTriadRow(
        ac_id=ac_id,
        seed_run_id=run,
        is_decomposed_child=True,
        decomposition_trustworthy=True,
        effort_level="medium",
        effort_mode="enforced",
        token_spend=token,
        baseline_token_spend=baseline,
        baseline_mode="shadow_replay",
        traceguard_verdict="accepted",
        unsupported_claim_rate=0.0,
        grounding_regression=regression,
    )


class TestAssembleTriads:
    def test_joins_all_axes_by_ac_id(self) -> None:
        events = [
            _evt(
                EVENT_EFFORT_ROUTED,
                ac_id="ac1",
                effort_level="medium",
                effort_mode="enforced",
                is_decomposed_child=True,
                seed_run_id="r1",
            ),
            _evt(EVENT_TOKEN_ATTRIBUTION, ac_id="ac1", token_spend=80.0),
            _evt(
                EVENT_SHADOW_REPLAY,
                ac_id="ac1",
                baseline_token_spend=100.0,
                baseline_mode="shadow_replay",
                decomposition_trustworthy=True,
            ),
            _evt(
                EVENT_DELIVER_VERDICT,
                ac_id="ac1",
                traceguard_verdict="accepted",
                unsupported_claim_rate=0.0,
                grounding_regression=False,
            ),
        ]
        rows = assemble_triads(events)
        assert len(rows) == 1
        r = rows[0]
        assert r.effort_mode == "enforced" and r.effort_level == "medium"
        assert r.token_spend == 80.0 and r.baseline_token_spend == 100.0
        assert r.grounding_regression is False
        assert r.has_all_axes and r.counts_in_proof

    def test_effort_only_row_does_not_count(self) -> None:
        rows = assemble_triads(
            [
                _evt(EVENT_EFFORT_ROUTED, ac_id="ac1", effort_level="high", effort_mode="enforced"),
            ]
        )
        assert rows[0].is_enforced
        assert not rows[0].has_all_axes
        assert not rows[0].counts_in_proof  # token/grounding/baseline missing

    def test_advised_row_never_counts(self) -> None:
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        advised = FrugalityTriadRow(**{**r.__dict__, "effort_mode": "advised"})
        assert not advised.counts_in_proof

    def test_untrustworthy_decomposition_never_counts(self) -> None:
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        quarantined = FrugalityTriadRow(**{**r.__dict__, "decomposition_trustworthy": False})
        assert not quarantined.counts_in_proof

    def test_event_without_ac_id_is_skipped(self) -> None:
        assert assemble_triads([_evt(EVENT_EFFORT_ROUTED, effort_level="high")]) == []


class TestEvaluateProof:
    def test_insufficient_data_when_only_effort_axis(self) -> None:
        rows = assemble_triads(
            [
                _evt(
                    EVENT_EFFORT_ROUTED, ac_id=f"ac{i}", effort_level="high", effort_mode="enforced"
                )
                for i in range(30)
            ]
        )
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert not v.passed

    def test_pass(self) -> None:
        rows = [_full_row(f"ac{i}", run=f"r{i % 3}", token=80, baseline=100) for i in range(21)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.PASS
        assert v.token_reduction_pct == 20.0
        assert v.runs == 3 and v.counted_rows == 21

    def test_grounding_regression_is_a_veto(self) -> None:
        rows = [_full_row(f"ac{i}", run=f"r{i % 3}", token=80, baseline=100) for i in range(21)]
        rows[5] = _full_row("ac5", run="r2", token=80, baseline=100, regression=True)
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.FAIL_GROUNDING_REGRESSION
        assert v.grounding_regressions == 1

    def test_insufficient_sample(self) -> None:
        rows = [_full_row(f"ac{i}", run="r1", token=80, baseline=100) for i in range(5)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_SAMPLE  # < 20 rows, 1 run

    def test_no_frugality_when_reduction_below_bar(self) -> None:
        rows = [_full_row(f"ac{i}", run=f"r{i % 3}", token=95, baseline=100) for i in range(21)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.FAIL_NO_FRUGALITY  # 5% < 10%
        assert v.token_reduction_pct == 5.0

    def test_grounding_veto_precedes_sample_and_frugality(self) -> None:
        # A single regressing row fails even with a tiny sample — safety first.
        rows = [_full_row("ac1", run="r1", token=80, baseline=100, regression=True)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.FAIL_GROUNDING_REGRESSION
