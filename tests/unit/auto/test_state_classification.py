"""Persistence tests for the goal-classification state extension (#689 PR-B)."""

from __future__ import annotations

import json

import pytest

from ouroboros.auto.goal_classifier import classify_goal
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore


def _make_state() -> AutoPipelineState:
    return AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")


def test_state_default_classification_is_none() -> None:
    state = _make_state()
    assert state.classification is None
    assert state.direct_path_reason is None


def test_record_classification_persists_dict_and_reason() -> None:
    state = _make_state()
    classification = classify_goal(
        "https://github.com/Q00/ouroboros/pull/689 review please"
    )

    state.record_classification(classification)

    assert state.classification == classification.to_dict()
    assert state.direct_path_reason == classification.reason


def test_record_classification_clears_reason_when_interview_required() -> None:
    state = _make_state()
    classification = classify_goal("just plan the migration please")
    state.record_classification(classification)

    # Goal routed to interview, so direct_path_reason must remain None to
    # avoid confusing CLI output that suggests a direct path was taken.
    assert classification.direct_run_allowed is False
    assert state.classification == classification.to_dict()
    assert state.direct_path_reason is None


def test_record_classification_rejects_non_classification() -> None:
    state = _make_state()
    with pytest.raises(TypeError):
        state.record_classification({"reason": "not a classification"})  # type: ignore[arg-type]


def test_state_round_trips_classification_through_store(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _make_state()
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    classification = classify_goal(
        "merge https://github.com/Q00/ouroboros/pull/689 once CI is green"
    )
    state.record_classification(classification)

    path = store.save(state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["classification"] == classification.to_dict()
    assert raw["direct_path_reason"] == classification.reason

    loaded = store.load(state.auto_session_id)
    assert loaded.classification == classification.to_dict()
    assert loaded.direct_path_reason == classification.reason


def test_state_loads_legacy_record_without_classification(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _make_state()
    path = store.save(state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("classification", None)
    raw.pop("direct_path_reason", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.load(state.auto_session_id)
    assert loaded.classification is None
    assert loaded.direct_path_reason is None


def test_state_rejects_invalid_classification_payload(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _make_state()
    path = store.save(state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["classification"] = {"interview_required": True}  # missing fields
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="classification"):
        store.load(state.auto_session_id)


def test_state_rejects_blank_direct_path_reason(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = _make_state()
    path = store.save(state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["direct_path_reason"] = "   "
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="direct_path_reason"):
        store.load(state.auto_session_id)


def test_ledger_default_direct_path_reason_is_none() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI tool")
    assert ledger.direct_path_reason is None


def test_ledger_record_direct_path_reason_persists_through_round_trip() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI tool")
    ledger.record_direct_path_reason(
        "concrete PR/issue URL paired with operational verb (low risk)"
    )
    serialized = ledger.to_dict()
    assert (
        serialized["direct_path_reason"]
        == "concrete PR/issue URL paired with operational verb (low risk)"
    )

    restored = SeedDraftLedger.from_dict(serialized)
    assert restored.direct_path_reason == ledger.direct_path_reason


def test_ledger_legacy_dict_without_direct_path_reason_loads_clean() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI tool")
    payload = ledger.to_dict()
    payload.pop("direct_path_reason", None)
    restored = SeedDraftLedger.from_dict(payload)
    assert restored.direct_path_reason is None


def test_ledger_rejects_blank_direct_path_reason() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI tool")
    with pytest.raises(ValueError, match="direct_path_reason"):
        ledger.record_direct_path_reason("   ")


def test_ledger_from_dict_rejects_blank_direct_path_reason() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI tool")
    payload = ledger.to_dict()
    payload["direct_path_reason"] = "   "
    with pytest.raises(ValueError, match="direct_path_reason"):
        SeedDraftLedger.from_dict(payload)
