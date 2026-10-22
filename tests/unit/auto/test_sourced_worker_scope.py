"""An auto Seed's worker prompt renders only content with a known source.

The Seed generator writes the goal, constraints, criteria, assumptions,
ontology, principles and exit conditions from the whole interview, unanswered
questions included. ``with_ledger_carryover`` records what has a source
(``Seed.sourced_scope``: the user's goal verbatim and the entries that bind),
decided by ledger provenance, and the worker prompt renders only that record,
the requester answers and the repo facts. The model-written fields stay in the
Seed. A Seed without the record (a normal interview, an older Seed) renders as
before.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from ouroboros.auto.ledger import (
    REPO_FACT_KEY_INFIX,
    REQUESTER_KEY_SUFFIX,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import ledger_sourced_scope, with_ledger_carryover
from ouroboros.core.seed import Seed, SourcedScope
from ouroboros.core.seed_contract import GOAL_OBJECTIVE_CRITERION, SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution
from ouroboros.orchestrator.atomic_prompt_builder import (
    SOURCED_SCOPE_ATTR,
    AtomicPromptBuilder,
    install_sourced_scope,
)
from ouroboros.orchestrator.evidence.formatting import _build_governed_parent_summary
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.parallel_executor_models import inherit_check_package_hooks
from ouroboros.orchestrator.runner import build_system_prompt, build_task_prompt

USER_GOAL = "Session data cannot be decoded while instances run mixed versions."
MODEL_GOAL = "There is a problem with decoding session data due to a format change."
MODEL_CONSTRAINT = "Target imports of the current directory's package marker."
MODEL_ASSUMPTION = "Whether updated instances must read each other's sessions is unspecified."
GENERIC_AC = (
    "A command/API check returns stable observable output or artifacts proving "
    "the original requirement for the decoding fix."
)
ONTOLOGY_NAME = "Session Format Compatibility"
PRINCIPLE = "Resolve the stated decoding issue"
EXIT_CONDITION = "Multi-instance operation supported"
OPEN_QUESTION = "Should interoperability be enabled by default or opt-in?"
REQUESTER_QUESTION = "Which release should carry the fix?"
REQUESTER_ANSWER = "The next patch release."
REPO_QUESTION = "Where is session data encoded?"
REPO_ANSWER = "SessionBase.encode signs the session payload."
DEFAULTED_CONSTRAINT = "Do not add new dependencies."


def _entry(key: str, value: str, **kwargs: Any) -> LedgerEntry:
    fields: dict[str, Any] = {
        "source": LedgerSource.ASSUMPTION,
        "confidence": 0.5,
        "status": LedgerStatus.DEFAULTED,
    }
    fields.update(kwargs)
    return LedgerEntry(key=key, value=value, **fields)


def _ledger(goal: str = USER_GOAL) -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(goal)
    # An unanswered question: auto's own draft, which does not bind.
    ledger.add_entry(
        "constraints",
        _entry("constraints.interop", "Interoperability is opt-in", question=OPEN_QUESTION),
    )
    ledger.add_entry("constraints", _entry("constraints.deps", DEFAULTED_CONSTRAINT))
    ledger.add_entry(
        "constraints",
        _entry(
            f"constraints.release{REQUESTER_KEY_SUFFIX}",
            REQUESTER_ANSWER,
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.83,
            status=LedgerStatus.CONFIRMED,
            question=REQUESTER_QUESTION,
        ),
    )
    ledger.add_entry(
        "runtime_context",
        _entry(
            f"runtime_context.encode{REPO_FACT_KEY_INFIX}1",
            REPO_ANSWER,
            source=LedgerSource.REPO_FACT,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            question=REPO_QUESTION,
            evidence=["sessions/base.py:10-12: def encode"],
        ),
    )
    return ledger


def _generated_seed() -> Seed:
    """A Seed shaped like the generator's output in an auto run without a requester."""
    return Seed.from_dict(
        {
            "goal": MODEL_GOAL,
            "brownfield_context": {
                "project_type": "brownfield",
                "context_references": [
                    {"path": "/testbed", "role": "primary", "summary": "Model summary."}
                ],
            },
            "constraints": [MODEL_CONSTRAINT],
            "acceptance_criteria": [
                {
                    "description": GENERIC_AC,
                    "verify_exemption_reason": "No runnable entry point was named.",
                }
            ],
            "assumptions": [MODEL_ASSUMPTION],
            "ontology_schema": {
                "name": ONTOLOGY_NAME,
                "description": "d",
                "fields": [{"name": "decoding_outcome", "type": "boolean", "description": "d"}],
            },
            "evaluation_principles": [{"name": PRINCIPLE, "description": "d", "weight": 1.0}],
            "exit_conditions": [
                {"name": EXIT_CONDITION, "description": "d", "criteria": "c"},
            ],
            "metadata": {"ambiguity_score": 0.1},
        }
    )


