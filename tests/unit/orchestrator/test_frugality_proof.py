"""The deterministic frugality-proof machine: assembly + the PASS/FAIL gate."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_DECOMPOSITION_ATTESTED,
    EVENT_DELIVER_VERDICT,
    EVENT_EFFORT_ROUTED,
    EVENT_MODEL_ROUTED,
    EVENT_SHADOW_REPLAY,
    EVENT_TOKEN_ATTRIBUTION,
    FrugalityTriadRow,
    ProofStatus,
    assemble_triads,
    evaluate_proof,
)


def _evt(etype: str, **data) -> dict:
    return {"type": etype, "data": data}


#: Sentinel: derive a joinable ``parent_node_id`` from (run, root_ac_index).
_AUTO_PARENT: object = object()


def _triad_events(
    ac: str,
    run: str,
    *,
    retry_attempt: int = 0,
    root_ac_index: int = 0,
    final_success: bool = True,
    parent_node_id: object = _AUTO_PARENT,
    **effort_overrides,
) -> list[dict]:
    """A full, accepted model/token/grounding/baseline row for one attempt.

    Round-7 Finding #5: a decomposed-child row now also needs joinable parent
    identity plus a real TRUSTWORTHY gate-anchored attestation for the attempt
    to count in the proof, so the default helper output carries both. Pass
    ``parent_node_id=None`` to build a legacy row with the field absent
    entirely (and no attestation event).
    """
    resolved_parent = (
        f"node-{run}-root-{root_ac_index}" if parent_node_id is _AUTO_PARENT else parent_node_id
    )
    effort = {
        "ac_id": ac,
        "seed_run_id": run,
        "root_ac_index": root_ac_index,
        "retry_attempt": retry_attempt,
        "effort_level": "low",
        "effort_mode": "enforced",
        "is_decomposed_child": True,
    }
    effort.update(effort_overrides)
    shadow_replay_data = {
        "ac_id": ac,
        "seed_run_id": run,
        "root_ac_index": root_ac_index,
        "retry_attempt": retry_attempt,
        "baseline_token_spend": 100.0,
        "baseline_mode": "shadow_replay",
        "baseline_tier": "standard",
        "baseline_model": "claude-sonnet-4-6",
        "decomposition_trustworthy": True,
    }
    if resolved_parent is not None:
        shadow_replay_data["parent_node_id"] = resolved_parent
    attestation_events = (
        [
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id=resolved_parent,
                seed_run_id=run,
                retry_attempt=retry_attempt,
                trustworthy=True,
            )
        ]
        if resolved_parent is not None
        else []
    )
    return [
        _evt(EVENT_EFFORT_ROUTED, **effort),
        _evt(
            EVENT_MODEL_ROUTED,
            ac_id=ac,
            seed_run_id=run,
            root_ac_index=root_ac_index,
            retry_attempt=retry_attempt,
            model_tier="frugal",
            model="claude-haiku-4-5",
            model_mode="enforced",
            is_decomposed_child=True,
        ),
        _evt(
            EVENT_TOKEN_ATTRIBUTION,
            ac_id=ac,
            seed_run_id=run,
            root_ac_index=root_ac_index,
            retry_attempt=retry_attempt,
            token_spend=80.0,
        ),
        _evt(EVENT_SHADOW_REPLAY, **shadow_replay_data),
        _evt(
            EVENT_DELIVER_VERDICT,
            ac_id=ac,
            seed_run_id=run,
            root_ac_index=root_ac_index,
            retry_attempt=retry_attempt,
            traceguard_verdict="accepted",
            unsupported_claim_rate=0.0,
            grounding_regression=False,
        ),
        _evt(
            EVENT_AC_OUTCOME_FINALIZED,
            seed_run_id=run,
            root_ac_index=root_ac_index,
            retry_attempt=retry_attempt,
            success=final_success,
            is_decomposed=True,
        ),
        *attestation_events,
    ]


def _full_row(ac_id: str, *, run: str, token: float, baseline: float, regression: bool = False):
    return FrugalityTriadRow(
        ac_id=ac_id,
        seed_run_id=run,
        is_decomposed_child=True,
        decomposition_trustworthy=True,
        effort_level="medium",
        effort_mode="enforced",
        model_tier="frugal",
        model="claude-haiku-4-5",
        model_mode="enforced",
        baseline_tier="standard",
        baseline_model="claude-sonnet-4-6",
        model_lowering_enforced=True,
        token_spend=token,
        baseline_token_spend=baseline,
        baseline_mode="shadow_replay",
        traceguard_verdict="rejected" if regression else "accepted",
        unsupported_claim_rate=1.0 if regression else 0.0,
        grounding_regression=regression,
        authoritatively_accepted=True,
        attempts_paired=True,
    )


class TestAssembleTriads:
    def test_joins_all_axes_by_ac_id(self) -> None:
        events = _triad_events("ac1", "r1", effort_level="medium")
        rows = assemble_triads(events)
        assert len(rows) == 1
        r = rows[0]
        assert r.effort_mode == "enforced" and r.effort_level == "medium"
        assert r.model_mode == "enforced" and r.model_tier == "frugal"
        assert r.baseline_tier == "standard"
        assert r.baseline_model == "claude-sonnet-4-6"
        assert r.token_spend == 80.0 and r.baseline_token_spend == 100.0
        assert r.grounding_regression is False
        assert r.authoritatively_accepted and r.attempts_paired
        assert r.has_all_axes and r.counts_in_proof

    def test_effort_only_row_does_not_count(self) -> None:
        rows = assemble_triads(
            [
                _evt(EVENT_EFFORT_ROUTED, ac_id="ac1", effort_level="high", effort_mode="enforced"),
            ]
        )
        assert rows[0].is_effort_enforced
        assert not rows[0].is_enforced
        assert not rows[0].has_all_axes
        assert not rows[0].counts_in_proof  # token/grounding/baseline missing

    def test_advised_model_row_never_counts(self) -> None:
        r = _full_row("ac1", run="r1", token=80, baseline=100)
        advised = FrugalityTriadRow(
            **{**r.__dict__, "model_mode": "advised", "model_lowering_enforced": False}
        )
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
        rows = assemble_triads([runner_effort, *_triad_events("ac1", "r1")])
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
            for root_ac_index, ac in enumerate(("ac1", "ac2")):
                events += _triad_events(ac, run, root_ac_index=root_ac_index)
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

    @pytest.mark.parametrize(
        "spends",
        [
            (10.0, 25.0, 5.0),
            (5.0, 25.0, 10.0),
        ],
    )
    def test_sums_retry_token_spend_independent_of_event_order(
        self, spends: tuple[float, ...]
    ) -> None:
        events = [
            _evt(
                EVENT_TOKEN_ATTRIBUTION,
                ac_id="ac1",
                seed_run_id="r1",
                retry_attempt=index,
                token_spend=spend,
            )
            for index, spend in enumerate(spends)
        ]

        rows = assemble_triads(events)

        assert len(rows) == 1
        assert rows[0].token_spend == 40.0

    @pytest.mark.parametrize("malformed", [None, True, -1.0, float("nan"), float("inf"), "10"])
    @pytest.mark.parametrize("malformed_first", [False, True])
    def test_malformed_retry_token_spend_invalidates_axis_regardless_of_order(
        self, malformed: object, malformed_first: bool
    ) -> None:
        valid = _evt(
            EVENT_TOKEN_ATTRIBUTION,
            ac_id="ac1",
            seed_run_id="r1",
            retry_attempt=0,
            token_spend=10.0,
        )
        invalid = _evt(
            EVENT_TOKEN_ATTRIBUTION,
            ac_id="ac1",
            seed_run_id="r1",
            retry_attempt=1,
            token_spend=malformed,
        )

        rows = assemble_triads([invalid, valid] if malformed_first else [valid, invalid])

        assert len(rows) == 1
        assert rows[0].token_spend is None
        assert rows[0].has_all_axes is False

    def test_retry_spend_cannot_false_pass_frugality_gate(self) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        retry_events = _triad_events("ac1", "r1", retry_attempt=1)
        for event in retry_events:
            if event["type"] == EVENT_TOKEN_ATTRIBUTION:
                event["data"]["token_spend"] = 130.0
        events += retry_events

        verdict = evaluate_proof(assemble_triads(events), min_triads=1, min_runs=1)

        # The original attempt spent 80 and the retry spent 130. Their paired
        # baselines total 200, so the real aggregate is 210 (-5% reduction), not
        # whichever single attempt EventStore happened to return last.
        assert verdict.status is ProofStatus.FAIL_NO_FRUGALITY
        assert verdict.token_reduction_pct == -5.0

    def test_unpaired_retry_spend_is_excluded_not_underreported(self) -> None:
        events = _triad_events("ac1", "r1")
        events.append(
            _evt(
                EVENT_TOKEN_ATTRIBUTION,
                ac_id="ac1",
                seed_run_id="r1",
                root_ac_index=0,
                retry_attempt=1,
                token_spend=30.0,
            )
        )
        events.append(
            _evt(
                EVENT_AC_OUTCOME_FINALIZED,
                seed_run_id="r1",
                root_ac_index=0,
                retry_attempt=1,
                success=True,
                is_decomposed=True,
            )
        )

        row = assemble_triads(events)[0]
        assert row.token_spend == 110.0
        assert row.attempts_paired is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize("newest_first", [False, True])
    def test_retry_grounding_regression_is_order_independent(self, newest_first: bool) -> None:
        initial = _triad_events("ac1", "r1", retry_attempt=0)
        retry = _triad_events("ac1", "r1", retry_attempt=1)
        for event in retry:
            if event["type"] == EVENT_DELIVER_VERDICT:
                event["data"].update(
                    traceguard_verdict="rejected",
                    unsupported_claim_rate=0.5,
                    grounding_regression=True,
                )
        events = retry + initial if newest_first else initial + retry

        verdict = evaluate_proof(assemble_triads(events), min_triads=1, min_runs=1)

        assert verdict.status is ProofStatus.FAIL_GROUNDING_REGRESSION
        assert verdict.grounding_regressions == 1

    def test_outer_gate_rejection_excludes_otherwise_complete_row(self) -> None:
        events = _triad_events("ac1", "r1", final_success=False)

        row = assemble_triads(events)[0]

        assert row.has_all_axes
        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    def test_missing_outer_gate_marker_fails_closed(self) -> None:
        events = [
            event
            for event in _triad_events("ac1", "r1")
            if event["type"] != EVENT_AC_OUTCOME_FINALIZED
        ]

        row = assemble_triads(events)[0]

        assert row.has_all_axes
        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize("latest_is_decomposed", [False, None, "true"])
    def test_latest_marker_requires_strict_decomposed_success(
        self, latest_is_decomposed: object
    ) -> None:
        events = _triad_events("ac1", "r1")
        marker = next(event for event in events if event["type"] == EVENT_AC_OUTCOME_FINALIZED)
        marker["data"]["is_decomposed"] = latest_is_decomposed

        row = assemble_triads(events)[0]

        assert row.has_all_axes
        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    def test_stale_child_cannot_hitchhike_on_later_root_success(self) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        events.append(
            _evt(
                EVENT_AC_OUTCOME_FINALIZED,
                seed_run_id="r1",
                root_ac_index=0,
                retry_attempt=1,
                success=True,
                is_decomposed=True,
            )
        )

        row = assemble_triads(events)[0]

        # This child only participated in failed/obsolete attempt 0. A later root
        # success cannot retroactively accept it when attempt 1 has no matching axes.
        assert row.attempts_paired
        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    def test_stale_child_cannot_hitchhike_on_later_atomic_success(self) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        events.append(
            _evt(
                EVENT_AC_OUTCOME_FINALIZED,
                seed_run_id="r1",
                root_ac_index=0,
                retry_attempt=1,
                success=True,
                is_decomposed=False,
            )
        )

        row = assemble_triads(events)[0]

        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize("duplicate_success", [True, False])
    def test_duplicate_or_conflicting_latest_marker_fails_closed(
        self, duplicate_success: bool
    ) -> None:
        events = _triad_events("ac1", "r1")
        events.append(
            _evt(
                EVENT_AC_OUTCOME_FINALIZED,
                seed_run_id="r1",
                root_ac_index=0,
                retry_attempt=0,
                success=duplicate_success,
                is_decomposed=True,
            )
        )

        row = assemble_triads(events)[0]

        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize("malformed_attempt", [None, True, -1, "1", 1.0])
    def test_malformed_outcome_marker_poisons_known_root(self, malformed_attempt: object) -> None:
        events = _triad_events("ac1", "r1")
        malformed = _evt(
            EVENT_AC_OUTCOME_FINALIZED,
            seed_run_id="r1",
            root_ac_index=0,
            retry_attempt=malformed_attempt,
            success=True,
            is_decomposed=True,
        )
        if malformed_attempt is None:
            malformed["data"].pop("retry_attempt")
        events.append(malformed)

        row = assemble_triads(events)[0]

        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    def test_missing_retry_attempt_never_defaults_into_a_complete_row(self) -> None:
        events = _triad_events("ac1", "r1")
        for event in events:
            event["data"].pop("retry_attempt", None)

        row = assemble_triads(events)[0]

        assert row.attempts_paired is False
        assert row.authoritatively_accepted is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize(
        "event_type",
        [
            EVENT_MODEL_ROUTED,
            EVENT_TOKEN_ATTRIBUTION,
            EVENT_DELIVER_VERDICT,
            EVENT_SHADOW_REPLAY,
        ],
    )
    def test_duplicate_axis_event_for_same_attempt_invalidates_row(self, event_type: str) -> None:
        events = _triad_events("ac1", "r1")
        original = next(event for event in events if event["type"] == event_type)
        events.append({"type": original["type"], "data": dict(original["data"])})

        row = assemble_triads(events)[0]

        assert row.attempts_paired is False
        assert not row.counts_in_proof

    @pytest.mark.parametrize(
        ("unsupported_claim_rate", "grounding_regression"),
        [(0.5, False), (0.0, True)],
    )
    def test_semantically_inconsistent_accepted_verdict_invalidates_row(
        self,
        unsupported_claim_rate: float,
        grounding_regression: bool,
    ) -> None:
        events = _triad_events("ac1", "r1")
        verdict = next(event for event in events if event["type"] == EVENT_DELIVER_VERDICT)
        verdict["data"].update(
            unsupported_claim_rate=unsupported_claim_rate,
            grounding_regression=grounding_regression,
        )

        row = assemble_triads(events)[0]

        assert row.attempts_paired is False
        assert not row.counts_in_proof

    def test_default_effort_none_still_counts_enforced_lower_model(self) -> None:
        events = [
            event for event in _triad_events("ac1", "r1") if event["type"] != EVENT_EFFORT_ROUTED
        ]

        row = assemble_triads(events)[0]

        assert row.effort_level is None
        assert row.model_lowering_enforced
        assert row.counts_in_proof

    @pytest.mark.parametrize(
        ("child_tier", "baseline_tier", "mode"),
        [
            ("standard", "standard", "enforced"),
            ("frontier", "standard", "enforced"),
            ("frugal", "standard", "advised"),
            ("custom", "standard", "enforced"),
        ],
    )
    def test_non_lower_or_unenforced_model_never_counts(
        self, child_tier: str, baseline_tier: str, mode: str
    ) -> None:
        events = _triad_events("ac1", "r1")
        for event in events:
            if event["type"] == EVENT_MODEL_ROUTED:
                event["data"].update(model_tier=child_tier, model_mode=mode)
            elif event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["baseline_tier"] = baseline_tier

        row = assemble_triads(events)[0]

        assert not row.model_lowering_enforced
        assert not row.counts_in_proof

    def test_sparse_tier_fallback_to_same_model_never_counts(self) -> None:
        """Tier labels cannot claim a reduction when both calls used one model."""
        events = _triad_events("ac1", "r1")
        for event in events:
            if event["type"] == EVENT_MODEL_ROUTED:
                event["data"]["model"] = "claude-sonnet-4-6"

        row = assemble_triads(events)[0]

        assert row.model_tier == "frugal"
        assert row.baseline_tier == "standard"
        assert row.model == row.baseline_model
        assert not row.model_lowering_enforced
        assert not row.counts_in_proof


class TestDecompositionAttestedOverride:
    """Fix 3 (round 3, BLOCKING): ``execution.ac.shadow_replay`` records the
    PROPOSAL/prior-round trust signal BEFORE the current decomposition round
    is attested by the real gate-anchored oracle (attestation happens later,
    after all children finish -- see ``_attest_decomposition_round``). The
    proof must not keep treating an initially-cheap split as trustworthy once
    the post-round attestation proves otherwise for that same attempt.
    """

    def test_untrustworthy_post_round_attestation_overrides_trustworthy_shadow_replay(
        self,
    ) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                # Proposal-time snapshot: looked trustworthy before the round
                # attested.
                event["data"]["decomposition_trustworthy"] = True
                event["data"]["parent_node_id"] = "node-root-1"

        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=False,
            )
        )

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is False
        assert not row.counts_in_proof

    def test_trustworthy_post_round_attestation_keeps_row_trustworthy(self) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["decomposition_trustworthy"] = True
                event["data"]["parent_node_id"] = "node-root-1"

        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=True,
            )
        )

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is True
        assert row.counts_in_proof

    def test_missing_attestation_excludes_decomposed_child_row(self) -> None:
        """A row carrying ``parent_node_id`` is a decomposed-child row whose
        round SHOULD have emitted an attestation. A MISSING attestation event
        (legacy stream, or a lost/failed write) must fail closed and exclude
        the row: since properly-attested rounds legitimately emit
        non-trustworthy verdicts most of the time, defaulting missing
        telemetry to trustworthy would make a lost write MORE trusted than a
        present one -- the only rows still counting would be the degraded
        ones."""
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["decomposition_trustworthy"] = True
                event["data"]["parent_node_id"] = "node-root-1"

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is False
        assert not row.counts_in_proof

    def test_row_without_parent_node_id_is_excluded_fail_closed(self) -> None:
        """Round-7 Finding #5: a decomposed-child row with NO
        ``parent_node_id`` at all (legacy stream predating the field, or a
        producer that dropped it) can never be joined to any gate-anchored
        attestation round. It previously fell through with
        ``attestation_trustworthy=True`` and was admitted into the proof on
        the WEAKER proposal-time shadow-replay snapshot alone — no real
        attestation evidence ever required — making the one row shape that
        can never be attested the easiest to admit. It now fails closed the
        same way as present-but-unattested and ambiguous parentage: excluded
        from ``counts_in_proof``."""
        events = _triad_events("ac1", "r1", retry_attempt=0, parent_node_id=None)

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is False
        assert not row.counts_in_proof

    def test_top_level_row_without_parent_node_id_keeps_permissive_default(self) -> None:
        """Scope control for the Round-7 fail-closed fix: a TOP-LEVEL row
        (``is_decomposed_child=False``) legitimately has no parent and no
        attestation round; the absent-parent fail-closed rule must not sweep
        it into ``decomposition_trustworthy=False``. It stays excluded from
        the proof anyway because ``counts_in_proof`` requires
        ``is_decomposed_child``."""
        events = _triad_events(
            "ac1",
            "r1",
            retry_attempt=0,
            parent_node_id=None,
            is_decomposed_child=False,
        )
        for event in events:
            if event["type"] == EVENT_MODEL_ROUTED:
                event["data"]["is_decomposed_child"] = False

        row = assemble_triads(events)[0]

        assert row.is_decomposed_child is False
        assert row.decomposition_trustworthy is True
        assert not row.counts_in_proof

    def test_conflicting_parent_node_ids_exclude_row(self) -> None:
        """Ambiguous parentage: a row reporting two different decomposition
        rounds cannot bind its attestation, so it is excluded (previously it
        silently skipped the override and stayed trustworthy)."""
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["parent_node_id"] = "node-root-1"
            elif event["type"] == EVENT_MODEL_ROUTED:
                event["data"]["parent_node_id"] = "node-root-OTHER"
        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=True,
            )
        )

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is False
        assert not row.counts_in_proof

    def test_duplicate_attestation_for_same_round_attempt_is_ambiguous_and_poisons_row(
        self,
    ) -> None:
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["decomposition_trustworthy"] = True
                event["data"]["parent_node_id"] = "node-root-1"

        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=True,
            )
        )
        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=True,
            )
        )

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is False
        assert not row.counts_in_proof

    def test_attestation_for_a_different_run_does_not_leak_across_runs(self) -> None:
        """Attestations are keyed by run: an UNTRUSTWORTHY verdict from a
        DIFFERENT run must not poison this run's row, which carries its own
        real TRUSTWORTHY attestation."""
        events = _triad_events("ac1", "r1", retry_attempt=0)
        for event in events:
            if event["type"] == EVENT_SHADOW_REPLAY:
                event["data"]["decomposition_trustworthy"] = True
                event["data"]["parent_node_id"] = "node-root-1"

        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r1",
                retry_attempt=0,
                trustworthy=True,
            )
        )
        events.append(
            _evt(
                EVENT_DECOMPOSITION_ATTESTED,
                node_id="node-root-1",
                seed_run_id="r-OTHER",
                retry_attempt=0,
                trustworthy=False,
            )
        )

        row = assemble_triads(events)[0]

        assert row.decomposition_trustworthy is True
        assert row.counts_in_proof


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

    def test_oversized_integer_token_spend_does_not_crash(self) -> None:
        # JSON permits integers too large to convert to float. Treat corrupted
        # telemetry as missing instead of raising while assembling a verdict.
        row = _full_row("ac1", run="r1", token=10**10_000, baseline=100)

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

    def test_finite_rows_whose_aggregate_overflows_never_pass(self) -> None:
        rows = [
            _full_row(
                f"ac{i}",
                run=f"r{i % 3}",
                token=5e307,
                baseline=1e308,
            )
            for i in range(21)
        ]

        verdict = evaluate_proof(rows)

        assert verdict.status is ProofStatus.INSUFFICIENT_DATA
        assert verdict.token_reduction_pct is None
        assert not verdict.passed

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
