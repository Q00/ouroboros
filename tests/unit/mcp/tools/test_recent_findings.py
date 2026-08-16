"""Recent findings reach a lane, and the boundary is recency (RFC #2153).

The decision these hold is that a lane may answer from a finding another lane
produced recently, whichever session produced it — so what is asserted here is
mostly the *absence* of a session, and absences have no failing behaviour to
point at later if a guard is quietly restored.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest

from ouroboros.mcp.tools.question_advisory import (
    attach_question_advisory,
    build_question_advisory_subagents,
)
from ouroboros.mcp.tools.recent_findings import (
    RECENT_FINDINGS_WINDOW_SECONDS,
    recent_finding_paths,
)
from ouroboros.orchestrator.capabilities.pm_schemas import pm_repository_roster

QUESTION = "What happens today when a subscription lapses mid-period?"


@pytest.fixture
def roster() -> list[dict[str, str]]:
    return pm_repository_roster([{"path": "/repo/api", "name": "api"}])


def _publish(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    """Write one finding body the way the artifact store lays them out."""
    shard = root / name[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / f"{name}.json"
    path.write_text(json.dumps({"fanout_id": f"fanout_{name}"}), encoding="utf-8")
    if age_seconds:
        written = time.time() - age_seconds
        import os

        os.utime(path, (written, written))
    return path


def _lane_prompts(meta: dict[str, Any]) -> dict[str, str]:
    return {
        payload.context["lane_id"]: payload.to_dict()["prompt"]
        for payload in build_question_advisory_subagents(meta["question_advisory_request"])
    }


def _attach(roster: list[dict[str, str]], root: Path | None, **kwargs: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_pm_interview",
        session_id=kwargs.pop("session_id", "pm-1"),
        question=kwargs.pop("question", QUESTION),
        repository_roster=roster,
        findings_root=root,
        **kwargs,
    )
    return meta


def test_a_finding_from_another_session_is_offered(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """The decision, stated as the thing that used to be filtered out.

    Nothing about the session reaches this lookup, which is the point: a
    finding describes the system, and the system does not change at session
    granularity. Asserted on both lanes because both report on the system.
    """
    published = _publish(tmp_path, "aa" + "0" * 62)

    prompts = _lane_prompts(_attach(roster, tmp_path))

    assert set(prompts) == {"code_context", "data_context"}
    for prompt in prompts.values():
        assert "## Recently Found Here" in prompt
        assert str(published) in prompt


def test_a_finding_older_than_the_window_is_not_offered(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """Recency is the boundary, so something has to fall outside it."""
    fresh = _publish(tmp_path, "bb" + "1" * 62)
    stale = _publish(tmp_path, "cc" + "2" * 62, age_seconds=RECENT_FINDINGS_WINDOW_SECONDS + 60)

    prompt = _lane_prompts(_attach(roster, tmp_path))["code_context"]

    assert str(fresh) in prompt
    assert str(stale) not in prompt


def test_a_project_with_nothing_recent_is_told_nothing(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """An empty place is worse than no place: looking there costs a tool call."""
    _publish(tmp_path, "dd" + "3" * 62, age_seconds=RECENT_FINDINGS_WINDOW_SECONDS * 3)

    for prompt in _lane_prompts(_attach(roster, tmp_path)).values():
        assert "## Recently Found Here" not in prompt


def test_a_caller_with_no_root_still_gets_its_lanes(roster: list[dict[str, str]]) -> None:
    """Advisory: losing the shortcut costs a child a place to look, not a turn.

    This is also what keeps every other tool sharing this producer untouched —
    a caller that passes no root pays nothing and reads nothing.
    """
    prompts = _lane_prompts(_attach(roster, None))

    assert set(prompts) == {"code_context", "data_context"}
    for prompt in prompts.values():
        assert "## Recently Found Here" not in prompt


def test_the_lane_is_told_the_roster_does_not_travel_with_the_findings(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """The one thing that does not carry across sessions.

    A session chooses which repositories it is asking about, and a claim about
    any other is rejected at submission. The child is told where it is deciding
    what to read, rather than discovering it when its answer is refused.
    """
    _publish(tmp_path, "ee" + "4" * 62)

    prompt = _lane_prompts(_attach(roster, tmp_path))["code_context"]

    assert "other sessions" in prompt
    assert "rejected" in prompt


def test_the_producer_hands_over_paths_and_never_what_a_child_found(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """The prompt cannot grow with what was found, only with how many files.

    This is why the lookup reads nothing. Inlining findings would make every
    round pay for every earlier round and would make this server pick which of
    them matter without having read the question; it would also put
    child-authored text on the producing side, which this fan-out keeps free of
    it independently of this feature.
    """
    claim = "a sentence only a child could have written"
    shard = tmp_path / "ff"
    shard.mkdir(parents=True)
    (shard / f"{'ff' + '5' * 62}.json").write_text(
        json.dumps({"result": {"aggregated_outputs": [{"output": {"claim": claim}}]}}),
        encoding="utf-8",
    )

    meta = _attach(roster, tmp_path)

    assert claim not in json.dumps(meta["question_advisory_request"])
    assert claim not in _lane_prompts(meta)["code_context"]


def test_the_store_s_own_bookkeeping_is_not_a_finding(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """``contracts`` and ``bindings`` are how the store tracks itself.

    Excluded by the shape of the walk rather than by naming them, so a
    directory the store adds later does not have to be remembered here.
    """
    _publish(tmp_path, "aa" + "6" * 62)
    for bookkeeping in ("contracts", "bindings"):
        directory = tmp_path / bookkeeping
        directory.mkdir()
        (directory / "events.json").write_text("{}", encoding="utf-8")

    paths = recent_finding_paths(tmp_path)

    assert len(paths) == 1
    assert "contracts" not in paths[0]
    assert "bindings" not in paths[0]


def test_an_unreadable_root_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    """The turn belongs to the question; a missing shortcut must not take it."""
    assert recent_finding_paths(tmp_path / "absent") == []
    assert recent_finding_paths(None) == []