class _Executor:
    """The executor surface ``AtomicPromptBuilder`` reads.

    ``governed=False`` returns the legacy task section, so the builder appends
    the previous-level context itself; ``governed=True`` renders that context
    inside the task section, as the context governor does.
    """

    _execution_profile = None
    _fat_harness_mode = False

    def __init__(self, cwd: str, *, governed: bool = False) -> None:
        self._task_cwd = cwd
        self._adapter = SimpleNamespace(working_directory=cwd)
        self._governed = governed

    def _build_atomic_dispatch_context(self, **kwargs: Any) -> tuple[str, dict[str, Any] | None]:
        task = f"## Your Task ({kwargs['label']})\n{kwargs['ac_content']}"
        if not self._governed:
            return task, None
        parent = _build_governed_parent_summary(kwargs["level_contexts"])
        return f"{task}\n## Parent context\n{parent}", {"context_governed": True}


def _leaf_prompt(
    seed: Seed,
    cwd: str,
    *,
    level_contexts: list[LevelContext] | None = None,
    governed: bool = False,
) -> str:
    executor = _Executor(cwd, governed=governed)
    install_sourced_scope(executor, seed.sourced_scope)
    return (
        AtomicPromptBuilder(executor)  # type: ignore[arg-type]
        .build(
            ac_index=0,
            ac_content=seed.acceptance_criteria[0].description,
            seed_goal=seed.goal,
            is_sub_ac=False,
            parent_ac_index=None,
            sub_ac_index=None,
            node_identity=None,
            level_contexts=level_contexts,
            sibling_acs=[(0, "this criterion"), (1, "a sibling criterion the model wrote")],
            retry_attempt=0,
            retry_prompt_extra="",
            ac_spec=None,
        )
        .prompt
    )


def _worker_prompts(seed: Seed, cwd: str) -> tuple[str, ...]:
    return (
        build_system_prompt(seed, resolved_context_pack_fragment=""),
        build_task_prompt(seed),
        _leaf_prompt(seed, cwd),
    )


def test_auto_seed_worker_prompts_render_only_sourced_content(tmp_path) -> None:
    seed = with_ledger_carryover(_generated_seed(), _ledger())

    assert seed.sourced_scope == SourcedScope(goal=USER_GOAL)
    system_prompt, task_prompt, leaf_prompt = _worker_prompts(seed, str(tmp_path))
    for prompt in (system_prompt, task_prompt, leaf_prompt):
        assert USER_GOAL in prompt
        for model_text in (
            MODEL_GOAL,
            MODEL_CONSTRAINT,
            MODEL_ASSUMPTION,
            GENERIC_AC,
            ONTOLOGY_NAME,
            PRINCIPLE,
            EXIT_CONDITION,
            OPEN_QUESTION,
            DEFAULTED_CONSTRAINT,
            "Model summary.",
            "a sibling criterion the model wrote",
        ):
            assert model_text not in prompt
    # With no confirmed criterion the worker's objective is the request itself.
    assert GOAL_OBJECTIVE_CRITERION in task_prompt
    assert GOAL_OBJECTIVE_CRITERION in leaf_prompt
    # Requester answers and checked repo facts still reach the worker.
    for text in (REQUESTER_QUESTION, REQUESTER_ANSWER, REPO_QUESTION, REPO_ANSWER):
        assert text in system_prompt


def test_model_written_fields_stay_in_the_seed(tmp_path) -> None:
    generated = _generated_seed()
    seed = with_ledger_carryover(generated, _ledger())

    assert seed.goal == MODEL_GOAL
    assert seed.constraints == generated.constraints
    assert seed.acceptance_criteria == generated.acceptance_criteria
    assert MODEL_ASSUMPTION in seed.assumptions
    assert seed.ontology_schema == generated.ontology_schema
    assert seed.evaluation_principles == generated.evaluation_principles
    assert seed.exit_conditions == generated.exit_conditions
    assert OPEN_QUESTION in seed.open_questions


def test_confirmed_constraints_and_criteria_reach_the_worker(tmp_path) -> None:
    goal = (
        "Add a json flag to the export command. Constraints are keep the CSV output "
        "unchanged. Acceptance criteria are export --json prints one JSON object per row."
    )
    seed = with_ledger_carryover(_generated_seed(), _ledger(goal))

    assert seed.sourced_scope is not None
    assert seed.sourced_scope.goal == goal
    assert seed.sourced_scope.constraints == ("keep the CSV output unchanged",)
    assert seed.sourced_scope.acceptance_criteria == (
        "export --json prints one JSON object per row",
    )
    system_prompt, task_prompt, leaf_prompt = _worker_prompts(seed, str(tmp_path))
    assert "- keep the CSV output unchanged" in system_prompt
    assert DEFAULTED_CONSTRAINT not in system_prompt
    for prompt in (task_prompt, leaf_prompt):
        assert "export --json prints one JSON object per row" in prompt
        assert GOAL_OBJECTIVE_CRITERION not in prompt
        assert GENERIC_AC not in prompt


