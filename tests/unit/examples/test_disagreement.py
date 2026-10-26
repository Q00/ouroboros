"""Tests for stability masking, dropped probes, grouping and base relation."""

from __future__ import annotations

import random

import pytest

from ouroboros.examples.disagreement import (
    BaseRelation,
    DropReason,
    find_disagreements,
    group_outcomes,
    outcomes_match,
)
from ouroboros.examples.record import BASE_TREE_ID, ProbeStatus, canonical_json_bytes

from .helpers import probe, runs

CANDIDATES = ("c1", "c2", "c3", "c4")
RET = ProbeStatus.RETURNED
RAISED = ProbeStatus.RAISED
NOT_RUN = ProbeStatus.NOT_RUN


def _split_probe(probe_id: str = "p1") -> list:
    return probe(
        probe_id,
        {"value": 0},
        {"c1": {"value": 5400}, "c2": {"value": 5400}, "c3": {"value": 90}, "c4": {"value": 0}},
    )


def test_split_probe_is_a_disagreement_with_ordered_groups() -> None:
    report = find_disagreements(_split_probe(), CANDIDATES)
    assert [c.probe_id for c in report.disagreements] == ["p1"]
    groups = report.disagreements[0].groups
    # Groups ordered by canonical bytes: 0 < 5400 < 90 as JSON text.
    assert [g.candidate_ids for g in groups] == [("c4",), ("c1", "c2"), ("c3",)]
    assert [g.outcome["value"] for g in groups] == [0, 5400, 90]
    assert [g.base_relation for g in groups] == [
        BaseRelation.SAME_AS_BASE,
        BaseRelation.CHANGED_FROM_BASE,
        BaseRelation.CHANGED_FROM_BASE,
    ]
    assert not report.consensus_changes and not report.unchanged and not report.dropped


def test_one_candidate_never_disagrees() -> None:
    records = probe("p1", {"value": 0}, {"c1": {"value": 1}}) + probe(
        "p2", {"value": 0}, {"c1": {"value": 0}}
    )
    report = find_disagreements(records, ["c1"])
    assert not report.disagreements
    assert [c.probe_id for c in report.consensus_changes] == ["p1"]
    assert [c.probe_id for c in report.unchanged] == ["p2"]


def test_all_agree_same_as_base_is_unchanged() -> None:
    records = probe("p1", {"value": 1}, {c: {"value": 1} for c in CANDIDATES})
    report = find_disagreements(records, CANDIDATES)
    assert [c.probe_id for c in report.unchanged] == ["p1"]
    assert report.unchanged[0].groups[0].candidate_ids == CANDIDATES


def test_all_agree_changed_from_base_is_a_consensus_change() -> None:
    records = probe("p1", {"value": 1}, {c: {"value": 2} for c in CANDIDATES})
    report = find_disagreements(records, CANDIDATES)
    assert [c.probe_id for c in report.consensus_changes] == ["p1"]
    assert report.consensus_changes[0].is_consensus_change
    assert not report.disagreements


def test_not_run_on_every_candidate_is_dropped() -> None:
    records = probe(
        "p1", {"value": 1}, {c: {} for c in CANDIDATES}, status=dict.fromkeys(CANDIDATES, NOT_RUN)
    )
    report = find_disagreements(records, CANDIDATES)
    assert [(d.probe_id, d.reason) for d in report.dropped] == [("p1", DropReason.NOT_RUN)]


def test_not_run_on_base_only_is_kept() -> None:
    records = probe(
        "p1",
        {},
        {"c1": {"value": 1}, "c2": {"value": 2}},
        status={BASE_TREE_ID: NOT_RUN},
    )
    report = find_disagreements(records, ["c1", "c2"])
    comparison = report.disagreements[0]
    assert comparison.base_outcome == {"status": "not_run"}
    assert {g.base_relation for g in comparison.groups} == {BaseRelation.CHANGED_FROM_BASE}


