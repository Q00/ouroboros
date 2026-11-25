"""Auto builds what the request asks for at the expected level and never narrows it.

A kart-racing request scored 5/14 on a blind checklist under ``ooo auto`` and
12/14 as a one-shot, because auto's own defaults told the worker to keep a
"conservative MVP" and to treat unplanned additions as failure. These tests pin
the replacement texts: the answerer's default and its ledger entries, the safe
defaults, the answer refiner's scope rule (new product versus existing code)
and its starting-point label, and the fallback answers in every language.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.auto.adapters import (
    REFINER_BROWNFIELD_RULE,
    REFINER_GREENFIELD_RULE,
    LLMAnswerRefiner,
    build_answer_refiner,
)
from ouroboros.auto.answer_text import FALLBACK_ANSWERS, LANGUAGE_NAMES, answer_problems
from ouroboros.auto.answerer import (
    EXPECTED_LEVEL_POLICY,
    UNREQUESTED_CHANGE_FAILURE,
    AutoAnswerer,
    AutoAnswerSource,
)
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.ledger_seed import workspace_has_existing_code
from ouroboros.auto.safe_defaults import (
    _SAFE_DEFAULTS,
    SafeDefaultFinalization,
    build_safe_default_synthesis,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, Message, MessageRole, UsageInfo

# Words of the replaced texts that told the worker to build less.
_NARROWING = ("mvp", "smallest", "scope expansion", "conservative")


def _assert_not_narrowing(text: str) -> None:
    lowered = text.lower()
    for word in _NARROWING:
        assert word not in lowered, f"{word!r} in {text!r}"


# -- the answerer's default --------------------------------------------------------


def test_the_default_answer_is_the_expected_level_policy() -> None:
    answer = AutoAnswerer().answer(
        "¿Cuál es el color favorito del tablero?", SeedDraftLedger.from_goal("Build a dashboard")
    )

    assert answer.source == AutoAnswerSource.CONSERVATIVE_DEFAULT
    assert answer.text == EXPECTED_LEVEL_POLICY
    updates = dict(answer.ledger_updates)
    assert updates["constraints"].key == "constraints.expected_level"
    assert updates["constraints"].value == EXPECTED_LEVEL_POLICY
    assert updates["failure_modes"].key == "failure_modes.unverified_or_unrequested_change"
    assert updates["failure_modes"].value == UNREQUESTED_CHANGE_FAILURE
    for text in (answer.text, *(entry.value for entry in updates.values())):
        _assert_not_narrowing(text)
    for entry in updates.values():
        _assert_not_narrowing(entry.rationale)


def test_the_policy_says_what_it_keeps_and_what_it_never_limits() -> None:
    assert "level a user expects from a typical current product of its kind" in (
        EXPECTED_LEVEL_POLICY
    )
    assert "no limit the request does not state" in EXPECTED_LEVEL_POLICY
    assert "existing behavior the request does not mention unchanged" in EXPECTED_LEVEL_POLICY
    assert "follow the project's patterns" in EXPECTED_LEVEL_POLICY
    assert "verifiable" in EXPECTED_LEVEL_POLICY
    assert "changes to existing behavior the request does not ask for" in (
        UNREQUESTED_CHANGE_FAILURE
    )


def test_no_failure_text_asks_the_product_to_behave_the_same_on_every_run() -> None:
    # "non-reproducible output" made the interview ask for identical replays, and
    # the worker replaced random item boxes with a fixed cycle.
    for text in (UNREQUESTED_CHANGE_FAILURE, _SAFE_DEFAULTS["failure_modes"].value):
        assert "reproduc" not in text.lower()
        assert "repeat" not in text.lower()


@pytest.mark.parametrize("section", ["constraints", "failure_modes"])
def test_a_constraint_or_failure_gap_gets_the_same_policy(section: str) -> None:
    answer = AutoAnswerer().answer_gap(section, SeedDraftLedger.from_goal("Build a kart game"))

    assert answer.text == EXPECTED_LEVEL_POLICY
    keys = [entry.key for _section, entry in answer.ledger_updates]
    assert keys == ["constraints.expected_level", "failure_modes.unverified_or_unrequested_change"]


# -- the safe defaults -------------------------------------------------------------


def test_the_safe_defaults_no_longer_narrow_outputs_constraints_or_failures() -> None:
    outputs = _SAFE_DEFAULTS["outputs"]
    constraints = _SAFE_DEFAULTS["constraints"]
    failures = _SAFE_DEFAULTS["failure_modes"]

    assert "the goal asks for, at the level a user expects" in outputs.value
    assert "preserve existing project patterns and the behavior" in constraints.value
    assert "add a dependency only when the goal needs it" in constraints.value
    assert "changes the goal does not ask for to existing" in failures.value
    for spec in (outputs, constraints, failures):
        _assert_not_narrowing(spec.value)
        _assert_not_narrowing(spec.rationale)


def test_the_safe_default_synthesis_header_is_neutral() -> None:
    text = build_safe_default_synthesis(
        SafeDefaultFinalization(("outputs",), (), (("outputs", _SAFE_DEFAULTS["outputs"]),))
    )

    header = text.splitlines()[0]
    assert "The following safe default assumptions close the remaining required" in header
    _assert_not_narrowing(text)


def test_a_product_behavior_answer_is_in_scope_without_an_mvp() -> None:
    answer = AutoAnswerer().answer(
        "Should users be able to export their reports as PDF?",
        SeedDraftLedger.from_goal("Build a report app"),
    )

    assert answer.text.startswith("Treat this requested product behavior as in scope: ")
    _assert_not_narrowing(answer.text)


def test_the_safety_non_goals_are_unchanged() -> None:
    assert _SAFE_DEFAULTS["non_goals"].value == (
        "Do not perform credential handling, billing, production deployment, legal or medical "
        "judgment, security-sensitive authority choices, or ambiguous external side effects."
    )


# -- the answer refiner ------------------------------------------------------------


class _CapturingAdapter:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def complete(self, messages, config):  # noqa: ARG002
        self.messages = list(messages)
        return Result.ok(
            CompletionResponse(
                content="AI karts race as the main mode, with solo time trials too.",
                model="fake",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def _text(adapter: _CapturingAdapter, role: MessageRole) -> str:
    return "\n".join(message.content for message in adapter.messages if message.role == role)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "rule", "other"),
    [
        ({"has_existing_code": False}, REFINER_GREENFIELD_RULE, REFINER_BROWNFIELD_RULE),
        ({"has_existing_code": True}, REFINER_BROWNFIELD_RULE, REFINER_GREENFIELD_RULE),
        ({}, REFINER_BROWNFIELD_RULE, REFINER_GREENFIELD_RULE),
    ],
)
async def test_the_refiner_uses_the_scope_rule_for_the_workspace(
    kwargs: dict[str, bool], rule: str, other: str
) -> None:
    adapter = _CapturingAdapter()
    refiner = LLMAnswerRefiner(adapter, **kwargs)  # type: ignore[arg-type]

    await refiner(
        "Build a kart racing game",
        "Solo time trial or races against AI karts?",
        "constraints",
        EXPECTED_LEVEL_POLICY,
    )

    system = _text(adapter, MessageRole.SYSTEM)
    assert rule in system
    assert other not in system
    user = _text(adapter, MessageRole.USER)
    assert f"A starting point, to rewrite in your own words: {EXPECTED_LEVEL_POLICY}" in user
    assert "conservative" not in user.lower()
    assert "conservative" not in system.lower()


def test_the_two_rules_say_what_the_probes_measured() -> None:
    assert "never narrow it" in REFINER_GREENFIELD_RULE
    assert "include what such products have from every option" in REFINER_GREENFIELD_RULE
    assert "server, network service, account, payment or personal data" in (REFINER_GREENFIELD_RULE)
    assert "(sharing or syncing between people or devices does)" in REFINER_GREENFIELD_RULE
    assert "Name counts and contents concretely, as many as such products have" in (
        REFINER_GREENFIELD_RULE
    )
    assert "even where the question names a smaller one: never one track" in (
        REFINER_GREENFIELD_RULE
    )
    assert 'never only "no limit", "every" or "the full variety"' in REFINER_GREENFIELD_RULE
    assert "from every part of the answer, including what the result shows" in (
        REFINER_GREENFIELD_RULE
    )
    assert "an earlier answer included it, or the question asks to bring it back" in (
        REFINER_GREENFIELD_RULE
    )
    assert "Never answer that less is enough or call anything a first version or release" in (
        REFINER_GREENFIELD_RULE
    )
    assert "keep the variety such products have, such as randomness and animation" in (
        REFINER_GREENFIELD_RULE
    )
    assert "variety" not in REFINER_BROWNFIELD_RULE
    assert "add no other feature, option or behavior the request does not name" in (
        REFINER_BROWNFIELD_RULE
    )
    for rule in (REFINER_GREENFIELD_RULE, REFINER_BROWNFIELD_RULE):
        assert rule.startswith(
            "You have not read the repository: never state what the existing code does or contains."
        )
        assert "the request does not mention" in rule


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "main.py").write_text("print('existing')\n", encoding="utf-8")
    return root


def test_the_workspace_decides_which_rule_the_built_refiner_uses(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    repo = _git_repo(tmp_path / "repo")

    with patch("ouroboros.providers.create_llm_adapter", return_value=_CapturingAdapter()):
        greenfield = build_answer_refiner(empty)
        brownfield = build_answer_refiner(repo)
        unknown = build_answer_refiner(None)
        missing = build_answer_refiner(tmp_path / "missing")

    assert greenfield is not None and greenfield.has_existing_code is False
    for refiner in (brownfield, unknown, missing):
        assert refiner is not None and refiner.has_existing_code is True


def test_existing_code_is_the_answer_whenever_the_workspace_is_unknown(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    (configured / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    assert workspace_has_existing_code(empty) is False
    assert workspace_has_existing_code(str(empty)) is False
    assert workspace_has_existing_code(_git_repo(tmp_path / "repo")) is True
    assert workspace_has_existing_code(configured) is True
    assert workspace_has_existing_code(None) is True
    assert workspace_has_existing_code("") is True
    assert workspace_has_existing_code(tmp_path / "missing") is True
    assert workspace_has_existing_code(a_file) is True


# -- the fallback answers ----------------------------------------------------------


@pytest.mark.parametrize("language", sorted(LANGUAGE_NAMES))
def test_every_fallback_answer_passes_the_answer_rules(language: str) -> None:
    assert answer_problems(FALLBACK_ANSWERS[language], language) == []


def test_the_english_fallback_states_the_policy_in_one_sentence() -> None:
    text = FALLBACK_ANSWERS["en"]

    assert text == (
        "Do what a typical current product of this kind does, keep existing behavior "
        "the request does not mention, and follow the project's conventions."
    )
    _assert_not_narrowing(text)
    assert set(FALLBACK_ANSWERS) == set(LANGUAGE_NAMES)
