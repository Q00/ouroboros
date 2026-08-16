"""Recent findings reach a lane, and the boundary is recency (RFC #2153).

The decision these hold is that a lane may answer from a finding another lane
produced recently, whichever session produced it — so what is asserted here is
mostly the *absence* of a session, and absences have no failing behaviour to
point at later if a guard is quietly restored.
"""

from __future__ import annotations

import hashlib
import json
import os
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


def _publish(
    root: Path,
    *,
    age_seconds: float = 0.0,
    kind: str = "question_advisory",
    lanes: tuple[str, ...] = ("code_context", "data_context"),
    claim: str = "access continues to period end",
) -> Path:
    """Publish one body the way the artifact store does: named by its own digest.

    Written through the same rule the store writes by, rather than by hand,
    because content addressing is what the lookup checks a file against — a
    fixture that named files freely would pass a test the store's own files
    would fail.
    """
    body = json.dumps(
        {
            "kind": kind,
            "result": {
                "aggregated_outputs": [
                    {"lane_id": lane, "output": {"claim": claim}} for lane in lanes
                ]
            },
        }
    ).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    shard = root / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / f"{digest}.json"
    path.write_bytes(body)
    if age_seconds:
        written = time.time() - age_seconds
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
    published = _publish(tmp_path)

    prompts = _lane_prompts(_attach(roster, tmp_path))

    assert set(prompts) == {"code_context", "data_context"}
    for prompt in prompts.values():
        assert "## Recently Found Here" in prompt
        assert str(published) in prompt


def test_a_finding_older_than_the_window_is_not_offered(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """Recency is the boundary, so something has to fall outside it."""
    fresh = _publish(tmp_path, claim="fresh")
    stale = _publish(tmp_path, claim="stale", age_seconds=RECENT_FINDINGS_WINDOW_SECONDS + 60)

    prompt = _lane_prompts(_attach(roster, tmp_path))["code_context"]

    assert str(fresh) in prompt
    assert str(stale) not in prompt


def test_a_project_with_nothing_recent_is_told_nothing(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """An empty place is worse than no place: looking there costs a tool call."""
    _publish(tmp_path, age_seconds=RECENT_FINDINGS_WINDOW_SECONDS * 3)

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
    _publish(tmp_path)

    prompt = _lane_prompts(_attach(roster, tmp_path))["code_context"]

    assert "other sessions" in prompt
    assert "rejected" in prompt


def test_the_producer_hands_over_paths_and_never_what_a_child_found(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """The prompt cannot grow with what was found, only with how many files.

    Inlining findings would make every
    round pay for every earlier round and would make this server pick which of
    them matter without having read the question; it would also put
    child-authored text on the producing side, which this fan-out keeps free of
    it independently of this feature.
    """
    claim = "a sentence only a child could have written"
    _publish(tmp_path, claim=claim)

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
    _publish(tmp_path)
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


# ── What a listing offers is checked, because a listing is not authority ──


def test_a_body_that_is_not_what_its_name_says_is_not_offered(tmp_path: Path) -> None:
    """Content addressing is the store's naming rule, so it is the integrity test.

    A body planted by hand carries whatever claims its author wanted, and those
    reach the user through a confirmation prompt that says the code says so. It
    cannot pass here without finding a preimage for the name it sits under.
    """
    published = _publish(tmp_path)
    published.write_bytes(published.read_bytes().replace(b"period end", b"never ends"))

    assert recent_finding_paths(tmp_path) == []


def test_a_symlink_out_of_the_project_is_not_offered(tmp_path: Path) -> None:
    """The one thing this lookup could newly expose, and the RFC forbids it.

    A lane is otherwise bounded to the repositories the question named. A link
    handing back an absolute path elsewhere would put a file this project does
    not own into a child's prompt — which is cross-project reuse by another
    name. It fails the same test a forged body fails: what it resolves to does
    not hash to the name it was found under.
    """
    # Another project's store, holding a body that is entirely valid there: it
    # hashes to its own name and carries an eligible lane. Only where it lives
    # makes it not this project's, so integrity cannot be what excludes it.
    elsewhere = tmp_path / "other-project"
    elsewhere.mkdir()
    theirs = _publish(elsewhere, claim="another project's policy")

    root = tmp_path / "artifacts"
    shard = root / theirs.stem[:2]
    shard.mkdir(parents=True)
    link = shard / theirs.name
    link.symlink_to(theirs)

    offered = recent_finding_paths(root)

    assert offered == []
    assert all(str(elsewhere) not in path for path in offered)


def test_another_fanout_kind_is_not_offered(tmp_path: Path) -> None:
    """Persona panels publish into the same namespace and are not findings.

    Recency alone cannot establish that a file is an eligible finding, because
    every kind shares one content-addressed directory.
    """
    _publish(tmp_path, kind="lateral_persona_panel")

    assert recent_finding_paths(tmp_path) == []


def test_a_body_with_no_eligible_lane_is_not_offered(tmp_path: Path) -> None:
    """The RFC closes the list at two lanes, and an interview turn runs six.

    A contrarian's challenge or a drafted set of answer options is reasoning
    about one question. Reused as evidence it is an answer to a question nobody
    asked — and for PM, answer options are the one thing a lane must never hand
    the user.
    """
    _publish(tmp_path, lanes=("ambiguity_contrarian", "answer_simplifier", "web_context"))

    assert recent_finding_paths(tmp_path) == []


def test_a_mixed_body_is_offered_for_the_lanes_it_does_carry(tmp_path: Path) -> None:
    """An interview body holds eligible and ineligible lanes in one file.

    The file is offered because it carries something eligible, and the boundary
    inside it is the lane the child is told to read. Splitting the file server
    side would mean this server deciding what is relevant without having read
    the question, which is what handing over paths exists to avoid.
    """
    published = _publish(
        tmp_path,
        lanes=("code_context", "ambiguity_contrarian", "answer_simplifier"),
    )

    assert recent_finding_paths(tmp_path) == [str(published)]


def test_the_prompt_names_which_entries_may_be_read(
    roster: list[dict[str, str]], tmp_path: Path
) -> None:
    """Because the boundary inside a file cannot be enforced by the listing."""
    _publish(tmp_path, lanes=("code_context", "answer_simplifier"))

    prompt = _lane_prompts(_attach(roster, tmp_path))["code_context"]

    assert "`code_context` and `data_context`" in prompt
    assert "reasoning about a different question" in prompt
