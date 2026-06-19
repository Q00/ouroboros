"""Tests for ``_unsafe_context_reason`` behaviour with user-declared non-goal
sections inside the free-form ``goal`` argument.

The detector already documents that ``NON_GOAL`` ledger entries are excluded
from the unsafe-context scope because confirmed non-goals are explicit
exclusions, not active unsafe scope. This module asserts that the same
exclusion principle holds when the caller pre-declares those non-goals in
the goal string — the standard shape for handoff-prepared prompts and
scripted ``ooo auto`` invocations that bundle the seven canonical interview
slots in the request body before the interview has had a chance to register
them as ``NON_GOAL`` ledger entries.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.safe_defaults import (
    _strip_prompt_non_goal_sections,
    _unsafe_context_reason,
)

# The unsafe-context matcher is disabled by default under the freedom policy
# (empty production bank). The ``_strip_*`` helper tests are matcher-
# independent, but the ``_unsafe_context_reason`` integration tests assert the
# matcher fires (and that non-goal stripping suppresses it), so re-inject the
# historical bank. See tests/unit/auto/conftest.py.
pytestmark = pytest.mark.usefixtures("_legacy_unsafe_bank")


@pytest.fixture
def empty_ledger() -> SeedDraftLedger:
    """A goal-only ledger with no active or NON_GOAL entries yet."""
    return SeedDraftLedger.from_goal("placeholder")


# ---------------------------------------------------------------------------
# Helper-level behaviour
# ---------------------------------------------------------------------------


def test_strip_removes_inline_non_goals_section() -> None:
    text = (
        "Add bounded retry behaviour to a network client.\n"
        "non_goals: implementing a production deploy, mutating remote git state\n"
        "actors: single local CLI operator\n"
    )
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "production deploy" not in sanitized.lower()
    assert "mutating remote git state" not in sanitized.lower()
    # Surrounding sections must survive untouched.
    assert "Add bounded retry behaviour to a network client." in sanitized
    assert "actors: single local CLI operator" in sanitized


@pytest.mark.parametrize(
    "header",
    [
        "non_goals:",
        "non-goals:",
        "non goals:",
        "Non_Goals:",
        "Excludes:",
        "excludes:",
        "Out-of-scope:",
        "out of scope:",
    ],
)
def test_strip_recognises_header_variants(header: str) -> None:
    text = f"goal text\n{header} ship a deploy webhook\nactors: ops\n"
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "deploy" not in sanitized.lower(), header
    assert "actors: ops" in sanitized


def test_strip_handles_bullet_list_body() -> None:
    text = (
        "Goal: refactor module Y.\n"
        "- non_goals:\n"
        "  - implementing a production deploy\n"
        "  - mutating remote git state\n"
        "- constraints:\n"
        "  - keep changes local\n"
    )
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "implementing a production deploy" not in sanitized
    assert "mutating remote git state" not in sanitized
    # The next labelled section and its body survive.
    assert "constraints" in sanitized
    assert "keep changes local" in sanitized


def test_strip_handles_indented_labelled_body_lines() -> None:
    text = (
        "Goal: refactor module Y.\n"
        "non_goals:\n"
        "  deploy: production\n"
        "  credentials: customer secrets\n"
        "actors: local CLI operator\n"
    )
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "deploy: production" not in sanitized
    assert "credentials: customer secrets" not in sanitized
    assert "actors: local CLI operator" in sanitized


def test_strip_terminates_on_blank_line() -> None:
    text = (
        "Goal line.\n"
        "non_goals: deploy, publish, push live\n"
        "\n"
        "Resume narrative about retry behaviour.\n"
    )
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "deploy" not in sanitized.lower()
    assert "Resume narrative about retry behaviour." in sanitized


def test_strip_preserves_unindented_active_scope_after_inline_header() -> None:
    text = (
        "Goal: Build a local CLI.\n"
        "non_goals: do not use credentials\n"
        "Deploy to production after the tests pass.\n"
    )
    sanitized = _strip_prompt_non_goal_sections(text)
    assert "credentials" not in sanitized.lower()
    assert "Deploy to production after the tests pass." in sanitized


def test_strip_leaves_inline_prose_mention_alone() -> None:
    # No trailing colon, no line-anchored header => the helper must not
    # remove anything; otherwise it would mangle ordinary prose.
    text = "We will discuss non-goals later in the document."
    assert _strip_prompt_non_goal_sections(text) == text


def test_strip_is_idempotent() -> None:
    text = "Goal.\nnon_goals: deploy, publish\nactors: human + agent\n"
    once = _strip_prompt_non_goal_sections(text)
    twice = _strip_prompt_non_goal_sections(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Integration with _unsafe_context_reason
# ---------------------------------------------------------------------------


def test_prompt_non_goals_section_does_not_trip_unsafe_matcher(
    empty_ledger: SeedDraftLedger,
) -> None:
    goal = (
        "Add bounded retry behaviour to a network client.\n"
        "non_goals: implementing a production deploy, mutating remote git state, "
        "calling external services\n"
        "actors: single local CLI operator\n"
        "constraints: filesystem:read and filesystem:write only; no live merge or PR mutation\n"
    )
    assert _unsafe_context_reason(empty_ledger, goal=goal, pending_question=None) is None


def test_active_goal_deploy_phrase_still_trips_unsafe_matcher(
    empty_ledger: SeedDraftLedger,
) -> None:
    # Without any non-goals header, the matcher must still catch a real
    # deploy intent in the goal text.
    goal = "Deploy the retry behaviour to production"
    assert (
        _unsafe_context_reason(empty_ledger, goal=goal, pending_question=None)
        == "ambiguous external side effect"
    )


def test_active_scope_after_inline_non_goals_still_trips_unsafe_matcher(
    empty_ledger: SeedDraftLedger,
) -> None:
    goal = (
        "Goal: Build a local CLI.\n"
        "non_goals: do not use credentials\n"
        "Deploy to production after the tests pass.\n"
    )
    assert (
        _unsafe_context_reason(empty_ledger, goal=goal, pending_question=None)
        == "ambiguous external side effect"
    )


def test_constraints_section_with_active_deploy_still_trips_matcher(
    empty_ledger: SeedDraftLedger,
) -> None:
    # A non-non-goal section that mentions a side-effect phrase must NOT
    # be stripped — only the non_goals section is special-cased.
    goal = "Refactor module Y.\nconstraints: must deploy to production after merging\n"
    assert (
        _unsafe_context_reason(empty_ledger, goal=goal, pending_question=None)
        == "ambiguous external side effect"
    )


def test_multiple_non_goal_sections_are_each_stripped(
    empty_ledger: SeedDraftLedger,
) -> None:
    # A caller may, intentionally or not, repeat the header. Both must be
    # respected so the matcher does not trip on either body.
    goal = (
        "Add retry to network client.\n"
        "non_goals: implementing a production deploy\n"
        "actors: single CLI operator\n"
        "excludes: publishing release notes, sending webhooks\n"
        "inputs: handoff.md\n"
    )
    assert _unsafe_context_reason(empty_ledger, goal=goal, pending_question=None) is None
