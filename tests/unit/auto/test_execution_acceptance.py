from __future__ import annotations

from ouroboros.auto.execution_acceptance import (
    is_auto_reporting_acceptance_criterion,
    normalize_execution_acceptance,
)

_SINGLE_HELLO_AUTO_OBSERVATION_AC = (
    "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
    "`hello_auto() -> str` returns exactly `hello from ooo auto`, "
    "the test imports `hello_auto` and asserts that exact value, and "
    "the exact command `uv run pytest tests/test_hello_auto.py` passes."
)

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed(*criteria: str) -> Seed:
    return Seed(
        goal="Verify ooo auto with a minimal coding task",
        constraints=("Only edit hello_auto.py and tests/test_hello_auto.py",),
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(
            name="HelloAuto",
            description="Minimal coding task",
            fields=(OntologyField(name="file", field_type="string", description="File"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Runnable tests pass"),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Targeted test passes",
                evaluation_criteria="All execution criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_test", ambiguity_score=0.1),
    )


def test_normalize_execution_acceptance_drops_auto_report_criteria() -> None:
    seed = _seed(
        "`ooo auto` is dispatched to the MCP tool `ouroboros_auto`.",
        "Manual fallback is not used.",
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        "Final report includes auto session id, seed id, seed path, and test result.",
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_drops_observation_report_metadata() -> None:
    seed = _seed(
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        "Manual fallback used: no.",
        "Previous last_question blocker did not recur.",
        "Previous Seed grade C blocker did not recur.",
        "Previous interview closure blocker did not recur.",
        "Recursive auto invocation occurred: no.",
    ).model_copy(
        update={
            "goal": "Verify current ooo auto can create hello_auto.py and tests/test_hello_auto.py using ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_filters_latest_observation_prompt_metadata() -> None:
    seed = _seed(
        "`ooo auto` is dispatched through the installed Ouroboros MCP tool, not interpreted as plain text.",
        "Seed reaches grade A.",
        "Execution is handed off to the background execution job.",
        "`hello_auto.py` exists.",
        "`tests/test_hello_auto.py` exists.",
        "`uv run pytest tests/test_hello_auto.py` passes.",
        "The execution job reaches a terminal status without manual cancellation.",
        "Whether progress accounting stalled at AC 0/N is reported.",
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto. hello_auto returns exactly hello from ooo auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_preserves_non_equivalent_file_criteria() -> None:
    seed = _seed(
        "`ooo auto` is dispatched through the installed Ouroboros MCP tool, not interpreted as plain text.",
        "`hello_auto.py` contains a module-level docstring.",
        "`tests/test_hello_auto.py` uses pytest.mark.smoke.",
        "pytest tests/test_hello_auto.py -q passes.",
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto. hello_auto returns exactly hello from ooo auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        "`hello_auto.py` contains a module-level docstring.",
        "`tests/test_hello_auto.py` uses pytest.mark.smoke.",
        "pytest tests/test_hello_auto.py -q passes.",
    )


def test_normalize_execution_acceptance_preserves_extra_hello_auto_requirements() -> None:
    seed = _seed(
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        "`hello_auto.py` contains a module-level docstring.",
        "`tests/test_hello_auto.py` uses pytest.mark.smoke.",
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        _SINGLE_HELLO_AUTO_OBSERVATION_AC,
        "`hello_auto.py` contains a module-level docstring.",
        "`tests/test_hello_auto.py` uses pytest.mark.smoke.",
    )


def test_normalize_execution_acceptance_preserves_real_product_lifecycle_criteria() -> None:
    seed = _seed(
        "`ooo auto` is dispatched through the installed Ouroboros MCP tool, not interpreted as plain text.",
        "Implement a manual fallback mode for unavailable tools.",
        "Persist execution job status for resumed runs.",
        "Display progress accounting for every acceptance criterion.",
        "`hello_auto.py` exists.",
        "`tests/test_hello_auto.py` exists.",
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto. hello_auto returns exactly hello from ooo auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        "Implement a manual fallback mode for unavailable tools.",
        "Persist execution job status for resumed runs.",
        "Display progress accounting for every acceptance criterion.",
        "`hello_auto.py` exists.",
        "`tests/test_hello_auto.py` exists.",
    )


def test_reporting_classifier_keeps_broad_observation_markers_context_scoped() -> None:
    assert is_auto_reporting_acceptance_criterion("Manual fallback is not used.")
    assert not is_auto_reporting_acceptance_criterion(
        "The execution job reaches a terminal status without manual cancellation."
    )
    assert not is_auto_reporting_acceptance_criterion(
        "Whether progress accounting stalled at AC 0/N is reported."
    )


def test_normalize_execution_acceptance_unwraps_repaired_observation_criteria() -> None:
    seed = _seed(
        "A command/API check returns stable observable output or artifacts proving the original requirement for `hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for tests/test_hello_auto.py imports hello_auto and asserts exact return value.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Final observation report plain chat summary including requested unavailable MCP/auto metadata as not available/not run in this surface when applicable.",
    ).model_copy(
        update={
            "goal": "Observation run for ooo auto via ouroboros_auto: create hello_auto.py and tests/test_hello_auto.py; hello_auto returns exactly hello from ooo auto; validate with uv run pytest tests/test_hello_auto.py."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_drops_repaired_fragments_with_exact_command() -> None:
    exact_contract = (
        "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
        "`hello_auto() -> str` returns exactly `hello from ooo auto`, "
        "the test imports `hello_auto` and asserts that exact value, and "
        "the exact command `uv run pytest tests/test_hello_auto.py` passes."
    )
    seed = _seed(
        exact_contract,
        "A command/API check returns stable observable output or artifacts proving the original requirement for hello_auto.py exists at repository root.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for hello_auto() function exists.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Test imports exactly from hello_auto import hello_auto.",
    ).model_copy(update={"goal": "Create a tiny Python module and exact pytest verification."})

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (exact_contract,)


def test_normalize_execution_acceptance_relaxes_conflicting_minimality_constraints() -> None:
    seed = _seed(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
    ).model_copy(
        update={
            "constraints": (
                "No extra files or behavior beyond the exact return-value test",
                "Avoid new dependencies",
                "Do not add new dependencies",
                "Verification command must be uv run pytest tests/test_hello_auto.py",
            ),
            "exit_conditions": (
                ExitCondition(
                    name="scope_preserved",
                    description="No unauthorized expansion occurred",
                    evaluation_criteria=(
                        "no new dependencies, frameworks, deployment targets, credentials, "
                        "or external side effects are introduced"
                    ),
                ),
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.constraints == (
        "Verification command must be uv run pytest tests/test_hello_auto.py",
        "Use the real verification tool named by the exact command; do not satisfy it "
        "with a local shim or replacement executable. Minimal project metadata and "
        "dependencies needed for that tool are allowed.",
        "Do not add unrelated files, dependencies, or behavior; minimal project metadata "
        "needed to run the exact verification command is allowed.",
    )
    assert normalized.exit_conditions[0].evaluation_criteria == (
        "No unrelated dependencies, frameworks, deployment targets, credentials, or external "
        "side effects are introduced; minimal project metadata and dependencies needed to run "
        "the exact verification command are allowed."
    )


def test_normalize_execution_acceptance_keeps_minimality_constraints_without_exact_command() -> None:
    seed = _seed("Tests cover the behavior.").model_copy(
        update={
            "constraints": (
                "No extra files or behavior beyond the exact return-value test",
                "Avoid new dependencies",
            ),
        }
    )

    assert normalize_execution_acceptance(seed) is seed


def test_normalize_execution_acceptance_keeps_repaired_fragments_without_exact_ac_command() -> None:
    seed = _seed(
        "A command/API check returns stable observable output or artifacts proving the original requirement for hello_auto.py exists at repository root.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Test imports exactly from hello_auto import hello_auto.",
    ).model_copy(
        update={
            "constraints": ("Verification command must be uv run pytest tests/test_hello_auto.py",),
        }
    )

    assert normalize_execution_acceptance(seed) is seed


def test_normalize_execution_acceptance_keeps_original_when_filter_would_empty() -> None:
    seed = _seed("Final report includes auto session id and seed id.")

    assert normalize_execution_acceptance(seed) is seed


def test_normalize_execution_acceptance_preserves_mixed_non_keyword_requirements() -> None:
    seed = _seed(
        "`foo.py` exists.",
        "CLI exits 2 on invalid flags.",
        "HTTP 400 responses include a machine-readable error code.",
        "JSON output matches the documented schema.",
        "Final report includes auto session id and seed path.",
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        "`foo.py` exists.",
        "CLI exits 2 on invalid flags.",
        "HTTP 400 responses include a machine-readable error code.",
        "JSON output matches the documented schema.",
        "Final report includes auto session id and seed path.",
    )


def test_normalize_execution_acceptance_preserves_expected_ooo_auto_output() -> None:
    seed = _seed(
        "The command prints exactly `hello from ooo auto`.",
        "Manual fallback is not used.",
    )

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        "The command prints exactly `hello from ooo auto`.",
        "Manual fallback is not used.",
    )


def test_normalize_execution_acceptance_preserves_product_final_report_and_fallback() -> None:
    seed = _seed(
        "Implement a manual fallback mode for offline users.",
        "The final report endpoint includes the session id field.",
        "The final report endpoint includes seed id and seed path.",
        "Previous blocker history is visible in the admin UI.",
        "Persist last_question for resumed interviews.",
        "Manual fallback is not used.",
    ).model_copy(update={"goal": "Build a reporting API with fallback controls"})

    normalized = normalize_execution_acceptance(seed)

    assert normalized.acceptance_criteria == (
        "Implement a manual fallback mode for offline users.",
        "The final report endpoint includes the session id field.",
        "The final report endpoint includes seed id and seed path.",
        "Previous blocker history is visible in the admin UI.",
        "Persist last_question for resumed interviews.",
        "Manual fallback is not used.",
    )


def test_normalize_execution_acceptance_preserves_exact_product_metadata_requirement() -> None:
    seed = _seed(
        "Final report includes auto session id, seed id, seed path, and test result.",
    ).model_copy(update={"goal": "Build a product final-report endpoint"})

    assert normalize_execution_acceptance(seed) is seed
