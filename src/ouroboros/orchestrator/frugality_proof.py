"""Deterministic frugality-proof machine (the seed's FrugalityProofTriad gate).

The hypothesis the seed exists to prove: *if work is decomposed well, each child
runs on a lower model tier and stays token-frugal WITHOUT losing grounding.*
This module is the deterministic, LLM-free judge of that hypothesis. It reads the
event stream a run produces, assembles one :class:`FrugalityTriadRow` per AC, and
computes a PASS/FAIL verdict — no model is asked anything, so the proof cannot be
reward-hacked.

A triad row joins three measured axes by ``ac_id``:

* **routing** — ``execution.ac.model_routed`` (model_tier + model_mode). The child
  tier must be natively enforced and strictly lower than the shadow baseline tier.
  ``execution.ac.effort_routed`` remains auxiliary audit metadata.
* **token** — ``execution.ac.token_attribution.reported`` (token_spend), harvested
  from runtime usage telemetry (seed AC2).
* **grounding** — ``execution.ac.deliver_verdict`` (traceguard_verdict +
  unsupported_claim_rate + fail-closed grounding_regression), validated against
  accepted-leaf journal evidence (seed AC4).
* **baseline** — ``execution.ac.shadow_replay`` (baseline_token_spend at parent
  model tier), emitted only by the opt-in isolated replay experiment (seed AC5).
* **acceptance** — ``execution.ac.outcome_finalized``. Proof rows remain provisional
  until the seed-level verify/retry layer authoritatively accepts their root AC.

A row only ``counts_in_proof`` when the lower model tier was ENFORCED, every retry
attempt has a paired token/deliver/shadow measurement, the root AC was finally
accepted, the unit is a decomposed child (the hypothesis is about children, not
top-level ACs), the decomposition was trustworthy, and all axes are present. The
gate therefore returns
``INSUFFICIENT_DATA`` honestly when a run lacks a measured axis (for example the
opt-in replay is off or a claim cannot be bound unambiguously to journal evidence).
Across a bounded same-seed cohort with every axis measured, the same gate yields a
real PASS/FAIL. The contract (event types + fields) remains deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import math

from ouroboros.orchestrator.evidence.common import (
    event_data as _event_data,
)
from ouroboros.orchestrator.evidence.common import (
    event_type as _event_type,
)
from ouroboros.orchestrator.evidence.common import (
    execution_run_anchor,
    parse_retry_attempt,
    parse_root_ac_index,
)
from ouroboros.orchestrator.evidence.common import (
    finite_number as _finite_number,
)
from ouroboros.orchestrator.evidence.common import (
    strict_bool as _strict_bool,
)

# -- Event-type contract the producers must emit -----------------------------
EVENT_EFFORT_ROUTED = "execution.ac.effort_routed"
EVENT_MODEL_ROUTED = "execution.ac.model_routed"
EVENT_TOKEN_ATTRIBUTION = "execution.ac.token_attribution.reported"
EVENT_DELIVER_VERDICT = "execution.ac.deliver_verdict"
EVENT_SHADOW_REPLAY = "execution.ac.shadow_replay"
EVENT_AC_OUTCOME_FINALIZED = "execution.ac.outcome_finalized"

EFFORT_MODE_ENFORCED = "enforced"
MODEL_MODE_ENFORCED = "enforced"
BASELINE_MODE_SHADOW_REPLAY = "shadow_replay"
MODEL_TIER_ORDER: Mapping[str, int] = {
    "frugal": 0,
    "standard": 1,
    "frontier": 2,
}

# -- Default gate thresholds (the seed's acceptance criteria) -----------------
DEFAULT_MIN_TRIADS = 20
DEFAULT_MIN_RUNS = 3
DEFAULT_MIN_REDUCTION_PCT = 10.0


class ProofStatus(StrEnum):
    PASS = "pass"
    FAIL_GROUNDING_REGRESSION = "fail_grounding_regression"
    FAIL_NO_FRUGALITY = "fail_no_frugality"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class FrugalityTriadRow:
    """One AC's measured triad (token x model routing x grounding) + baseline."""

    ac_id: str
    seed_run_id: str | None = None
    root_ac_index: int | None = None
    is_decomposed_child: bool = False
    decomposition_trustworthy: bool = True
    # effort axis
    effort_level: str | None = None
    effort_mode: str | None = None
    parent_effort: str | None = None
    # model-routing axis
    model_tier: str | None = None
    model: str | None = None
    model_mode: str | None = None
    baseline_tier: str | None = None
    baseline_model: str | None = None
    model_lowering_enforced: bool = False
    # token axis
    token_spend: float | None = None
    baseline_token_spend: float | None = None
    baseline_mode: str | None = None
    # grounding axis
    traceguard_verdict: str | None = None
    unsupported_claim_rate: float | None = None
    grounding_regression: bool | None = None
    # authoritative admission / retry pairing
    authoritatively_accepted: bool = False
    attempts_paired: bool = False

    @property
    def is_enforced(self) -> bool:
        """Whether the frugality actuator was natively enforced and lowered."""
        return self.model_lowering_enforced

    @property
    def is_effort_enforced(self) -> bool:
        """Auxiliary effort-contract metadata; not proof admission."""
        return self.effort_mode == EFFORT_MODE_ENFORCED and self.effort_level is not None

    @property
    def has_all_axes(self) -> bool:
        # Every measured axis must be a usable measurement, not merely present:
        # * token_spend must be finite and NON-NEGATIVE. A negative (or NaN/inf)
        #   spend is malformed telemetry — counting it lets _reduction_pct produce a
        #   >100% "reduction" and a false PASS (e.g. token_spend=-1 → 101%). Zero is
        #   valid (a child that spent nothing is maximally frugal).
        # * baseline_token_spend must be finite and STRICTLY POSITIVE: it is the
        #   denominator of the token-reduction ratio, so a zero/negative/non-finite
        #   shadow-replay baseline is not a usable measurement and the row is excluded
        #   rather than counted (which would make the aggregate reduction undefined).
        # * The grounding axis must carry the ACTUAL TraceGuard output the contract
        #   defines (deliver_verdict → traceguard_verdict + unsupported_claim_rate),
        #   not just a defaulted grounding_regression flag. Otherwise a malformed or
        #   defaulted future producer could assert "no grounding loss" (regression
        #   False) without ever measuring grounding, and the gate would PASS on it.
        #   Require a verdict string and a finite unsupported-claim rate in [0, 1].
        token = _finite_number(self.token_spend)
        baseline = _finite_number(self.baseline_token_spend)
        claim_rate = _finite_number(self.unsupported_claim_rate)
        verdict = self.traceguard_verdict
        normalized_verdict = verdict.strip().casefold() if isinstance(verdict, str) else ""
        grounding_consistent = (
            normalized_verdict == "accepted"
            and claim_rate == 0.0
            and self.grounding_regression is False
        ) or (normalized_verdict != "accepted" and self.grounding_regression is True)
        return (
            token is not None
            and token >= 0
            and baseline is not None
            and baseline > 0
            and isinstance(verdict, str)
            and bool(verdict.strip())
            and claim_rate is not None
            and 0.0 <= claim_rate <= 1.0
            and self.grounding_regression is not None
            and grounding_consistent
            and self.baseline_mode == BASELINE_MODE_SHADOW_REPLAY
            and self.attempts_paired
        )

    @property
    def counts_in_proof(self) -> bool:
        """Only accepted, lowered-model, trustworthy, fully-measured rows count.

        The hypothesis is specifically about *decomposed children* running at a
        lower model tier than their parent (see module docstring), so a top-level AC
        (``is_decomposed_child=False``) is excluded even when fully measured —
        otherwise a sample of ordinary top-level executions could PASS the gate and
        "prove" a frugality claim the run never tested. A top-level unit also has no
        parent tier to lower from and no shadow-replay baseline that means anything.
        Advised/equal-tier routing, an outer-gate rejection, untrustworthy
        (forced-atomic) decomposition, an unpaired retry, or a missing axis likewise
        exclude the row — the exact honesty the deterministic proof needs.
        """
        return (
            self.is_enforced
            and self.authoritatively_accepted
            and self.is_decomposed_child
            and self.decomposition_trustworthy
            and self.has_all_axes
        )