def test_seed_without_sourced_scope_renders_as_before(tmp_path) -> None:
    seed = _generated_seed()
    assert seed.sourced_scope is None

    system_prompt, task_prompt, leaf_prompt = _worker_prompts(seed, str(tmp_path))
    for text in (MODEL_GOAL, MODEL_CONSTRAINT, MODEL_ASSUMPTION, ONTOLOGY_NAME, PRINCIPLE):
        assert text in system_prompt
    assert EXIT_CONDITION in system_prompt
    assert "## Existing Codebase Context (BROWNFIELD)" in system_prompt
    for prompt in (task_prompt, leaf_prompt):
        assert GENERIC_AC in prompt
        assert MODEL_GOAL in prompt
    assert "a sibling criterion the model wrote" in leaf_prompt
    contract = SeedContract.from_seed(seed)
    assert contract.sourced_only is False
    assert render_seed_contract_for_execution(contract) in system_prompt


def test_sourced_scope_round_trips_and_older_seeds_load() -> None:
    seed = with_ledger_carryover(_generated_seed(), _ledger())

    data = seed.to_dict()
    assert data["sourced_scope"] == {
        "goal": USER_GOAL,
        "constraints": [],
        "acceptance_criteria": [],
    }
    restored = Seed.from_dict(yaml.safe_load(yaml.safe_dump(data)))
    assert restored.sourced_scope == seed.sourced_scope

    older = dict(data)
    older.pop("sourced_scope")
    loaded = Seed.from_dict(older)
    assert loaded.sourced_scope is None
    assert "sourced_scope" not in loaded.to_dict()


def test_a_rewritten_criterion_still_renders_the_objective(tmp_path) -> None:
    """A repair pass rewrites criteria through to_dict/from_dict; the record survives."""
    seed = with_ledger_carryover(_generated_seed(), _ledger())
    data = seed.to_dict()
    data["acceptance_criteria"] = ["A rewritten criterion the repairer wrote."]
    repaired = Seed.from_dict(data)

    assert repaired.sourced_scope == seed.sourced_scope
    _system, task_prompt, leaf_prompt = _worker_prompts(repaired, str(tmp_path))
    for prompt in (task_prompt, leaf_prompt):
        assert "A rewritten criterion the repairer wrote." not in prompt
        assert GOAL_OBJECTIVE_CRITERION in prompt


def test_scope_is_decided_by_provenance_not_text() -> None:
    ledger = _ledger()
    # Same wording as a confirmed constraint, but auto drafted it: it does not bind.
    ledger.add_entry(
        "constraints", _entry("constraints.lookalike", "keep the CSV output unchanged")
    )
    scope = ledger_sourced_scope(ledger)

    assert scope is not None
    assert scope.constraints == ()
    # No active user goal entry: no record, so the Seed renders as before.
    assert ledger_sourced_scope(SeedDraftLedger.from_goal("   ")) is None


def test_derived_executor_inherits_the_sourced_scope() -> None:
    scope = SourcedScope(goal=USER_GOAL)
    parent = SimpleNamespace()
    install_sourced_scope(parent, scope)
    child = SimpleNamespace()

    inherit_check_package_hooks(parent, child)

    assert getattr(child, SOURCED_SCOPE_ATTR) == scope


@pytest.mark.parametrize("governed", [False, True])
def test_previous_level_context_shows_the_objective_not_the_seed_criterion(
    tmp_path, governed: bool
) -> None:
    """A later leaf's previous-work context keeps the work summary, not the criterion text."""
    previous = [
        LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Earlier criterion the model wrote",
                    success=True,
                    files_modified=("sessions/base.py",),
                    key_output="Changed the encoder.",
                ),
            ),
        )
    ]
    sourced = with_ledger_carryover(_generated_seed(), _ledger())
    prompt = _leaf_prompt(sourced, str(tmp_path), level_contexts=previous, governed=governed)

    assert "Earlier criterion the model wrote" not in prompt
    assert "sessions/base.py" in prompt
    assert "Changed the encoder." in prompt
    assert f"AC 1: {GOAL_OBJECTIVE_CRITERION}" in prompt

    legacy = _leaf_prompt(
        _generated_seed(), str(tmp_path), level_contexts=previous, governed=governed
    )
    assert "Earlier criterion the model wrote" in legacy
