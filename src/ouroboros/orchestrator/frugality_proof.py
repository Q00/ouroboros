"""Deterministic frugality-proof machine (the seed's FrugalityProofTriad gate).

The hypothesis the seed exists to prove: *if work is decomposed well, each child
runs at a lower reasoning-effort and stays token-frugal WITHOUT losing grounding.*
This module is the deterministic, LLM-free judge of that hypothesis. It reads the
event stream a run produces, assembles one :class:`FrugalityTriadRow` per AC, and
computes a PASS/FAIL verdict — no model is asked anything, so the proof cannot be
reward-hacked.

A triad row joins three measured axes by ``ac_id``:

* **effort** — ``execution.ac.effort_routed`` (effort_level + effort_mode). Emitted
  today by the effort contract.
* **token** — ``execution.ac.token_attribution.reported`` (token_spend). Production
  side not wired yet (seed AC2).
* **grounding** — ``execution.ac.deliver_verdict`` (traceguard_verdict +
  unsupported_claim_rate). Production side not wired yet (seed AC4).
* **baseline** — ``execution.ac.shadow_replay`` (baseline_token_spend at parent
  effort). Production side not wired yet (seed AC5).

A row only ``counts_in_proof`` when effort was ENFORCED, the unit is a decomposed
child (the hypothesis is about children, not top-level ACs), the decomposition was
trustworthy, and all axes are present. The gate therefore returns
``INSUFFICIENT_DATA`` honestly until the token / grounding / baseline producers are
wired — and the *same* gate yields PASS/FAIL once they are. The contract (event
types + fields) is fixed here so the producers have a precise target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import math


def _finite_number(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it is not a usable number.

    Rejects ``None``, booleans (``True``/``False`` are ints in Python but are never
    a valid token measurement), non-numeric types, and non-finite floats (NaN/inf).
    The deterministic proof is fed by event payloads from not-yet-wired producers, so
    a malformed measurement must be treated as *missing*, never silently trusted.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _strict_bool(value: object) -> bool | None:
    """Return ``value`` only if it is a real boolean, else ``None``.

    Proof-admission flags (``is_decomposed_child``, ``decomposition_trustworthy``,
    ``grounding_regression``) must never be derived via Python truthiness: a JSON
    boolean deserializes to ``bool``, but a malformed string payload like
    ``"false"`` would coerce to ``True`` under ``bool("false")`` and flip an
    admission flag the wrong way. Anything that is not already a ``bool`` is treated
    as unknown so the caller can fail safe (exclude the row) rather than admit it.
    """
    return value if isinstance(value, bool) else None


# -- Event-type contract the producers must emit -----------------------------
EVENT_EFFORT_ROUTED = "execution.ac.effort_routed"
EVENT_TOKEN_ATTRIBUTION = "execution.ac.token_attribution.reported"
EVENT_DELIVER_VERDICT = "execution.ac.deliver_verdict"
EVENT_SHADOW_REPLAY = "execution.ac.shadow_replay"

EFFORT_MODE_ENFORCED = "enforced"

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
    """One AC's measured triad (token x effort x grounding) + its baseline."""

    ac_id: str
    seed_run_id: str | None = None
    is_decomposed_child: bool = False
    decomposition_trustworthy: bool = True
    # effort axis
    effort_level: str | None = None
    effort_mode: str | None = None
    parent_effort: str | None = None
    # token axis
    token_spend: float | None = None
    baseline_token_spend: float | None = None
    baseline_mode: str | None = None
    # grounding axis
    traceguard_verdict: str | None = None
    unsupported_claim_rate: float | None = None
    grounding_regression: bool | None = None

    @property
    def is_enforced(self) -> bool:
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
        return (
            token is not None
            and token >= 0
            and baseline is not None
            and baseline > 0
            and bool(self.traceguard_verdict)
            and claim_rate is not None
            and 0.0 <= claim_rate <= 1.0
            and self.grounding_regression is not None
        )

    @property
    def counts_in_proof(self) -> bool:
        """Only enforced + decomposed-child + trustworthy + fully-measured rows count.

        The hypothesis is specifically about *decomposed children* running at a
        lower effort than their parent (see module docstring), so a top-level AC
        (``is_decomposed_child=False``) is excluded even when fully measured —
        otherwise a sample of ordinary top-level executions could PASS the gate and
        "prove" a frugality claim the run never tested. A top-level unit also has no
        parent effort to lower from and no shadow-replay baseline that means
        anything. Advised effort, untrustworthy (forced-atomic) decomposition, or a
        missing axis likewise exclude the row — the exact honesty the deterministic
        proof needs.
        """
        return (
            self.is_enforced
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


def _event_type(event: object) -> str | None:
    if isinstance(event, Mapping):
        return event.get("type") or event.get("event_type")
    return getattr(event, "type", None) or getattr(event, "event_type", None)


def _event_data(event: object) -> Mapping:
    if isinstance(event, Mapping):
        data = event.get("data") or event.get("payload") or {}
    else:
        data = getattr(event, "data", None) or getattr(event, "payload", None) or {}
    return data if isinstance(data, Mapping) else {}


def assemble_triads(events: Iterable[object]) -> list[FrugalityTriadRow]:
    """Join the per-axis events into one triad row per ``(run, ac_id)``.

    Accepts events as mappings or objects exposing ``type``/``event_type`` and
    ``data``/``payload``. Unknown event types are ignored. An event without an
    ``ac_id`` cannot be correlated and is skipped.

    Skipping the ``ac_id``-less event is **by design, not a gap**: the proof is a
    per-decomposed-AC measurement, and the whole-seed direct-runner effort event
    (``OrchestratorRunner._route_call_effort``) is emitted without a per-AC id
    because a non-decomposed single-call run has no child to lower effort on and no
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

    def slot(data: Mapping) -> dict | None:
        ac_id = data.get("ac_id")
        if not ac_id:
            return None
        run = data.get("seed_run_id") or data.get("execution_id")
        run_key = str(run) if run is not None else None
        return acc.setdefault((run_key, str(ac_id)), {"ac_id": str(ac_id), "seed_run_id": run_key})

    for event in events:
        etype = _event_type(event)
        data = _event_data(event)
        if etype == EVENT_EFFORT_ROUTED:
            row = slot(data)
            if row is None:
                continue
            row["effort_level"] = data.get("effort_level")
            row["effort_mode"] = data.get("effort_mode")
            # Strict boolean: a malformed/missing value fails safe to False, which
            # excludes the row (counts_in_proof requires a decomposed child) rather
            # than admitting a top-level row whose flag was a truthy string.
            row["is_decomposed_child"] = _strict_bool(data.get("is_decomposed_child")) or False
            if data.get("parent_effort") is not None:
                row["parent_effort"] = data.get("parent_effort")
            # seed_run_id is established as part of the row key (see ``slot``), so it
            # is not re-read here.
        elif etype == EVENT_TOKEN_ATTRIBUTION:
            row = slot(data)
            if row is None:
                continue
            row["token_spend"] = data.get("token_spend")
        elif etype == EVENT_DELIVER_VERDICT:
            row = slot(data)
            if row is None:
                continue
            row["traceguard_verdict"] = data.get("traceguard_verdict")
            row["unsupported_claim_rate"] = data.get("unsupported_claim_rate")
            # Strict boolean: a malformed grounding flag stays None (unset) so
            # has_all_axes excludes the row, rather than a truthy string defaulting
            # the veto to a value the producer did not actually measure.
            grounding = _strict_bool(data.get("grounding_regression"))
            if grounding is not None:
                row["grounding_regression"] = grounding
        elif etype == EVENT_SHADOW_REPLAY:
            row = slot(data)
            if row is None:
                continue
            row["baseline_token_spend"] = data.get("baseline_token_spend")
            row["baseline_mode"] = data.get("baseline_mode")
            # Strict boolean: a present-but-malformed trustworthiness flag fails safe
            # to False (excludes the row) instead of a truthy string admitting an
            # untrustworthy decomposition into the proof.
            if data.get("decomposition_trustworthy") is not None:
                trustworthy = _strict_bool(data.get("decomposition_trustworthy"))
                row["decomposition_trustworthy"] = trustworthy if trustworthy is not None else False

    return [
        FrugalityTriadRow(
            ac_id=v["ac_id"],
            seed_run_id=v.get("seed_run_id"),
            is_decomposed_child=v.get("is_decomposed_child", False),
            decomposition_trustworthy=v.get("decomposition_trustworthy", True),
            effort_level=v.get("effort_level"),
            effort_mode=v.get("effort_mode"),
            parent_effort=v.get("parent_effort"),
            token_spend=v.get("token_spend"),
            baseline_token_spend=v.get("baseline_token_spend"),
            baseline_mode=v.get("baseline_mode"),
            traceguard_verdict=v.get("traceguard_verdict"),
            unsupported_claim_rate=v.get("unsupported_claim_rate"),
            grounding_regression=v.get("grounding_regression"),
        )
        for v in acc.values()
    ]


def evaluate_proof(
    rows: Iterable[FrugalityTriadRow],
    *,
    min_triads: int = DEFAULT_MIN_TRIADS,
    min_runs: int = DEFAULT_MIN_RUNS,
    min_reduction_pct: float = DEFAULT_MIN_REDUCTION_PCT,
) -> ProofVerdict:
    """Deterministically judge the frugality hypothesis from triad rows.

    Order of checks (the seed's exit conditions):

    1. **Grounding is a per-AC veto** — any counted row whose lower-effort run
       produced a newly-rejected claim (``grounding_regression``) fails the proof
       outright; lowering effort must never reduce grounding.
    2. **Sample sufficiency** — at least ``min_triads`` counted rows across at least
       ``min_runs`` runs, else the result is anecdotal.
    3. **Frugality** — aggregate token reduction vs the shadow-replay baseline must
       beat ``min_reduction_pct``.

    Returns ``INSUFFICIENT_DATA`` when no row carries all axes (the token / grounding
    / baseline producers are not wired yet) — honest about an unproven hypothesis
    rather than asserting one.
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
                "No fully-measured enforced rows. The effort axis is produced, but "
                "the token / grounding / shadow-replay axes are not wired yet, so the "
                "hypothesis is not yet testable."
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
                f"{regressions} AC(s) lost grounding at lower effort "
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
    if baseline <= 0:
        return None
    return (baseline - spent) / baseline * 100.0