@dataclass(frozen=True)
class ProofVerdict:
    status: ProofStatus
    counted_rows: int
    runs: int
    token_reduction_pct: float | None
    grounding_regressions: int
    reason: str
    thresholds: Mapping[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status is ProofStatus.PASS


def assemble_triads(events: Iterable[object]) -> list[FrugalityTriadRow]:
    """Join the per-axis events into one triad row per ``(run, ac_id)``.

    Accepts events as mappings or objects exposing ``type``/``event_type`` and
    ``data``/``payload``. Unknown event types are ignored. An event without an
    ``ac_id`` cannot be correlated and is skipped.

    Skipping the ``ac_id``-less event is **by design, not a gap**: the proof is a
    per-decomposed-AC measurement, and the whole-seed direct-runner routing events
    are emitted without a per-AC id because a non-decomposed single-call run has no
    child tier to lower and no
    shadow-replay baseline — there is nothing for the frugality triad to prove. Such
    runs are intentionally out of the proof's scope rather than counted as
    missing-axis rows; only the parallel executor's per-AC events (which carry
    ``ac_id``) contribute.

    Rows are keyed by ``(run, ac_id)`` — **not** ``ac_id`` alone — because the proof
    spans runs (``min_runs``) and the same logical AC id recurs every run. Keying by
    ``ac_id`` only would let a later run's events overwrite an earlier run's in the
    same slot, collapsing valid cross-run evidence to the last run. The run anchor is
    each event's ``seed_run_id`` (falling back to ``execution_id``); a logical AC
    therefore yields one row per run it ran in. Events carrying no run anchor share a
    single implicit run — the original single-run behavior, preserved.
    """
    acc: dict[tuple[str | None, str], dict] = {}
    finalized: dict[
        tuple[str | None, int],
        dict[int, list[tuple[bool | None, bool | None]]],
    ] = {}
    finalized_invalid: set[tuple[str | None, int]] = set()

    def merge_root(row: dict, data: Mapping) -> None:
        observed = parse_root_ac_index(data)
        if observed is None:
            return
        current = row.get("root_ac_index")
        if current is None:
            row["root_ac_index"] = observed
        elif current != observed:
            row["root_index_invalid"] = True

    def merge_decomposed(row: dict, data: Mapping) -> None:
        observed = _strict_bool(data.get("is_decomposed_child"))
        if observed is None:
            row["decomposition_flag_invalid"] = True
            return
        current = row.get("is_decomposed_child")
        if current is None:
            row["is_decomposed_child"] = observed
        elif current != observed:
            row["decomposition_flag_invalid"] = True

    def slot(data: Mapping) -> dict | None:
        ac_id = data.get("ac_id")
        if not ac_id:
            return None
        run_key = execution_run_anchor(data)
        return acc.setdefault((run_key, str(ac_id)), {"ac_id": str(ac_id), "seed_run_id": run_key})

    for event in events:
        etype = _event_type(event)
        data = _event_data(event)
        if etype == EVENT_AC_OUTCOME_FINALIZED:
            run_key = execution_run_anchor(data)
            root_index = parse_root_ac_index(data)
            attempt = parse_retry_attempt(data)
            success = _strict_bool(data.get("success"))
            is_decomposed = _strict_bool(data.get("is_decomposed"))
            if root_index is None:
                continue
            if attempt is None:
                # A malformed marker for a known root must poison admission for
                # that root. Ignoring it would let the assembler fall back to an
                # older successful marker and potentially PASS after the producer
                # emitted a newer-but-corrupt authoritative outcome.
                finalized_invalid.add((run_key, root_index))
                continue
            # Preserve malformed booleans as ``None`` instead of dropping the
            # marker. If this is the latest root attempt, admission must fail closed
            # rather than falling back to an older successful marker.
            finalized.setdefault((run_key, root_index), {}).setdefault(attempt, []).append(
                (success, is_decomposed)
            )
        elif etype == EVENT_EFFORT_ROUTED:
            row = slot(data)
            if row is None:
                continue
            merge_root(row, data)
            merge_decomposed(row, data)
            row.setdefault("effort_levels", set()).add(data.get("effort_level"))
            row.setdefault("effort_modes", set()).add(data.get("effort_mode"))
            if data.get("parent_effort") is not None:
                row.setdefault("parent_efforts", set()).add(data.get("parent_effort"))
        elif etype == EVENT_MODEL_ROUTED:
            row = slot(data)
            if row is None:
                continue
            merge_root(row, data)
            merge_decomposed(row, data)
            attempt = parse_retry_attempt(data)
            if attempt is None:
                row["model_invalid"] = True
                continue
            model_tier = data.get("model_tier")
            model = data.get("model")
            model_mode = data.get("model_mode")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (model_tier, model, model_mode)
            ):
                row["model_invalid"] = True
                continue
            value = (model_tier.strip(), model.strip(), model_mode.strip())
            models = row.setdefault("models_by_attempt", {})
            if attempt in models:
                # One routing decision is authoritative per leaf attempt. Even an
                # identical duplicate can be an at-least-once persistence replay;
                # accepting it would make other single-axis duplicates exploitable.
                row["model_invalid"] = True
            else:
                models[attempt] = value
        elif etype == EVENT_TOKEN_ATTRIBUTION:
            row = slot(data)
            if row is None:
                continue
            merge_root(row, data)
            attempt = parse_retry_attempt(data)
            # One logical AC may emit one attribution event per retry/resume
            # attempt. Spend is cumulative across those attempts, and EventStore
            # query order is newest-first, so replacement would both undercount
            # rework and make the result order-dependent. Sum every valid
            # measurement instead. If any measurement is malformed, poison the
            # axis for this row: silently dropping a bad retry could understate
            # spend and create a false frugality PASS.
            token_spend = _finite_number(data.get("token_spend"))
            if attempt is None or token_spend is None or token_spend < 0:
                row["token_spend_invalid"] = True
            else:
                tokens = row.setdefault("tokens_by_attempt", {})
                # Runtime-message usage is already aggregated by the producer into
                # one attribution event. A second event for the same attempt is not
                # another usage fragment; it is ambiguous duplicate telemetry.
                if attempt in tokens:
                    row["token_spend_invalid"] = True
                else:
                    tokens[attempt] = token_spend
        elif etype == EVENT_DELIVER_VERDICT:
            row = slot(data)
            if row is None:
                continue
            merge_root(row, data)
            attempt = parse_retry_attempt(data)
            verdict = data.get("traceguard_verdict")
            claim_rate = _finite_number(data.get("unsupported_claim_rate"))
            grounding = _strict_bool(data.get("grounding_regression"))
            if (
                attempt is None
                or not isinstance(verdict, str)
                or not verdict.strip()
                or claim_rate is None
                or not 0.0 <= claim_rate <= 1.0
                or grounding is None
            ):
                row["deliver_invalid"] = True
                continue
            deliveries = row.setdefault("deliveries_by_attempt", {})
            normalized_verdict = verdict.strip()
            if normalized_verdict.casefold() == "accepted" and (
                claim_rate != 0.0 or grounding is not False
            ):
                # An accepted TraceGuard verdict means every claim was supported.
                # A non-zero unsupported rate or regression flag contradicts that
                # verdict, so the payload is malformed rather than ground truth.
                row["deliver_invalid"] = True
                continue
            if attempt in deliveries:
                row["deliver_invalid"] = True
            else:
                deliveries[attempt] = (normalized_verdict, claim_rate, grounding)
        elif etype == EVENT_SHADOW_REPLAY:
            row = slot(data)
            if row is None:
                continue
            merge_root(row, data)
            attempt = parse_retry_attempt(data)
            baseline = _finite_number(data.get("baseline_token_spend"))
            baseline_mode = data.get("baseline_mode")
            baseline_tier = data.get("baseline_tier")
            baseline_model = data.get("baseline_model")
            trustworthy = _strict_bool(data.get("decomposition_trustworthy"))
            if (
                attempt is None
                or baseline is None
                or baseline <= 0
                or baseline_mode != BASELINE_MODE_SHADOW_REPLAY
                or not isinstance(baseline_tier, str)
                or not baseline_tier.strip()
                or not isinstance(baseline_model, str)
                or not baseline_model.strip()
                or trustworthy is None
            ):
                row["baseline_invalid"] = True
                continue
            baselines = row.setdefault("baselines_by_attempt", {})
            normalized_tier = baseline_tier.strip()
            normalized_model = baseline_model.strip()
            if attempt not in baselines:
                baselines[attempt] = (
                    baseline,
                    normalized_tier,
                    normalized_model,
                    trustworthy,
                )
            else:
                # A replay is run once per live attempt. Summing a duplicate only
                # on the denominator can turn an ordinary row into a false PASS.
                row["baseline_invalid"] = True

    rows: list[FrugalityTriadRow] = []
    for v in acc.values():
        models = v.get("models_by_attempt", {})
        tokens = v.get("tokens_by_attempt", {})
        deliveries = v.get("deliveries_by_attempt", {})
        baselines = v.get("baselines_by_attempt", {})
        attempt_sets = (set(models), set(tokens), set(deliveries), set(baselines))
        attempts_paired = bool(attempt_sets[0]) and all(
            attempt_set == attempt_sets[0] for attempt_set in attempt_sets[1:]
        )
        attempts_paired = attempts_paired and not any(
            v.get(flag, False)
            for flag in (
                "model_invalid",
                "token_spend_invalid",
                "deliver_invalid",
                "baseline_invalid",
                "root_index_invalid",
                "decomposition_flag_invalid",
            )
        )

        model_tiers = {value[0] for value in models.values()}
        model_names = {value[1] for value in models.values()}
        model_modes = {value[2] for value in models.values()}
        baseline_tiers = {value[1] for value in baselines.values()}
        baseline_models = {value[2] for value in baselines.values()}
        model_lowering_enforced = attempts_paired and all(
            model_mode == MODEL_MODE_ENFORCED
            and child_tier in MODEL_TIER_ORDER
            and baseline_tier in MODEL_TIER_ORDER
            and MODEL_TIER_ORDER[child_tier] < MODEL_TIER_ORDER[baseline_tier]
            and child_model != baseline_model
            for attempt, (child_tier, child_model, model_mode) in models.items()
            for _baseline_spend, baseline_tier, baseline_model, _trustworthy in (
                baselines[attempt],
            )
        )

        root_index = v.get("root_ac_index")
        final_markers = (
            finalized.get((v.get("seed_run_id"), root_index), {})
            if isinstance(root_index, int)
            else {}
        )
        authoritatively_accepted = False
        if final_markers:
            final_attempt = max(final_markers)
            final_records = final_markers[final_attempt]
            # The outer marker is authoritative only when there is exactly one
            # unambiguous result for its latest attempt, that result is a successful
            # decomposition, and this child actually participated in that final
            # attempt. This prevents failed/stale child rows from hitchhiking on a
            # later atomic root success.
            authoritatively_accepted = (
                (v.get("seed_run_id"), root_index) not in finalized_invalid
                and len(final_records) == 1
                and final_records[0] == (True, True)
                and final_attempt in attempt_sets[0]
            )

        delivery_values = list(deliveries.values())
        grounding_regression = (
            any(value[2] or value[0].casefold() != "accepted" for value in delivery_values)
            if delivery_values and not v.get("deliver_invalid", False)
            else None
        )
        traceguard_verdict = None
        unsupported_claim_rate = None
        if delivery_values and grounding_regression is not None:
            traceguard_verdict = "rejected" if grounding_regression else "accepted"
            unsupported_claim_rate = max(value[1] for value in delivery_values)

        effort_levels = v.get("effort_levels", set())
        effort_modes = v.get("effort_modes", set())
        parent_efforts = v.get("parent_efforts", set())
        rows.append(
            FrugalityTriadRow(
                ac_id=v["ac_id"],
                seed_run_id=v.get("seed_run_id"),
                root_ac_index=root_index if isinstance(root_index, int) else None,
                is_decomposed_child=(
                    bool(v.get("is_decomposed_child"))
                    and not v.get("decomposition_flag_invalid", False)
                ),
                decomposition_trustworthy=(
                    bool(baselines)
                    and all(value[3] for value in baselines.values())
                    and not v.get("baseline_invalid", False)
                ),
                effort_level=next(iter(effort_levels)) if len(effort_levels) == 1 else None,
                effort_mode=next(iter(effort_modes)) if len(effort_modes) == 1 else None,
                parent_effort=next(iter(parent_efforts)) if len(parent_efforts) == 1 else None,
                model_tier=next(iter(model_tiers)) if len(model_tiers) == 1 else None,
                model=next(iter(model_names)) if len(model_names) == 1 else None,
                model_mode=next(iter(model_modes)) if len(model_modes) == 1 else None,
                baseline_tier=(next(iter(baseline_tiers)) if len(baseline_tiers) == 1 else None),
                baseline_model=(next(iter(baseline_models)) if len(baseline_models) == 1 else None),
                model_lowering_enforced=model_lowering_enforced,
                token_spend=(
                    sum(tokens.values())
                    if tokens and not v.get("token_spend_invalid", False)
                    else None
                ),
                baseline_token_spend=(
                    sum(value[0] for value in baselines.values())
                    if baselines and not v.get("baseline_invalid", False)
                    else None
                ),
                baseline_mode=(
                    BASELINE_MODE_SHADOW_REPLAY
                    if baselines and not v.get("baseline_invalid", False)
                    else None
                ),
                traceguard_verdict=traceguard_verdict,
                unsupported_claim_rate=unsupported_claim_rate,
                grounding_regression=grounding_regression,
                authoritatively_accepted=authoritatively_accepted,
                attempts_paired=attempts_paired,
            )
        )
    return rows


