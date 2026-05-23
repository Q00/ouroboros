"""Tests for L1-d task-class default AC application."""

from __future__ import annotations

import pytest

from ouroboros.auto.task_class_application import (
    AppliedTaskClassDefaults,
    apply_default_ac_template,
)
from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed(ac: tuple[str, ...] = (), goal: str = "Build a CLI") -> Seed:
    return Seed(
        goal=goal,
        constraints=("Use existing project patterns",),
        acceptance_criteria=ac,
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_test_001", ambiguity_score=0.12),
    )


def test_returns_applied_defaults_dataclass() -> None:
    seed = _seed()
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    assert isinstance(applied, AppliedTaskClassDefaults)
    assert applied.task_class is TaskClass.CLI


def test_prepends_full_template_for_empty_user_ac() -> None:
    """User supplied no AC — the entire catalog template is prepended."""
    seed = _seed(ac=())
    profile = TASK_CLASS_CATALOG[TaskClass.CLI]
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    assert applied.injected_ac == profile.default_ac_template
    assert applied.seed.acceptance_criteria == profile.default_ac_template


def test_user_ac_appears_after_template_entries() -> None:
    """Template entries are prepended (not appended) so they read as
    preconditions, with user-supplied criteria following."""
    seed = _seed(ac=("Foo must do bar",))
    profile = TASK_CLASS_CATALOG[TaskClass.CLI]
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    expected = profile.default_ac_template + ("Foo must do bar",)
    assert applied.seed.acceptance_criteria == expected
    # injected_ac carries only the entries this call added.
    assert applied.injected_ac == profile.default_ac_template


def test_does_not_duplicate_when_user_already_supplied_one_template_entry() -> None:
    """If the user accidentally wrote one of the canonical template
    AC entries verbatim, the helper must not duplicate it."""
    profile = TASK_CLASS_CATALOG[TaskClass.CLI]
    one_template_entry = profile.default_ac_template[0]
    seed = _seed(ac=(one_template_entry, "User-specific AC"))
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    # The duplicate template entry is skipped; the rest is prepended.
    assert one_template_entry in applied.seed.acceptance_criteria
    assert applied.seed.acceptance_criteria.count(one_template_entry) == 1
    assert one_template_entry not in applied.injected_ac


def test_no_op_when_every_template_entry_already_present() -> None:
    """If the user happens to declare every template entry verbatim,
    the helper returns the *same* seed object — no allocation."""
    profile = TASK_CLASS_CATALOG[TaskClass.CLI]
    seed = _seed(ac=profile.default_ac_template)
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    assert applied.injected_ac == ()
    assert applied.seed is seed


def test_each_task_class_has_a_nonempty_template() -> None:
    """Every L1-a TaskClass declares at least one default AC entry,
    so applying it always changes the seed (assuming user AC is empty
    of course). Guards the catalog against accidentally shipping an
    empty AC list for some class."""
    seed = _seed(ac=())
    for task_class in TaskClass:
        applied = apply_default_ac_template(seed, task_class)
        assert applied.injected_ac, (
            f"TaskClass {task_class.value} has empty default_ac_template — "
            f"adding it would be a no-op forever"
        )


def test_seed_acceptance_criteria_type_preserved() -> None:
    """``Seed.acceptance_criteria`` is ``tuple[str, ...]``; after
    application, the type stays a tuple (pydantic ``frozen=True`` seed
    can return a different container shape via ``model_copy`` if we
    are not careful)."""
    seed = _seed(ac=("user-ac",))
    applied = apply_default_ac_template(seed, TaskClass.CLI)
    assert isinstance(applied.seed.acceptance_criteria, tuple)
    assert all(isinstance(item, str) for item in applied.seed.acceptance_criteria)


def test_seed_is_frozen_after_application() -> None:
    """The returned seed is still ``frozen=True`` — re-applying the
    helper twice does not crash."""
    seed = _seed(ac=())
    once = apply_default_ac_template(seed, TaskClass.CLI)
    twice = apply_default_ac_template(once.seed, TaskClass.CLI)
    # Second application is a no-op since the template is already there.
    assert twice.injected_ac == ()
    assert twice.seed is once.seed


def test_invalid_task_class_raises() -> None:
    """A TaskClass enum value not in the catalog (synthetic test
    construction) raises KeyError. Production code cannot hit this
    because the L1 enum and catalog are kept in sync by
    test_task_classes_match_catalog."""
    # We have to manufacture an unregistered enum-like value to exercise
    # the KeyError path; the production registry guard rejects None
    # already.
    with pytest.raises(KeyError):
        apply_default_ac_template(_seed(), "not_a_task_class")  # type: ignore[arg-type]
