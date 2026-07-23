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
    AcceptanceCriterionSpec,
    EvaluationPrinciple,
    ExitCondition,
    InvestmentSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_texts,
    derive_semantic_ac_key,
)


def _seed(*criteria: str | AcceptanceCriterionSpec) -> Seed:
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

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_preserves_requested_hello_auto_value() -> None:
    seed = _seed(
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
    ).model_copy(
        update={
            "goal": (
                "Create hello_auto.py with hello_auto() returning exactly "
                "'hello from ooo auto fresh'. Create tests/test_hello_auto.py "
                "importing hello_auto and asserting that exact value. Verification "
                "command is uv run pytest tests/test_hello_auto.py."
            ),
            "constraints": (
                "hello_auto() must return exactly 'hello from ooo auto fresh'",
                "Only edit hello_auto.py and tests/test_hello_auto.py",
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (
        "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
        "`hello_auto() -> str` returns exactly `hello from ooo auto fresh`, "
        "the test imports `hello_auto` and asserts that exact value, and "
        "the exact command `uv run pytest tests/test_hello_auto.py` passes.",
    )


def test_normalize_execution_acceptance_reads_requested_value_from_constraints() -> None:
    seed = _seed(
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
    ).model_copy(
        update={
            "goal": (
                "Observation run: verify latest main Ouroboros ooo auto with "
                "hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
            ),
            "constraints": (
                "hello_auto() must return exactly 'hello from ooo auto fresh'",
                "Only edit hello_auto.py and tests/test_hello_auto.py",
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (
        "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
        "`hello_auto() -> str` returns exactly `hello from ooo auto fresh`, "
        "the test imports `hello_auto` and asserts that exact value, and "
        "the exact command `uv run pytest tests/test_hello_auto.py` passes.",
    )


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

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


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

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


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

    assert ac_texts(normalized.acceptance_criteria) == (
        "`hello_auto.py` contains a module-level docstring.",
        "`tests/test_hello_auto.py` uses pytest.mark.smoke.",
        "pytest tests/test_hello_auto.py -q passes.",
    )


def test_normalize_execution_acceptance_preserves_surviving_success_contracts() -> None:
    spec = AcceptanceCriterionSpec(
        description="`hello_auto.py` contains a module-level docstring.",
        verify_command="uv run pytest tests/test_hello_auto.py",
        expected_artifacts=("hello_auto.py",),
    )
    seed = _seed(
        "`ooo auto` is dispatched through the installed Ouroboros MCP tool, not interpreted as plain text.",
        spec,
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto. hello_auto returns exactly hello from ooo auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert len(normalized.acceptance_criteria) == 1
    normalized_spec = normalized.acceptance_criteria[0]
    assert isinstance(normalized_spec, AcceptanceCriterionSpec)
    assert normalized_spec.description == spec.description
    assert normalized_spec.verify_command == "uv run pytest tests/test_hello_auto.py"
    assert normalized_spec.expected_artifacts == ("hello_auto.py",)


def test_normalize_execution_acceptance_preserves_canonicalized_success_contract() -> None:
    spec = AcceptanceCriterionSpec(
        description=(
            "A command/API check returns stable observable output or artifacts proving "
            "the original requirement for `hello_auto.py` defines `hello_auto() -> str` "
            "returning exactly `hello from ooo auto`."
        ),
        semantic_ac_key="ac_0123456789abcdef",
        verify_command="uv run pytest tests/test_hello_auto.py",
        expected_artifacts=("hello_auto.py", "tests/test_hello_auto.py"),
        output_assertion="1 passed",
        investment=InvestmentSpec(
            difficulty="low",
            stakes="medium",
            provenance="declared",
            confidence="high",
        ),
    )
    seed = _seed(spec).model_copy(
        update={
            "goal": (
                "Verify current ooo auto with hello_auto.py and tests/test_hello_auto.py; "
                "hello_auto returns exactly hello from ooo auto and "
                "uv run pytest tests/test_hello_auto.py passes."
            )
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert len(normalized.acceptance_criteria) == 1
    normalized_spec = normalized.acceptance_criteria[0]
    assert isinstance(normalized_spec, AcceptanceCriterionSpec)
    assert normalized_spec.description == _SINGLE_HELLO_AUTO_OBSERVATION_AC
    assert normalized_spec.semantic_ac_key == spec.semantic_ac_key
    assert normalized_spec.verify_command == spec.verify_command
    assert normalized_spec.expected_artifacts == spec.expected_artifacts
    assert normalized_spec.output_assertion == spec.output_assertion
    assert normalized_spec.investment == spec.investment


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

    assert ac_texts(normalized.acceptance_criteria) == (
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

    assert ac_texts(normalized.acceptance_criteria) == (
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

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)


def test_normalize_execution_acceptance_collapses_legacy_equivalents_without_duplication() -> None:
    """Legacy-collapse regression: equivalent strings canonicalize to one AC.

    ``Seed`` materializes every legacy string as an ``AcceptanceCriterionSpec``.
    Treating each as an independent structured source duplicated the canonical
    AC and re-ran equivalent execution work; the collapse must yield exactly one
    criterion carrying no phantom success contract.
    """
    seed = _seed(
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)
    only = normalized.acceptance_criteria[0]
    assert isinstance(only, AcceptanceCriterionSpec)
    assert not only.has_success_contract
    # A bare legacy collapse carries a key freshly derived from the canonical
    # description, never a stale key inherited from a source description. (Seed
    # always materializes a derived key, so persistence is a
    # {description, semantic_ac_key} dict — the key must match the canonical
    # text, not any collapsed source.)
    assert only.semantic_ac_key == derive_semantic_ac_key(only)
    stale_source_key = derive_semantic_ac_key(
        AcceptanceCriterionSpec(
            description="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
        )
    )
    assert only.semantic_ac_key != stale_source_key
    serialized = normalized.to_dict()["acceptance_criteria"]
    assert serialized == [
        {
            "description": _SINGLE_HELLO_AUTO_OBSERVATION_AC,
            "semantic_ac_key": derive_semantic_ac_key(only),
        }
    ]


def test_normalize_execution_acceptance_preserves_distinct_contracts_reachably() -> None:
    """Multi-source regression: refusing a collapse is loss-free.

    When several sources carry *different* verification contracts, collapsing
    them into one scalar ``AcceptanceCriterionSpec`` would drop every command
    but the first.  Refusing the collapse must keep EVERY source verbatim —
    both distinct contracts and the bare requirement the canonical text would
    otherwise represent — never silently deleting a caller-authorized AC.
    """
    from ouroboros.mcp.tools.evaluation_handlers import _seed_ac_spec_map

    returns_spec = AcceptanceCriterionSpec(
        description="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        verify_command="python -m pytest tests/test_return.py",
        expected_artifacts=("hello_auto.py",),
    )
    imports_spec = AcceptanceCriterionSpec(
        description="`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        verify_command="python -m pytest tests/test_import.py",
        expected_artifacts=("tests/test_hello_auto.py",),
        output_assertion="1 passed",
    )
    bare_requirement = "The exact command `uv run pytest tests/test_hello_auto.py` passes."
    seed = _seed(
        returns_spec,
        imports_spec,
        # Bare requirement: must survive the refused collapse, not be deleted.
        AcceptanceCriterionSpec(description=bare_requirement),
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    # Every source survives verbatim in source order — nothing deleted.
    assert ac_texts(normalized.acceptance_criteria) == (
        returns_spec.description,
        imports_spec.description,
        bare_requirement,
    )

    spec_map = _seed_ac_spec_map(normalized)
    assert (
        spec_map[returns_spec.description].verify_command == "python -m pytest tests/test_return.py"
    )
    assert (
        spec_map[imports_spec.description].verify_command == "python -m pytest tests/test_import.py"
    )
    assert spec_map[imports_spec.description].output_assertion == "1 passed"


def test_normalize_execution_acceptance_collapses_equivalent_contracts_to_one() -> None:
    """A canonical text backed by byte-identical contracts stays a single AC.

    Equivalent contracts are safe to collapse — there is no evidence to lose —
    so the canonical criterion keeps one reachable contract rather than a
    duplicated one.
    """
    from ouroboros.mcp.tools.evaluation_handlers import _seed_ac_spec_map

    shared = {
        "verify_command": "uv run pytest tests/test_hello_auto.py",
        "expected_artifacts": ("hello_auto.py",),
        "output_assertion": "1 passed",
    }
    seed = _seed(
        AcceptanceCriterionSpec(
            description="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
            **shared,
        ),
        AcceptanceCriterionSpec(
            description="`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
            **shared,
        ),
        AcceptanceCriterionSpec(
            description="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
            **shared,
        ),
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (_SINGLE_HELLO_AUTO_OBSERVATION_AC,)
    spec_map = _seed_ac_spec_map(normalized)
    assert list(spec_map) == [_SINGLE_HELLO_AUTO_OBSERVATION_AC]
    contract = spec_map[_SINGLE_HELLO_AUTO_OBSERVATION_AC]
    assert contract.verify_command == "uv run pytest tests/test_hello_auto.py"
    assert contract.expected_artifacts == ("hello_auto.py",)
    assert contract.output_assertion == "1 passed"


def test_normalize_execution_acceptance_preserves_explicit_semantic_identity() -> None:
    """Distinct explicit semantic keys are identity that collapse must not destroy.

    ``Seed`` auto-derives a key for every criterion, but an *explicitly supplied*
    key (differing from the derived value) is the identity runtime routing and
    recovery correlate events on.  Three hello criteria with distinct explicit
    keys must not fold into one criterion under a single re-derived key.
    """
    keyed = [
        AcceptanceCriterionSpec(
            description="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
            semantic_ac_key="ac_00000000000000a1",
        ),
        AcceptanceCriterionSpec(
            description="`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
            semantic_ac_key="ac_00000000000000b2",
        ),
        AcceptanceCriterionSpec(
            description="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
            semantic_ac_key="ac_00000000000000c3",
        ),
    ]
    seed = _seed(*keyed).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    # Each explicit identity survives — never conflated into one derived key.
    surviving_keys = {
        c.semantic_ac_key
        for c in normalized.acceptance_criteria
        if isinstance(c, AcceptanceCriterionSpec)
    }
    for original in keyed:
        assert original.semantic_ac_key in surviving_keys


def test_normalize_execution_acceptance_preserves_source_order_of_refused_collapse() -> None:
    """Refused collapses keep source position; sequential execution reads order.

    A criterion surviving between two distinct contracts must not be reordered
    around them — persisted Seeds may execute criteria sequentially, where tuple
    order is stage order.
    """
    contract_a = AcceptanceCriterionSpec(
        description="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        verify_command="python -m pytest tests/test_a.py",
    )
    contract_b = AcceptanceCriterionSpec(
        description="`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        verify_command="python -m pytest tests/test_b.py",
    )
    seed = _seed(
        contract_a,
        # A distinct product criterion sitting between the two contracts.
        AcceptanceCriterionSpec(description="`hello_auto.py` contains a module-level docstring."),
        contract_b,
    ).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    texts = ac_texts(normalized.acceptance_criteria)
    # contract_a must precede contract_b, in original source order.
    assert texts.index(contract_a.description) < texts.index(contract_b.description)


def test_normalize_execution_acceptance_never_deletes_same_description_contracts() -> None:
    """Duplicate descriptions must not cost a distinct contract.

    Two criteria that arrive sharing a description but carrying different verify
    commands are a caller-authored (if awkward) Seed.  Normalization must not
    delete either contract to force description uniqueness — both survive to
    durable handoff.  The description-keyed evaluation map is a pre-existing
    boundary limitation, not a licence to discard a valid contract here.
    """
    shared_description = (
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
    )
    first = AcceptanceCriterionSpec(
        description=shared_description,
        verify_command="python -m pytest tests/test_first.py",
    )
    second = AcceptanceCriterionSpec(
        description=shared_description,
        verify_command="python -m pytest tests/test_second.py",
    )
    seed = _seed(first, second).model_copy(
        update={
            "goal": "Observation run: verify latest main Ouroboros ooo auto with hello_auto.py and tests/test_hello_auto.py via ouroboros_auto."
        }
    )

    normalized = normalize_execution_acceptance(seed)

    # Both distinct contracts survive — cardinality and commands preserved.
    surviving = [
        c for c in normalized.acceptance_criteria if isinstance(c, AcceptanceCriterionSpec)
    ]
    commands = {c.verify_command for c in surviving}
    assert "python -m pytest tests/test_first.py" in commands
    assert "python -m pytest tests/test_second.py" in commands


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

    assert ac_texts(normalized.acceptance_criteria) == (
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

    assert ac_texts(normalized.acceptance_criteria) == (
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

    assert ac_texts(normalized.acceptance_criteria) == (
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


def test_normalize_execution_acceptance_drops_library_defaults_for_file_artifact() -> None:
    seed = _seed(
        "All public API symbols are importable from the documented module path.",
        "Unit tests cover every public function/method's primary success path.",
        "`ruff check` and the project's type-check command exit 0.",
        "pi_auto_smoke.txt exists at repository root.",
        "pi_auto_smoke.txt full content exactly pi-auto-ok followed by a newline.",
    ).model_copy(
        update={
            "goal": (
                "Create a tiny smoke-test file named pi_auto_smoke.txt. "
                "The file must contain exactly the single line pi-auto-ok."
            ),
            "constraints": ("Keep the implementation to this one smoke-test file.",),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (
        "pi_auto_smoke.txt exists at repository root.",
        "pi_auto_smoke.txt full content exactly pi-auto-ok followed by a newline.",
    )


def test_normalize_execution_acceptance_drops_wrapped_library_defaults_for_file_artifact() -> None:
    seed = _seed(
        "A command/API check returns stable observable output or artifacts proving the original requirement for All public API symbols importable from the documented module path.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Unit tests cover every public function/method's primary success path.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for `ruff check` and the project's type-check command exit 0.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for pi_auto_smoke.txt exists at repository root.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for pi_auto_smoke.txt full content exactly pi-auto-ok followed by a newline.",
    ).model_copy(
        update={
            "goal": (
                "Create a tiny smoke-test file named pi_auto_smoke.txt. "
                "The file must contain exactly the single line pi-auto-ok."
            ),
            "constraints": ("Keep the implementation to this one smoke-test file.",),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (
        "A command/API check returns stable observable output or artifacts proving the original requirement for pi_auto_smoke.txt exists at repository root.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for pi_auto_smoke.txt full content exactly pi-auto-ok followed by a newline.",
    )


def test_normalize_execution_acceptance_preserves_library_defaults_for_api_goal() -> None:
    seed = _seed(
        "All public API symbols are importable from the documented module path.",
        "Unit tests cover every public function/method's primary success path.",
        "`ruff check` and the project's type-check command exit 0.",
        "client.py exists.",
    ).model_copy(update={"goal": "Build an importable SDK package with a public API"})

    assert normalize_execution_acceptance(seed) is seed


def test_normalize_execution_acceptance_canonicalizes_autoresearch_contract() -> None:
    seed = _seed(
        "A command/API check returns stable observable output or artifacts proving the original requirement for Seed preserves explicit Runtime Context, Non-Goals, and Acceptance Criteria as first-class content for the autoresearch contract.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Seed requires execution to record a baseline uv run train.py result before any experiment changes evaluated.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Seed requires up to two post-baseline experiments to selected sequentially from the current best state, with improvements kept and all non-improvements reverted before the next attempt.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Seed requires every baseline and experiment ledger entry to report command, changed files, diff summary, observed val_bpb, memory, status, and keep/discard conclusion.",
        "A command/API check returns stable observable output or artifacts proving the original requirement for Seed requires final kept changes to limited to train.py unless explicit scope widening recorded in the ledger.",
        "Seed defines discard behavior for ties, regressions, invalid runs, missing val_bpb, missing memory, timeouts, memory-heavy behavior, nonzero exits, and unauthorized file changes.",
    ).model_copy(
        update={
            "goal": (
                "Run a bounded Karpathy-style autoresearch loop.\n"
                "Repository: /tmp/autoresearch-demo\n"
                "Treat program.md as instructions, edit only "
                "train.py, use val_bpb as the primary metric, and verify with uv run train.py."
            ),
            "constraints": (
                "Runtime Context: local autoresearch repository with train.py and prepare.py.",
                "Non-Goals: do not edit prepare.py.",
                "Run at most 2 experiments.",
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    assert ac_texts(normalized.acceptance_criteria) == (
        "The experiment ledger artifact contains a baseline entry written before any edit; it includes measured command `/usr/bin/time -l uv run train.py`, inner command, exit status, val_bpb, maximum resident set size bytes, and baseline status.",
        "The experiment ledger artifact contains at most two train.py-only experiment entries, each evaluated with the same measured command and timeout budget.",
        "The experiment ledger artifact contains sequential decision entries; each entry includes keep/revert status from the current best state, keeping strict val_bpb improvements and reverting ties, regressions, invalid runs, timeouts, crashes, missing metrics, missing memory, and unauthorized scope changes before the next attempt.",
        "Every baseline and experiment ledger artifact entry includes command, changed files, diff summary, observed val_bpb, memory, status, and keep/discard conclusion.",
        "The final git diff artifact contains only train.py changes unless scope_widening_ledger contains an explicit justification for a wider edit.",
        "The final report artifact includes baseline val_bpb, each attempted experiment result, final best val_bpb, and the keep/discard reason for every candidate.",
    )
    assert normalized.to_dict()["runtime_context"] == {
        "repository_path": "/tmp/autoresearch-demo",
        "research_program": "program.md",
        "editable_files": ["train.py"],
        "fixed_files": ["prepare.py"],
        "verification_command": "uv run train.py",
        "measurement_command": "/usr/bin/time -l uv run train.py",
        "experiment_budget": 2,
        "timeout_seconds": 60,
        "primary_metric": "val_bpb",
        "metric_direction": "lower_is_better",
        "memory_source": "maximum resident set size from /usr/bin/time -l stderr, recorded as bytes.",
        "memory_heavy_threshold": "discard if experiment memory exceeds baseline by more than max(10% of baseline, 67108864 bytes).",
    }
    assert normalized.to_dict()["non_goals"] == [
        "Do not edit prepare.py.",
        "Do not edit files outside train.py unless scope_widening_ledger explicitly widens scope.",
        "Do not install dependencies, change package metadata, or modify the evaluation harness.",
        "Do not run training during Seed creation.",
    ]


def test_normalize_execution_acceptance_preserves_distinct_autoresearch_user_ac() -> None:
    seed = _seed(
        "Seed requires execution to record a baseline uv run train.py result before any experiment changes evaluated.",
        "train.py must preserve the existing --device CLI flag behavior.",
        "Final report must include the baseline val_bpb and memory.",
    ).model_copy(
        update={
            "goal": (
                "Run a bounded Karpathy-style autoresearch loop. "
                "Edit train.py and optimize val_bpb."
            ),
            "constraints": (
                "Runtime Context: local autoresearch repository with train.py.",
                "Non-Goals: do not edit prepare.py.",
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    normalized_texts = ac_texts(normalized.acceptance_criteria)
    assert "train.py must preserve the existing --device CLI flag behavior." in normalized_texts
    assert "Final report must include the baseline val_bpb and memory." in normalized_texts


def test_normalize_execution_acceptance_preserves_structured_contract_across_autoresearch() -> None:
    """Finding #2 regression: autoresearch canonicalization keeps contracts.

    The autoresearch normalizer replaces source descriptions with its six fixed
    canonical ACs, none of which the hello_auto matcher recognizes.  A source
    that carried an explicit verification contract must not silently degrade to
    a contractless canonical criterion — its evidence is re-appended verbatim.
    """
    from ouroboros.mcp.tools.evaluation_handlers import _seed_ac_spec_map

    baseline_spec = AcceptanceCriterionSpec(
        description="Seed requires execution to record a baseline uv run train.py result before any experiment changes evaluated.",
        verify_command="/usr/bin/time -l uv run train.py",
        expected_artifacts=("experiment_ledger.json",),
        output_assertion="baseline recorded",
    )
    seed = _seed(baseline_spec).model_copy(
        update={
            "goal": (
                "Run a bounded Karpathy-style autoresearch loop. "
                "Edit train.py and optimize val_bpb."
            ),
            "constraints": (
                "Runtime Context: local autoresearch repository with train.py.",
                "Non-Goals: do not edit prepare.py.",
            ),
        }
    )

    normalized = normalize_execution_acceptance(seed)

    # The six canonical autoresearch ACs are present, and the structured source
    # contract survives verbatim and reachable rather than being dropped.
    normalized_texts = ac_texts(normalized.acceptance_criteria)
    assert baseline_spec.description in normalized_texts
    spec_map = _seed_ac_spec_map(normalized)
    assert baseline_spec.description in spec_map
    preserved = spec_map[baseline_spec.description]
    assert preserved.verify_command == "/usr/bin/time -l uv run train.py"
    assert preserved.expected_artifacts == ("experiment_ledger.json",)
    assert preserved.output_assertion == "baseline recorded"


def test_normalize_execution_acceptance_preserves_existing_autoresearch_runtime_context() -> None:
    seed = Seed.from_dict(
        {
            **_seed(
                "Seed requires execution to record a baseline uv run train.py result before any experiment changes evaluated."
            ).to_dict(),
            "goal": "Run a bounded Karpathy-style autoresearch loop over train.py val_bpb.",
            "constraints": ["Runtime Context: local autoresearch repository with train.py."],
            "runtime_context": {
                "repository_path": "/custom/repo",
                "measurement_command": "python custom_measure.py",
                "timeout_seconds": 120,
                "experiment_budget": 3,
            },
        }
    )

    normalized = normalize_execution_acceptance(seed)

    runtime_context = normalized.to_dict()["runtime_context"]
    assert runtime_context["repository_path"] == "/custom/repo"
    assert runtime_context["measurement_command"] == "python custom_measure.py"
    assert runtime_context["timeout_seconds"] == 120
    assert runtime_context["experiment_budget"] == 3
    assert runtime_context["verification_command"] == "uv run train.py"


def test_normalize_execution_acceptance_leaves_non_autoresearch_train_metric_seed_alone() -> None:
    seed = _seed(
        "A command/API check returns stable observable output or artifacts proving the task goal.",
    ).model_copy(
        update={
            "goal": "Build a training dashboard that can display a val_bpb column for train.py runs."
        }
    )

    assert normalize_execution_acceptance(seed) is seed