def evaluate_proof(
    rows: Iterable[FrugalityTriadRow],
    *,
    min_triads: int = DEFAULT_MIN_TRIADS,
    min_runs: int = DEFAULT_MIN_RUNS,
    min_reduction_pct: float = DEFAULT_MIN_REDUCTION_PCT,
) -> ProofVerdict:
    """Deterministically judge the frugality hypothesis from triad rows.

    Order of checks (the seed's exit conditions):

    1. **Grounding is a per-AC veto** — any counted row whose lower-tier run
       produced a newly-rejected claim (``grounding_regression``) fails the proof
       outright; lowering model tier must never reduce grounding.
    2. **Sample sufficiency** — at least ``min_triads`` counted rows across at least
       ``min_runs`` runs, else the result is anecdotal.
    3. **Frugality** — aggregate token reduction vs the shadow-replay baseline must
       beat ``min_reduction_pct``.

    Returns ``INSUFFICIENT_DATA`` when no row carries all measured axes — honest
    about an unproven hypothesis rather than asserting one.
    """
    thresholds = {
        "min_triads": float(min_triads),
        "min_runs": float(min_runs),
        "min_reduction_pct": min_reduction_pct,
    }
    counted = [r for r in rows if r.counts_in_proof]
    if not counted:
        return ProofVerdict(
            status=ProofStatus.INSUFFICIENT_DATA,
            counted_rows=0,
            runs=0,
            token_reduction_pct=None,
            grounding_regressions=0,
            reason=(
                "No fully-measured, outer-gate-accepted lower-tier rows. One or "
                "more routing, retry pairing, token, grounding, acceptance, or "
                "opt-in shadow-replay measurements are missing, so the hypothesis "
                "is not yet testable."
            ),
            thresholds=thresholds,
        )

    # 1. Grounding veto (per-AC, epsilon=0).
    regressions = sum(1 for r in counted if r.grounding_regression)
    if regressions:
        return ProofVerdict(
            status=ProofStatus.FAIL_GROUNDING_REGRESSION,
            counted_rows=len(counted),
            runs=_distinct_runs(counted),
            token_reduction_pct=_reduction_pct(counted),
            grounding_regressions=regressions,
            reason=(
                f"{regressions} AC(s) lost grounding at a lower model tier "
                "(newly-rejected TraceGuard claim) — do not merge."
            ),
            thresholds=thresholds,
        )

    # 2. Sample sufficiency.
    runs = _distinct_runs(counted)
    if len(counted) < min_triads or runs < min_runs:
        return ProofVerdict(
            status=ProofStatus.INSUFFICIENT_SAMPLE,
            counted_rows=len(counted),
            runs=runs,
            token_reduction_pct=_reduction_pct(counted),
            grounding_regressions=0,
            reason=(
                f"{len(counted)} counted triad(s) over {runs} run(s); "
                f"need >= {min_triads} over >= {min_runs}."
            ),
            thresholds=thresholds,
        )

    # 3. Frugality.
    reduction = _reduction_pct(counted)
    if reduction is None:
        # No positive aggregate baseline to measure against (every counted row's
        # baseline was non-positive). has_all_axes already excludes such rows, so
        # this is a defensive guard against malformed/degenerate shadow-replay
        # events — report it as unmeasurable rather than crashing the gate.
        return ProofVerdict(
            status=ProofStatus.INSUFFICIENT_DATA,
            counted_rows=len(counted),
            runs=runs,
            token_reduction_pct=None,
            grounding_regressions=0,
            reason=(
                "Counted rows carry no positive shadow-replay baseline, so token "
                "reduction is unmeasurable — the baseline producer emitted a "
                "degenerate value."
            ),
            thresholds=thresholds,
        )
    if reduction < min_reduction_pct:
        return ProofVerdict(
            status=ProofStatus.FAIL_NO_FRUGALITY,
            counted_rows=len(counted),
            runs=runs,
            token_reduction_pct=reduction,
            grounding_regressions=0,
            reason=(
                f"Aggregate token reduction {reduction:.2f}% < {min_reduction_pct:.2f}% — "
                "decomposition overhead was not beaten by real savings."
            ),
            thresholds=thresholds,
        )

    return ProofVerdict(
        status=ProofStatus.PASS,
        counted_rows=len(counted),
        runs=runs,
        token_reduction_pct=reduction,
        grounding_regressions=0,
        reason=(
            f"Proven: {len(counted)} enforced triads over {runs} runs, zero grounding "
            f"regressions, {reduction:.2f}% aggregate token reduction."
        ),
        thresholds=thresholds,
    )


def _distinct_runs(rows: list[FrugalityTriadRow]) -> int:
    runs = {r.seed_run_id for r in rows if r.seed_run_id is not None}
    # Rows without a run id collapse to one implicit run.
    if any(r.seed_run_id is None for r in rows):
        runs.add(None)
    return len(runs)


def _reduction_pct(rows: list[FrugalityTriadRow]) -> float | None:
    baseline = sum(r.baseline_token_spend or 0.0 for r in rows)
    spent = sum(r.token_spend or 0.0 for r in rows)
    # Individual rows are finite, but summing a large cohort can still overflow
    # to infinity. A non-finite aggregate would make ``(inf - inf) / inf`` NaN;
    # comparing NaN with the threshold is always false and could otherwise fall
    # through to a false PASS. Treat overflow as unmeasurable instead.
    if not math.isfinite(baseline) or not math.isfinite(spent) or baseline <= 0:
        return None
    reduction = (baseline - spent) / baseline * 100.0
    return reduction if math.isfinite(reduction) else None
