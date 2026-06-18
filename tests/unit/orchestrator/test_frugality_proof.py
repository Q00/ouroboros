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


def _triad_events(ac: str, run: str, **effort_overrides) -> list[dict]:
    """A full, valid 4-axis triad for one (ac, run) — overridable on the effort event."""
    effort = {
        "ac_id": ac,
        "seed_run_id": run,
        "effort_level": "low",
        "effort_mode": "enforced",
        "is_decomposed_child": True,
    }
    effort.update(effort_overrides)
    return [
        _evt(EVENT_EFFORT_ROUTED, **effort),
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

    def test_string_is_decomposed_child_does_not_truthy_admit(self) -> None:
        # A malformed payload is_decomposed_child="false" must NOT become True via
        # bool("false"). The flag fails safe to False, excluding the (now top-level)
        # row from the proof.
        events = _triad_events("ac1", "r1", is_decomposed_child="false")
        rows = assemble_triads(events)
        assert len(rows) == 1
        assert rows[0].is_decomposed_child is False
        assert rows[0].counts_in_proof is False

    def test_string_decomposition_trustworthy_does_not_truthy_admit(self) -> None:
        # A malformed shadow-replay decomposition_trustworthy="false" must not coerce
        # to True; it fails safe to False, excluding the untrustworthy row.
        events = _triad_events("ac1", "r1")
        for e in events:
            if e["type"] == EVENT_SHADOW_REPLAY:
                e["data"]["decomposition_trustworthy"] = "false"
        rows = assemble_triads(events)
        assert rows[0].decomposition_trustworthy is False
        assert rows[0].counts_in_proof is False

    def test_string_grounding_regression_stays_unmeasured(self) -> None:
        # A malformed grounding_regression="false" must not coerce to a boolean; it
        # stays unset (None) so has_all_axes excludes the unmeasured row.
        events = _triad_events("ac1", "r1")
        for e in events:
            if e["type"] == EVENT_DELIVER_VERDICT:
                e["data"]["grounding_regression"] = "false"
        rows = assemble_triads(events)
        assert rows[0].grounding_regression is None
        assert rows[0].has_all_axes is False
        assert rows[0].counts_in_proof is False

    def test_string_boolean_payloads_never_pass(self) -> None:
        # Reviewer repro: 21 rows whose admission booleans are truthy strings must not
        # PASS. With strict parsing they are excluded → INSUFFICIENT_DATA.
        events: list[dict] = []
        for i in range(21):
            events += _triad_events(f"ac{i}", f"r{i % 3}", is_decomposed_child="false")
        v = evaluate_proof(assemble_triads(events))
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert v.counted_rows == 0
        assert not v.passed

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

    def test_negative_token_spend_row_does_not_count(self) -> None:
        # Malformed telemetry: a negative token spend is not a usable measurement.
        # Counting it would let _reduction_pct report a >100% "reduction" and PASS.
        row = _full_row("ac1", run="r1", token=-1.0, baseline=100)
        assert row.has_all_axes is False
        assert row.counts_in_proof is False

    def test_non_finite_token_spend_row_does_not_count(self) -> None:
        # NaN/inf token spend (e.g. a divide-by-zero producer bug) is excluded.
        for bad in (float("nan"), float("inf"), float("-inf")):
            row = _full_row("ac1", run="r1", token=bad, baseline=100)
            assert row.has_all_axes is False
            assert row.counts_in_proof is False

    def test_zero_token_spend_is_valid(self) -> None:
        # Zero spend is legitimate — a child that cost nothing is maximally frugal.
        row = _full_row("ac1", run="r1", token=0.0, baseline=100)
        assert row.has_all_axes is True
        assert row.counts_in_proof is True

    def test_negative_token_spend_never_passes(self) -> None:
        # Reproduces the reviewer's repro: 21 decomposed enforced rows over 3 runs
        # each with token_spend=-1.0 returned PASS at 101% reduction. They must now
        # be excluded, so the gate honestly returns INSUFFICIENT_DATA.
        rows = [_full_row(f"ac{i}", run=f"r{i % 3}", token=-1.0, baseline=100) for i in range(21)]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert v.counted_rows == 0
        assert not v.passed

    def test_missing_grounding_measurement_does_not_count(self) -> None:
        # grounding_regression=False alone is not a grounding measurement. The axis
        # contract (deliver_verdict) requires an actual traceguard_verdict and a
        # finite unsupported_claim_rate; a defaulted flag without them is excluded.
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        unmeasured = FrugalityTriadRow(
            **{**r.__dict__, "traceguard_verdict": None, "unsupported_claim_rate": None}
        )
        assert unmeasured.grounding_regression is False  # flag is set...
        assert unmeasured.has_all_axes is False  # ...but the measurement is absent
        assert unmeasured.counts_in_proof is False

    def test_non_string_or_blank_verdict_does_not_count(self) -> None:
        # The verdict must be a non-empty string, not merely truthy. A blank string
        # or a non-string truthy payload (e.g. a dict) is not a real TraceGuard
        # verdict and must not satisfy the grounding axis.
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        for bad in ("", "   ", {"x": 1}, 1, True):
            row = FrugalityTriadRow(**{**r.__dict__, "traceguard_verdict": bad})
            assert row.has_all_axes is False
            assert row.counts_in_proof is False

    def test_out_of_range_claim_rate_does_not_count(self) -> None:
        # A rate outside [0, 1] is malformed telemetry, not a usable measurement.
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        for bad in (-0.1, 1.5, float("nan"), float("inf")):
            row = FrugalityTriadRow(**{**r.__dict__, "unsupported_claim_rate": bad})
            assert row.has_all_axes is False
            assert row.counts_in_proof is False

    def test_grounding_flag_without_measurement_never_passes(self) -> None:
        # Reviewer repro: 21 enforced decomposed rows whose deliver-verdict omitted
        # traceguard_verdict and unsupported_claim_rate but set grounding_regression
        # False returned PASS. They must now be excluded → INSUFFICIENT_DATA.
        rows = [
            FrugalityTriadRow(
                **{
                    **_full_row(f"ac{i}", run=f"r{i % 3}", token=80, baseline=100).__dict__,
                    "traceguard_verdict": None,
                    "unsupported_claim_rate": None,
                }
            )
            for i in range(21)
        ]
        v = evaluate_proof(rows)
        assert v.status is ProofStatus.INSUFFICIENT_DATA
        assert v.counted_rows == 0
        assert not v.passed