def test_not_run_on_some_candidates_is_one_outcome_among_others() -> None:
    records = probe(
        "p1",
        {"value": 0},
        {"c1": {"value": 1}, "c2": {}},
        status={"c2": NOT_RUN},
    )
    report = find_disagreements(records, ["c1", "c2"])
    groups = report.disagreements[0].groups
    assert {g.candidate_ids: g.outcome["status"] for g in groups} == {
        ("c1",): "returned",
        ("c2",): "not_run",
    }


def test_runs_on_some_and_raises_on_others_is_kept() -> None:
    records = probe(
        "p1",
        {"exception_type": "AttributeError"},
        {"c1": {"value": 5400}, "c2": {"exception_type": "ValueError", "message": "bad"}},
        status={BASE_TREE_ID: RAISED, "c2": RAISED},
    )
    report = find_disagreements(records, ["c1", "c2"])
    assert len(report.disagreements[0].groups) == 2


def test_unstable_field_is_masked_on_every_tree() -> None:
    # The message differs between repeats on c1 only; it is masked on every
    # tree, so c1 and c2 now match on the stable exception type alone.
    records = (
        runs("p1", BASE_TREE_ID, RET, value=0)
        + runs(
            "p1",
            "c1",
            RAISED,
            second={"exception_type": "ValueError", "message": "id 2"},
            exception_type="ValueError",
            message="id 1",
        )
        + runs("p1", "c2", RAISED, exception_type="ValueError", message="other")
    )
    report = find_disagreements(records, ["c1", "c2"])
    comparison = report.consensus_changes[0]
    assert comparison.unstable_fields == ("message",)
    assert comparison.groups[0].candidate_ids == ("c1", "c2")
    assert comparison.groups[0].outcome == {"status": "raised", "exception_type": "ValueError"}


def test_unstable_field_on_base_masks_candidates_too() -> None:
    records = (
        runs("p1", BASE_TREE_ID, RET, second={"value": 0, "stdout": "t=2"}, value=0, stdout="t=1")
        + runs("p1", "c1", RET, value=0, stdout="a")
        + runs("p1", "c2", RET, value=0, stdout="b")
    )
    report = find_disagreements(records, ["c1", "c2"])
    assert report.unchanged[0].unstable_fields == ("stdout",)


def test_field_present_in_one_repeat_only_is_unstable() -> None:
    records = (
        runs("p1", BASE_TREE_ID, RET, value=0)
        + runs("p1", "c1", RET, second={"value": 1}, value=1, stderr="warn")
        + runs("p1", "c2", RET, value=1)
    )
    report = find_disagreements(records, ["c1", "c2"])
    assert report.consensus_changes[0].unstable_fields == ("stderr",)


def test_status_differing_between_repeats_drops_the_probe() -> None:
    records = (
        runs("p1", BASE_TREE_ID, RET, value=0)
        + runs("p1", "c1", RET, value=1, second={}, second_status=ProbeStatus.TIMED_OUT)
        + runs("p1", "c2", RET, value=2)
    )
    report = find_disagreements(records, ["c1", "c2"])
    assert [(d.probe_id, d.reason) for d in report.dropped] == [("p1", DropReason.UNSTABLE)]


def test_unstable_value_leaves_status_only() -> None:
    records = (
        runs("p1", BASE_TREE_ID, RET, value=0)
        + runs("p1", "c1", RET, second={"value": 2}, value=1)
        + runs("p1", "c2", RAISED, exception_type="KeyError")
    )
    report = find_disagreements(records, ["c1", "c2"])
    comparison = report.disagreements[0]
    assert comparison.unstable_fields == ("value",)
    assert comparison.base_outcome == {"status": "returned"}
    assert [g.base_relation for g in comparison.groups] == [
        BaseRelation.CHANGED_FROM_BASE,
        BaseRelation.SAME_AS_BASE,
    ]


def test_int_and_float_values_group_under_harness_equal() -> None:
    records = probe("p1", {"value": 0}, {"c1": {"value": 1}, "c2": {"value": 1.0}})
    report = find_disagreements(records, ["c1", "c2"])
    comparison = report.consensus_changes[0]
    assert comparison.groups[0].candidate_ids == ("c1", "c2")
    # The representative is the member with the smallest canonical bytes.
    assert comparison.groups[0].outcome_bytes == b'{"status":"returned","value":1.0}'


