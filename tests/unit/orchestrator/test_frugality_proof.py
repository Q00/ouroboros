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
            _evt(EVENT_TOKEN_ATTRIBUTION, ac_id="ac1", seed_run_id="r1", token_spend=80.0),
            _evt(
                EVENT_SHADOW_REPLAY,
                ac_id="ac1",
                seed_run_id="r1",
                baseline_token_spend=100.0,
                baseline_mode="shadow_replay",
                decomposition_trustworthy=True,
            ),
            _evt(
                EVENT_DELIVER_VERDICT,
                ac_id="ac1",
                seed_run_id="r1",
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

    def test_top_level_non_decomposed_row_never_counts(self) -> None:
        """A fully-measured, enforced, trustworthy TOP-LEVEL AC must not count.

        The hypothesis is about decomposed children running at lower effort, so a
        top-level unit (is_decomposed_child=False) — including every per-AC event
        the parallel executor emits for non-decomposed work and the whole-seed
        direct-runner path — is excluded even when all axes are present. Otherwise
        a sample of ordinary top-level executions could falsely PASS the gate.
        """
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        top_level = FrugalityTriadRow(**{**r.__dict__, "is_decomposed_child": False})
        assert top_level.has_all_axes  # measurement is complete...
        assert not top_level.counts_in_proof  # ...but it is the wrong unit class

    def test_event_without_ac_id_is_skipped(self) -> None:
        assert assemble_triads([_evt(EVENT_EFFORT_ROUTED, effort_level="high")]) == []

    def test_whole_seed_runner_effort_event_is_excluded_by_design(self) -> None:
        # The direct-runner effort event (OrchestratorRunner._route_call_effort) is
        # whole-seed: it carries execution_id/session_id but no per-AC ac_id, because
        # a non-decomposed single-call run has no child to lower effort on and no
        # shadow-replay baseline. It is intentionally excluded from the per-AC proof
        # rather than counted as a missing-axis row.
        runner_effort = _evt(
            EVENT_EFFORT_ROUTED,
            execution_id="exec_direct",
            session_id="sess_direct",
            effort_level="high",
            effort_mode="enforced",
            is_decomposed_child=False,
        )
        # Even joined with a real per-AC triad, the whole-seed event contributes no row.
        rows = assemble_triads(
            [
                runner_effort,
                _evt(
                    EVENT_EFFORT_ROUTED,
                    ac_id="ac1",
                    seed_run_id="r1",
                    effort_level="low",
                    effort_mode="enforced",
                    is_decomposed_child=True,
                ),
                _evt(EVENT_TOKEN_ATTRIBUTION, ac_id="ac1", seed_run_id="r1", token_spend=80.0),
                _evt(
                    EVENT_SHADOW_REPLAY,
                    ac_id="ac1",
                    seed_run_id="r1",
                    baseline_token_spend=100.0,
                    baseline_mode="shadow_replay",
                    decomposition_trustworthy=True,
                ),
                _evt(
                    EVENT_DELIVER_VERDICT,
                    ac_id="ac1",
                    seed_run_id="r1",
                    traceguard_verdict="accepted",
                    unsupported_claim_rate=0.0,
                    grounding_regression=False,
                ),
            ]
        )
        assert len(rows) == 1  # only the per-AC row; the whole-seed event added none
        assert rows[0].ac_id == "ac1"

    def test_same_ac_id_across_runs_stays_distinct(self) -> None:
        # Regression: the proof spans runs, and the same logical AC id recurs every
        # run. Keying by ac_id alone collapsed all runs into the last; keying by
        # (run, ac_id) keeps one row per run so min_runs can be satisfied.
        events: list[dict] = []
        for run in ("r1", "r2", "r3"):
            for ac in ("ac1", "ac2"):
                events += [
                    _evt(
                        EVENT_EFFORT_ROUTED,
                        ac_id=ac,
                        seed_run_id=run,
                        effort_level="low",
                        effort_mode="enforced",
                        is_decomposed_child=True,
                    ),
                    _evt(EVENT_TOKEN_ATTRIBUTION, ac_id=ac, seed_run_id=run, token_spend=80.0),
                    _evt(
                        EVENT_SHADOW_REPLAY,
                        ac_id=ac,
                        seed_run_id=run,
                        baseline_token_spend=100.0,
                        baseline_mode="shadow_replay",
                        decomposition_trustworthy=True,
                    ),
                    _evt(
                        EVENT_DELIVER_VERDICT,
                        ac_id=ac,
                        seed_run_id=run,
                        traceguard_verdict="accepted",
                        unsupported_claim_rate=0.0,
                        grounding_regression=False,
                    ),
                ]
        rows = assemble_triads(events)
        assert len(rows) == 6  # 2 ACs x 3 runs, not collapsed to 2
        assert {r.seed_run_id for r in rows} == {"r1", "r2", "r3"}
        assert all(r.counts_in_proof for r in rows)
        v = evaluate_proof(rows, min_triads=6, min_runs=3)
        assert v.status is ProofStatus.PASS
        assert v.runs == 3 and v.counted_rows == 6

    def test_execution_id_used_as_run_anchor_when_no_seed_run_id(self) -> None:
        # The effort event carries execution_id even before seed_run_id is wired;
        # it serves as the run anchor so two executions of the same AC stay distinct.
        events = [
            _evt(EVENT_EFFORT_ROUTED, ac_id="ac1", execution_id="exec_a", effort_level="high"),
            _evt(EVENT_EFFORT_ROUTED, ac_id="ac1", execution_id="exec_b", effort_level="high"),
        ]
        rows = assemble_triads(events)
        assert len(rows) == 2
        assert {r.seed_run_id for r in rows} == {"exec_a", "exec_b"}


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

    def test_top_level_rows_alone_never_pass(self) -> None:
        """21 fully-measured TOP-LEVEL rows must NOT PASS — they are the wrong unit.

        Reproduces the reviewer's case directly: a sample built entirely from
        enforced, fully-measured non-decomposed AC rows would otherwise satisfy the
        gate and falsely prove frugality for ordinary top-level execution. With
        counts_in_proof requiring a decomposed child, none of them count, so the
        gate honestly returns INSUFFICIENT_DATA.
        """
        rows = [
            FrugalityTriadRow(
                **{
                    **_full_row(f"ac{i}", run=f"r{i % 3}", token=80, baseline=100).__dict__,
                    "is_decomposed_child": False,
                }
            )
            for i in range(21)
        ]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert not v.passed
        assert v.counted_rows == 0

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

    def test_zero_baseline_rows_do_not_count(self) -> None:
        # A non-positive shadow-replay baseline is not a usable measurement; such
        # rows are excluded (has_all_axes is False) rather than counted.
        row = _full_row("ac1", run="r1", token=80, baseline=0.0)
        assert row.has_all_axes is False
        assert row.counts_in_proof is False

    def test_zero_baseline_proof_does_not_crash(self) -> None:
        # Regression: counted rows with a zero aggregate baseline must yield a
        # deterministic verdict, not raise TypeError formatting a None reduction.
        rows = [_full_row(f"ac{i}", run=f"r{i % 3}", token=80, baseline=0.0) for i in range(21)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert v.token_reduction_pct is None
        assert not v.passed
