"""Where candidate outputs differ: stability, dropped probes, groups, base relation.

Input: the outcome records of a probe pool, exactly two repeats per probe on
the base and on every candidate tree. Output: a ``DisagreementReport`` that
puts every probe in exactly one bucket. Pure and deterministic: the result
depends only on the records, never on their order.

Rules, applied per probe in this order:

1. Stability. On each tree, the two repeats are compared field by field on
   canonical bytes. A field that differs on any tree (base included), or is
   present in one repeat and absent in the other, is unstable for that
   probe and is masked on every tree. If ``status`` differs between the
   repeats on any tree, the whole record is unstable and the probe is
   dropped (``DropReason.UNSTABLE``).
2. Executability. A probe whose status is ``not_run`` on every candidate tree
   is dropped (``DropReason.NOT_RUN``). A probe that runs on some candidates
   and not on others is kept; ``not_run`` is then one outcome among others.
   A probe that does not run on the base but runs on a candidate is kept.
3. Grouping. The masked outcomes of the candidate trees are grouped. Two
   outcomes match when they have the same keys, ``value`` compares equal
   under ``boundary.harness.equal`` with no tolerance (the comparator the
   controller uses for a check), and every other key has the same canonical
   bytes. ``equal`` is not guaranteed to be transitive (it allows a few
   units in the last place for floats), so grouping is fixed: outcomes are
   visited in order of their canonical bytes (then candidate id) and each
   joins the first group whose representative (its first member) it
   matches. Groups are ordered by the representative's canonical bytes.
   An integer too large for a float never equals a float (``equal`` would
   raise ``OverflowError`` converting it; that pair counts as unequal).
4. Base relation. Each group is marked ``same_as_base`` or
   ``changed_from_base`` by matching its representative against the masked
   base outcome with the same rule.

Buckets: two or more groups is a disagreement (a question candidate). One
group that changed from the base is a consensus change: it is recorded and
never asked, because no requester fixed it. One group the same as the base
is unchanged. With a single candidate there is always one group, so no
probe is ever a disagreement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ouroboros.boundary.harness import equal
from ouroboros.examples.record import (
    BASE_TREE_ID,
    REPEATS,
    STATUS_KEY,
    VALUE_KEY,
    OutcomeRecord,
    ProbeStatus,
    canonical_json_bytes,
    freeze_json,
)


class DropReason(StrEnum):
    """Why a probe was left out before grouping."""

    UNSTABLE = "unstable"
    NOT_RUN = "not_run"


class BaseRelation(StrEnum):
    """How a group's outcome relates to the base outcome on the same probe."""

    SAME_AS_BASE = "same_as_base"
    CHANGED_FROM_BASE = "changed_from_base"


@dataclass(frozen=True, slots=True)
class OutcomeGroup:
    """Candidate trees whose masked outcomes match on one probe."""

    candidate_ids: tuple[str, ...]
    outcome: Mapping[str, Any]
    outcome_bytes: bytes
    base_relation: BaseRelation


@dataclass(frozen=True, slots=True)
class ProbeComparison:
    """The kept result for one probe: masked fields and candidate groups."""

    probe_id: str
    unstable_fields: tuple[str, ...]
    base_outcome: Mapping[str, Any]
    groups: tuple[OutcomeGroup, ...]

    @property
    def is_disagreement(self) -> bool:
        return len(self.groups) >= 2

    @property
    def is_consensus_change(self) -> bool:
        return (
            len(self.groups) == 1 and self.groups[0].base_relation is BaseRelation.CHANGED_FROM_BASE
        )


@dataclass(frozen=True, slots=True)
class DroppedProbe:
    """A probe left out before grouping, and why."""

    probe_id: str
    reason: DropReason


@dataclass(frozen=True, slots=True)
class DisagreementReport:
    """Every probe in exactly one bucket, each bucket ordered by probe id."""

    disagreements: tuple[ProbeComparison, ...]
    consensus_changes: tuple[ProbeComparison, ...]
    unchanged: tuple[ProbeComparison, ...]
    dropped: tuple[DroppedProbe, ...]


def _values_equal(left: Any, right: Any) -> bool:
    """``harness.equal`` with no tolerance; an integer beyond float range is unequal.

    ``equal`` converts an integer to ``float`` when the other side is a
    float, which raises ``OverflowError`` for an integer above about
    1.8e308. No float equals such an integer, so the pair is unequal, as the
    controller's own ``harness.compare`` decides a case that raises.
    """
    try:
        return equal(left, right, None)
    except OverflowError:
        return False


def outcomes_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two masked outcome payloads count as the same output."""
    if set(left) != set(right):
        return False
    for key in left:
        if key == VALUE_KEY:
            if not _values_equal(left[key], right[key]):
                return False
        elif canonical_json_bytes(left[key]) != canonical_json_bytes(right[key]):
            return False
    return True


def unstable_fields(pairs: Iterable[tuple[OutcomeRecord, OutcomeRecord]]) -> frozenset[str]:
    """Return the payload keys that differ between repeats on any tree.

    A key present in one repeat and absent in the other differs. ``status``
    is included when the statuses differ.
    """
    unstable: set[str] = set()
    for first, second in pairs:
        one, two = first.payload(), second.payload()
        for key in set(one) | set(two):
            if (
                key not in one
                or key not in two
                or canonical_json_bytes(one[key]) != canonical_json_bytes(two[key])
            ):
                unstable.add(key)
    return frozenset(unstable)


def masked_outcome(record: OutcomeRecord, mask: frozenset[str]) -> dict[str, Any]:
    """Return the record's payload without the masked keys."""
    return {key: value for key, value in record.payload().items() if key not in mask}