def test_float_rounding_noise_groups_but_distinct_floats_do_not() -> None:
    noisy = 0 + 3 * 0.1
    records = probe(
        "p1",
        {"value": 0},
        {"c1": {"value": 0.3}, "c2": {"value": noisy}, "c3": {"value": 0.31}},
    )
    groups = find_disagreements(records, ["c1", "c2", "c3"]).disagreements[0].groups
    assert [g.candidate_ids for g in groups] == [("c1", "c2"), ("c3",)]


def test_bool_and_int_values_do_not_group() -> None:
    records = probe("p1", {"value": 0}, {"c1": {"value": True}, "c2": {"value": 1}})
    groups = find_disagreements(records, ["c1", "c2"]).disagreements[0].groups
    assert len(groups) == 2


def test_non_value_fields_compare_by_canonical_bytes() -> None:
    assert not outcomes_match(
        {"status": "exited", "exit_code": 1}, {"status": "exited", "exit_code": 1.0}
    )
    assert outcomes_match({"status": "returned", "value": 1}, {"status": "returned", "value": 1.0})
    assert not outcomes_match({"status": "returned"}, {"status": "returned", "value": None})


def test_grouping_is_greedy_against_the_first_member() -> None:
    # harness.equal is not transitive for floats: a ~ b and b ~ c but not
    # a ~ c. The fixed visit order (canonical bytes) decides, every time.
    eps = 2.220446049250313e-16
    a = 1.0 + 10 * eps
    b = a + 10 * eps
    c = b + 10 * eps
    assert canonical_json_bytes(a) < canonical_json_bytes(b) < canonical_json_bytes(c)
    outcomes = {"x": {"value": a}, "y": {"value": b}, "z": {"value": c}}
    first = group_outcomes(outcomes)
    assert first == group_outcomes(dict(reversed(list(outcomes.items()))))
    assert [ids for _, _, ids in first] == [("x", "y"), ("z",)]


def test_report_is_independent_of_record_order() -> None:
    records = (
        _split_probe("p1")
        + probe("p2", {"value": 1}, {c: {"value": 1} for c in CANDIDATES})
        + probe("p3", {"value": 1}, {c: {"value": 3} for c in CANDIDATES})
        + probe("p0", {}, {c: {} for c in CANDIDATES}, status=dict.fromkeys(CANDIDATES, NOT_RUN))
    )
    expected = find_disagreements(records, CANDIDATES)
    rng = random.Random(18)
    for _ in range(25):
        shuffled = list(records)
        rng.shuffle(shuffled)
        candidates = list(CANDIDATES)
        rng.shuffle(candidates)
        assert find_disagreements(shuffled, candidates) == expected
    assert [d.probe_id for d in expected.dropped] == ["p0"]


@pytest.mark.parametrize(
    ("records", "candidates", "message"),
    [
        (probe("p1", {}, {"c1": {}}), [], "must not be empty"),
        (probe("p1", {}, {"c1": {}}), ["c1", "c1"], "must not repeat"),
        (probe("p1", {}, {"c1": {}}), ["c1", BASE_TREE_ID], "must not be a candidate"),
        (probe("p1", {}, {"c1": {}, "c9": {}}), ["c1"], "unknown tree"),
        (probe("p1", {}, {"c1": {}}), ["c1", "c2"], "needs repeats"),
        (probe("p1", {}, {"c1": {}})[:-1], ["c1"], "needs repeats"),
        (probe("p1", {}, {"c1": {}}) + runs("p1", "c1")[:1], ["c1"], "duplicate record"),
    ],
)
def test_malformed_record_sets_are_refused(records: list, candidates: list, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        find_disagreements(records, candidates)


def test_no_records_gives_an_empty_report() -> None:
    report = find_disagreements([], ["c1", "c2"])
    assert report.disagreements == report.consensus_changes == report.unchanged == ()
    assert report.dropped == ()
