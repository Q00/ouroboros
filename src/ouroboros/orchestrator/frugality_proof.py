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

A row only ``counts_in_proof`` when effort was ENFORCED, the decomposition was
trustworthy, and all axes are present. The gate therefore returns
``INSUFFICIENT_DATA`` honestly until the token / grounding / baseline producers are
wired — and the *same* gate yields PASS/FAIL once they are. The contract (event
types + fields) is fixed here so the producers have a precise target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

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
        return (
            self.token_spend is not None
            and self.baseline_token_spend is not None
            and self.grounding_regression is not None
        )

    @property
    def counts_in_proof(self) -> bool:
        """Only enforced + trustworthy + fully-measured rows count.

        Advised effort, untrustworthy (forced-atomic) decomposition, or a missing
        axis all exclude the row — the exact honesty the deterministic proof needs.
        """
        return self.is_enforced and self.decomposition_trustworthy and self.has_all_axes


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
    """Join the per-axis events into one triad row per ``ac_id``.

    Accepts events as mappings or objects exposing ``type``/``event_type`` and
    ``data``/``payload``. Unknown event types are ignored. Rows are keyed by
    ``ac_id``; an event without an ``ac_id`` cannot be correlated and is skipped.
    """
    acc: dict[str, dict] = {}

    def slot(data: Mapping) -> dict | None:
        ac_id = data.get("ac_id")
        if not ac_id:
            return None
        return acc.setdefault(str(ac_id), {"ac_id": str(ac_id)})

    for event in events:
        etype = _event_type(event)
        data = _event_data(event)
        if etype == EVENT_EFFORT_ROUTED:
            row = slot(data)
            if row is None:
                continue
            row["effort_level"] = data.get("effort_level")
            row["effort_mode"] = data.get("effort_mode")
            row["is_decomposed_child"] = bool(data.get("is_decomposed_child", False))
            if data.get("parent_effort") is not None:
                row["parent_effort"] = data.get("parent_effort")
            if data.get("seed_run_id") is not None:
                row["seed_run_id"] = data.get("seed_run_id")
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
            if data.get("grounding_regression") is not None:
                row["grounding_regression"] = bool(data.get("grounding_regression"))
        elif etype == EVENT_SHADOW_REPLAY:
            row = slot(data)
            if row is None:
                continue
            row["baseline_token_spend"] = data.get("baseline_token_spend")
            row["baseline_mode"] = data.get("baseline_mode")
            if data.get("decomposition_trustworthy") is not None:
                row["decomposition_trustworthy"] = bool(data.get("decomposition_trustworthy"))

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
    if reduction is None or reduction < min_reduction_pct:
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