def group_outcomes(
    outcomes: Mapping[str, Mapping[str, Any]],
) -> list[tuple[dict[str, Any], bytes, tuple[str, ...]]]:
    """Group candidate outcomes; return (representative, its bytes, sorted ids)."""
    ordered = sorted(
        (
            (canonical_json_bytes(dict(outcome)), tree_id, outcome)
            for tree_id, outcome in outcomes.items()
        ),
        key=lambda item: (item[0], item[1]),
    )
    groups: list[tuple[dict[str, Any], bytes, list[str]]] = []
    for outcome_bytes, tree_id, outcome in ordered:
        for representative, _, members in groups:
            if outcomes_match(representative, outcome):
                members.append(tree_id)
                break
        else:
            groups.append((dict(outcome), outcome_bytes, [tree_id]))
    return [(rep, rep_bytes, tuple(sorted(members))) for rep, rep_bytes, members in groups]


def _index_records(
    records: Iterable[OutcomeRecord], tree_ids: frozenset[str]
) -> dict[str, dict[str, dict[int, OutcomeRecord]]]:
    index: dict[str, dict[str, dict[int, OutcomeRecord]]] = {}
    for record in records:
        if record.tree_id not in tree_ids:
            raise ValueError(f"record for unknown tree {record.tree_id!r}")
        repeats = index.setdefault(record.probe_id, {}).setdefault(record.tree_id, {})
        if record.repeat in repeats:
            raise ValueError(
                f"duplicate record: probe {record.probe_id!r}, tree {record.tree_id!r}, "
                f"repeat {record.repeat}"
            )
        repeats[record.repeat] = record
    for probe_id, by_tree in index.items():
        for tree_id in sorted(tree_ids):
            got = sorted(by_tree.get(tree_id, {}))
            if tuple(got) != REPEATS:
                raise ValueError(
                    f"probe {probe_id!r} on tree {tree_id!r} needs repeats {list(REPEATS)}, "
                    f"got {got}"
                )
    return index


def _compare_probe(
    probe_id: str,
    by_tree: Mapping[str, Mapping[int, OutcomeRecord]],
    candidate_ids: Sequence[str],
    base_id: str,
) -> ProbeComparison | DroppedProbe:
    pairs = [(by_tree[tree_id][0], by_tree[tree_id][1]) for tree_id in sorted(by_tree)]
    mask = unstable_fields(pairs)
    if STATUS_KEY in mask:
        return DroppedProbe(probe_id, DropReason.UNSTABLE)
    if all(by_tree[tree_id][0].status is ProbeStatus.NOT_RUN for tree_id in candidate_ids):
        return DroppedProbe(probe_id, DropReason.NOT_RUN)
    base_outcome = masked_outcome(by_tree[base_id][0], mask)
    outcomes = {tree_id: masked_outcome(by_tree[tree_id][0], mask) for tree_id in candidate_ids}
    groups = tuple(
        OutcomeGroup(
            candidate_ids=ids,
            outcome=freeze_json(representative),
            outcome_bytes=rep_bytes,
            base_relation=(
                BaseRelation.SAME_AS_BASE
                if outcomes_match(representative, base_outcome)
                else BaseRelation.CHANGED_FROM_BASE
            ),
        )
        for representative, rep_bytes, ids in group_outcomes(outcomes)
    )
    return ProbeComparison(
        probe_id=probe_id,
        unstable_fields=tuple(sorted(mask)),
        base_outcome=freeze_json(base_outcome),
        groups=groups,
    )


def find_disagreements(
    records: Iterable[OutcomeRecord],
    candidate_ids: Sequence[str],
    base_id: str = BASE_TREE_ID,
) -> DisagreementReport:
    """Sort every probe in ``records`` into one bucket of a ``DisagreementReport``.

    ``candidate_ids`` names every candidate tree; it must be non-empty, have
    no duplicates, and not contain ``base_id``. Every probe must have exactly
    one record per repeat on the base and on every candidate; anything else
    (a missing or duplicate repeat, an unknown tree) raises ``ValueError``.
    A producer that could not start a probe on a tree still writes two
    ``not_run`` records for it.
    """
    candidates = tuple(candidate_ids)
    if not candidates:
        raise ValueError("candidate_ids must not be empty")
    if len(set(candidates)) != len(candidates):
        raise ValueError("candidate_ids must not repeat")
    if base_id in candidates:
        raise ValueError(f"base id {base_id!r} must not be a candidate id")
    index = _index_records(records, frozenset(candidates) | {base_id})
    disagreements: list[ProbeComparison] = []
    consensus: list[ProbeComparison] = []
    unchanged: list[ProbeComparison] = []
    dropped: list[DroppedProbe] = []
    for probe_id in sorted(index):
        result = _compare_probe(probe_id, index[probe_id], candidates, base_id)
        if isinstance(result, DroppedProbe):
            dropped.append(result)
        elif result.is_disagreement:
            disagreements.append(result)
        elif result.is_consensus_change:
            consensus.append(result)
        else:
            unchanged.append(result)
    return DisagreementReport(
        disagreements=tuple(disagreements),
        consensus_changes=tuple(consensus),
        unchanged=tuple(unchanged),
        dropped=tuple(dropped),
    )
