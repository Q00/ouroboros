"""Catalog-shape tests for the L1-a frozen task-class catalog.

These tests pin the *shape* of the catalog: every :class:`TaskClass`
value has exactly one :class:`TaskClassProfile`, each profile's
``default_ac_template`` is a non-empty tuple of plain strings, and
each ``default_completion_mode`` resolves to the documented enum.

They do **not** exercise domain inference (that's L1-b),
interview-driver disambiguation (that's L1-c), or AC injection
(that's L1-d). Adding a new task class without a matching unit test
below will fail ``test_task_classes_match_catalog``.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.task_classes import (
    TASK_CLASS_CATALOG,
    CompletionMode,
    TaskClass,
    TaskClassProfile,
    get_task_class_profile,
)


def test_task_classes_match_catalog() -> None:
    """Every :class:`TaskClass` enum value has a catalog entry, and the
    catalog has no orphan entries. Adding a class without a profile
    (or vice versa) fails here."""
    assert set(TASK_CLASS_CATALOG.keys()) == set(TaskClass)


@pytest.mark.parametrize("task_class", list(TaskClass))
def test_catalog_entry_shape(task_class: TaskClass) -> None:
    """Each catalog entry has the required shape: non-empty AC template,
    declared completion mode, declared probe kinds, and ``name`` matching
    the enum value (string-equality, not identity)."""
    profile = get_task_class_profile(task_class)

    assert isinstance(profile, TaskClassProfile)
    assert profile.name == task_class.value
    assert isinstance(profile.default_completion_mode, CompletionMode)
    # AC template must be a non-empty tuple of plain strings — matches
    # ``Seed.acceptance_criteria``'s shape exactly so L1-d can prepend
    # entries without any type coercion.
    assert isinstance(profile.default_ac_template, tuple)
    assert profile.default_ac_template, f"{task_class.value} has empty default_ac_template"
    assert all(isinstance(item, str) for item in profile.default_ac_template)
    assert all(item.strip() for item in profile.default_ac_template), (
        f"{task_class.value} default_ac_template has empty/whitespace entries"
    )
    # Probe kinds is a tuple of plain strings (placeholder until L3 lands
    # a typed enum). Empty is allowed for forward-compat.
    assert isinstance(profile.runtime_probe_kinds, tuple)
    assert all(isinstance(item, str) for item in profile.runtime_probe_kinds)


def test_library_class_is_code_complete() -> None:
    """``library`` is the only class with ``CODE_COMPLETE`` plus
    ``refactor_in_place`` — every other class is ``PRODUCT_COMPLETE``."""
    code_complete = {
        tc
        for tc in TaskClass
        if get_task_class_profile(tc).default_completion_mode == CompletionMode.CODE_COMPLETE
    }
    assert code_complete == {TaskClass.LIBRARY, TaskClass.REFACTOR_IN_PLACE}


def test_catalog_is_immutable_view() -> None:
    """``TASK_CLASS_CATALOG`` is a ``MappingProxyType`` view that callers
    cannot mutate directly. Pin so a future refactor does not regress
    immutability."""
    with pytest.raises(TypeError):
        TASK_CLASS_CATALOG[TaskClass.LIBRARY] = TaskClassProfile(  # type: ignore[index]
            name="bogus",
            default_completion_mode=CompletionMode.CODE_COMPLETE,
            default_ac_template=("never",),
            runtime_probe_kinds=(),
        )


def test_completion_modes_are_string_enum() -> None:
    """``CompletionMode`` is a ``StrEnum`` so values serialize as plain
    strings into ``AutoPipelineResult`` envelope fields without a custom
    encoder."""
    assert CompletionMode.CODE_COMPLETE == "code_complete"
    assert CompletionMode.PRODUCT_COMPLETE == "product_complete"


def test_task_class_is_string_enum() -> None:
    """``TaskClass`` is a ``StrEnum`` so values can be used as catalog
    keys, JSON values, and ledger entries without coercion."""
    assert TaskClass.CLI == "cli"
    assert TaskClass.WEB_SERVICE == "web_service"
    assert TaskClass.REFACTOR_IN_PLACE == "refactor_in_place"


def test_webhook_and_web_service_are_distinct_in_catalog() -> None:
    """L1 design decision (#1171) keeps ``webhook`` and ``web_service``
    separate because their probe bindings differ (side-effect vs
    request-shape). Pin so a future merge requires explicit re-decision."""
    webhook = get_task_class_profile(TaskClass.WEBHOOK)
    web_service = get_task_class_profile(TaskClass.WEB_SERVICE)
    assert webhook.runtime_probe_kinds != web_service.runtime_probe_kinds
    assert "side_effect_probe" in webhook.runtime_probe_kinds
    assert "api_smoke" in web_service.runtime_probe_kinds
