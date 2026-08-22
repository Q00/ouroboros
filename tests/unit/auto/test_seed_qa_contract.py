from types import SimpleNamespace

from ouroboros.auto.seed_qa_contract import inherited_parent_seed_id


def _seed(goal: str) -> SimpleNamespace:
    return SimpleNamespace(goal=goal)


def test_parent_seed_uses_explicit_positive_inheritance_clause() -> None:
    seed = _seed("Reference seed_old for context; inherit seed_parent.")

    assert inherited_parent_seed_id(seed) == "seed_parent"


def test_parent_seed_ignores_negated_reference() -> None:
    seed = _seed("Do not inherit seed_old; inherit seed_parent.")

    assert inherited_parent_seed_id(seed) == "seed_parent"


def test_parent_seed_ignores_adverb_qualified_negation() -> None:
    for goal in (
        "Do not ever inherit seed_bad.",
        "Never directly inherit seed_bad.",
        "The Seed must not inherit seed_bad.",
        "Cannot inherit seed_bad.",
        "Continue without inheriting seed_bad.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_uses_positive_correction_after_negated_candidate() -> None:
    seed = _seed("Do not inherit seed_bad, instead inherit seed_good.")

    assert inherited_parent_seed_id(seed) == "seed_good"


def test_parent_seed_scopes_conjunction_negation_to_each_candidate() -> None:
    for goal in (
        "Inherit seed_good and do not copy its obsolete constraints.",
        "Do not inherit seed_bad and instead inherit seed_good.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_good"


def test_parent_seed_survives_adjacent_negative_constraint() -> None:
    for goal in (
        "Inherit seed_good without copying obsolete constraints.",
        "Derive from seed_good although we must not reuse its runtime settings.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_good"


def test_parent_seed_ignores_quoted_and_historical_references() -> None:
    for goal in (
        'The phrase "inherit seed_bad" is an example, not a requirement.',
        "We discussed inherit seed_bad in the rejected proposal.",
        "Do not copy obsolete constraints because this Seed should inherit seed_good.",
    ):
        expected = "seed_good" if "should inherit" in goal else None
        assert inherited_parent_seed_id(_seed(goal)) == expected


def test_parent_seed_scopes_possessives_and_historical_governors() -> None:
    assert (
        inherited_parent_seed_id(
            _seed("The previous proposal was rejected, but it said inherit seed_bad for reference.")
        )
        is None
    )
    assert inherited_parent_seed_id(_seed("Inherit seed_good for John's project.")) == ("seed_good")


def test_parent_seed_ignores_unrelated_same_clause_details() -> None:
    assert (
        inherited_parent_seed_id(_seed("Inherit seed_good to explain why seed_bad was rejected."))
        == "seed_good"
    )


def test_parent_seed_ignores_reference_examples_after_binding() -> None:
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_good. For reference, inherit seed_bad is an example.")
        )
        == "seed_good"
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_good. The docs show inherit seed_bad as an example.")
        )
        == "seed_good"
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_good for either a PDF or DOCX migration note.")
        )
        == "seed_good"
    )


def test_parent_seed_preserves_comma_separated_governors() -> None:
    for reference in ("For example", "For reference", "As an example", "e.g."):
        assert inherited_parent_seed_id(_seed(f"{reference}, inherit seed_bad.")) is None
        assert (
            inherited_parent_seed_id(_seed(f"Inherit seed_good. {reference}, inherit seed_bad."))
            == "seed_good"
        )
    for negated in (
        "Do not, ever, inherit seed_bad.",
        "Never, under any circumstances, inherit seed_bad.",
    ):
        assert inherited_parent_seed_id(_seed(negated)) is None


def test_parent_seed_scopes_descriptive_and_reference_prose() -> None:
    assert (
        inherited_parent_seed_id(_seed("Summarize the rejected proposal with inherit seed_good."))
        == "seed_good"
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_good. As a reference, inherit seed_bad appears in old docs.")
        )
        == "seed_good"
    )


def test_parent_seed_rejects_ordinary_negative_inheritance_language() -> None:
    for goal in (
        "It is false that we inherit seed_bad.",
        "Start fresh instead of inheriting seed_bad.",
        "Rather than inherit seed_bad, start fresh.",
        "We no longer inherit seed_bad.",
        "In the previous proposal, inherit seed_bad.",
        "It is not true that we inherit seed_bad.",
        "Inherit seed_bad will not be used.",
        "Inheriting from seed_bad is not required.",
        "Inheriting from seed_bad isn't required for this Seed.",
        "Inheriting seed_bad is unnecessary for this Seed.",
        "Inherit seed_bad, but not required.",
        "Inherit seed_bad, but no longer needed.",
        "Inherit seed_bad, but unnecessary.",
        "Inherit seed_bad, pending approval.",
        "Inherit seed_bad, unless the user confirms migration.",
        "Inherit seed_bad, but maybe seed_good.",
        "Inherit seed_bad need not be used.",
        "Inherit seed_bad only if requested.",
        "Use seed_bad as the parent seed only when resuming an interrupted run.",
        "Inherit seed_bad is no longer required.",
        "We won't inherit seed_bad.",
        "There is no requirement to inherit seed_bad.",
        "The team declined to inherit seed_bad.",
        "We refused to inherit seed_bad.",
        "We chose not to inherit seed_bad.",
        "We didn't inherit seed_bad.",
        "We didn’t inherit seed_bad.",
        "We decided not to inherit seed_bad.",
        "The requirement to inherit seed_bad was declined.",
        "We rejected inherit seed_bad.",
        "We abandoned the plan to inherit seed_bad.",
        "Inherit seed_bad, which was rejected.",
        "We ruled out inheriting seed_bad.",
        "We opted not to inherit seed_bad.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_resets_governors_at_punctuation_boundary() -> None:
    goal = "Rather than copy old constraints, start the repair; inherit seed_good."

    assert inherited_parent_seed_id(_seed(goal)) == "seed_good"


def test_parent_seed_accepts_ordinary_positive_wording() -> None:
    for goal in (
        "Inherit from seed_parent.",
        "Set parent_seed_id to seed_parent.",
        "Use seed_parent as the parent seed.",
        "Inherit seed_parent for the final proposal.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_parent"


def test_parent_seed_requires_inheritance_semantics() -> None:
    seed = _seed("Compare seed_old with seed_candidate.")

    assert inherited_parent_seed_id(seed) is None


def test_parent_seed_ignores_descriptive_mentions_and_comparisons() -> None:
    for goal in (
        "Inherit seed_good. Mention parent_seed_id: seed_bad in the appendix.",
        "Inherit seed_good. Compare with parent_seed_id: seed_bad.",
        "Inherit seed_good. This document mentions parent_seed_id: seed_bad in prose.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_good"


def test_parent_seed_keeps_affirmative_tail_and_rejects_conditional_conjunction() -> None:
    assert inherited_parent_seed_id(_seed("parent_seed_id: seed_good should be used.")) == (
        "seed_good"
    )
    assert (
        inherited_parent_seed_id(_seed("If approved and we inherit seed_good, copy its settings."))
        is None
    )


def test_parent_seed_honors_later_standalone_retractions() -> None:
    for goal in (
        "Inherit seed_old. Actually, scratch that.",
        "Inherit seed_old. We decided against it.",
        "Inherit seed_old. That requirement was rejected.",
        "Inherit seed_old. Never mind.",
        "Inherit seed_old. Forget that.",
        "Inherit seed_old. Cancel that requirement.",
        "Inherit seed_old. I take that back.",
        "Inherit seed_old. However, cancel that requirement.",
        "Inherit seed_old. But cancel that requirement.",
        "Inherit seed_old. That was only an example.",
        "Inherit seed_old. I was only giving an example.",
        "Inherit seed_old. Please disregard that requirement.",
        "Inherit seed_old. Withdraw that requirement.",
        "Inherit seed_old. Retract that requirement.",
        "Inherit seed_old. Cancel this requirement.",
        "Inherit seed_old. Forget it.",
        "Inherit seed_old. I take it back.",
        "Inherit seed_old. Cancel it.",
        "Inherit seed_old. Cancel the parent requirement.",
        "Inherit seed_good. Use SVG. Cancel the parent requirement.",
        "Inherit seed_good. Use SVG. Retract the parent contract.",
        "Inherit seed_old; cancel that requirement.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_ignores_validation_content_and_field_specific_retraction() -> None:
    assert (
        inherited_parent_seed_id(_seed("Write a validator that parses parent_seed_id: seed_bad."))
        is None
    )
    for goal in (
        "Write unit tests asserting parent_seed_id: seed_bad.",
        "Create documentation showing how to set parent_seed_id: seed_bad.",
        "Build a validator that rejects parent_seed_id: seed_bad.",
        "Write docs recommending inherit seed_old to users.",
        "Add support for parent_seed_id: seed_demo in the API.",
        "The schema must accept parent_seed_id: seed_demo.",
        "Add a test for parent_seed_id: seed_demo.",
        "Fix handling when users inherit seed_demo.",
        "Analyze why the API accepts parent_seed_id: seed_old.",
        "Explain how the schema stores parent_seed_id: seed_old.",
        "Rename parent_seed_id: seed_old to predecessor_id.",
        "Remove parent_seed_id: seed_old from the API.",
        "Implement support for inheriting from seed_demo.",
        "Add support for inheriting from seed_demo.",
        "Test inheriting from seed_demo.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_old. task_type: document. Cancel the parent requirement.")
        )
        is None
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_old. task_type: document. Cancel the task type requirement.")
        )
        == "seed_old"
    )
    for goal in (
        "Inherit seed_old. The report should say parent_seed_id: seed_fake.",
        "Inherit seed_old. A test fixture contains parent_seed_id: seed_fake.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_old"


def test_parent_seed_ignores_api_schema_and_parser_content() -> None:
    for goal in (
        "The API returns parent_seed_id: seed_fake.",
        "The schema property parent_seed_id is seed_fake.",
        "Ensure the parser preserves strings where parent_seed_id: seed_fake.",
        "Persist parent_seed_id: seed_demo in the database.",
        "Store parent_seed_id: seed_demo in the session state.",
        "Expose parent_seed_id: seed_demo in the CLI output.",
        "The config field parent_seed_id: seed_demo controls resume.",
        "Route records with parent_seed_id: seed_demo to replay.",
        "Treat parent_seed_id: seed_demo as an opaque string.",
        "Read parent_seed_id: seed_demo from the config.",
        "When parent_seed_id is provided, set parent_seed_id: seed_fake in the result.",
        "Handle parent_seed_id: seed_historical during migration while preserving current repair lineage.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_ignores_unrelated_bare_retraction_details() -> None:
    for goal in (
        "Inherit seed_good. Never mind the earlier color choice.",
        "Inherit seed_good. Scratch that old heading.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) == "seed_good"


def test_parent_seed_scopes_retraction_to_nearest_contract() -> None:
    assert (
        inherited_parent_seed_id(
            _seed("task_type: document. Inherit seed_old. Cancel that requirement.")
        )
        is None
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_old. task_type: document. Cancel that requirement.")
        )
        == "seed_old"
    )


def test_parent_seed_accepts_replacement_after_cancellation() -> None:
    seed = _seed("Inherit seed_old. Cancel that requirement. Inherit seed_new.")

    assert inherited_parent_seed_id(seed) == "seed_new"


def test_parent_seed_preserves_korean_inheritance_contract() -> None:
    seed = _seed("seed_parent를 계승해 문서형 Seed로 명세한다.")

    assert inherited_parent_seed_id(seed) == "seed_parent"


def test_parent_seed_rejects_typographic_quoted_conditional_and_ambiguous_language() -> None:
    for goal in (
        "We won’t inherit seed_bad.",
        "Inherit seed_bad should be avoided.",
        'The old docs say "Inherit seed_bad for migrations." Replace that guidance.',
        "If we inherit seed_bad, copy settings; otherwise start fresh.",
        "Inherit seed_one or seed_two after review.",
        "Inherit seed_bad pending approval.",
        "Inherit seed_bad after approval.",
        "Inherit seed_bad once approved.",
        "Inherit seed_bad upon approval.",
        "Inherit seed_bad assuming approval.",
        "Inherit seed_bad contingent on approval.",
        "Inherit seed_bad subject to approval.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_rejects_explanatory_optional_and_single_quoted_language() -> None:
    for goal in (
        "Add migration notes explaining how to inherit seed_old.",
        "The docs say 'inherit seed_old' for migrations.",
        "Only if approved, inherit seed_old.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_scopes_mixed_clause_authority_to_contract() -> None:
    assert (
        inherited_parent_seed_id(
            _seed("Whether to copy settings or rebuild them is undecided, but inherit seed_good.")
        )
        == "seed_good"
    )


def test_parent_seed_rejects_modal_or_optional_contracts() -> None:
    for goal in (
        "We may inherit seed_bad.",
        "Inheriting from seed_bad is optional.",
        "It does not inherit from seed_bad.",
        "Inherit seed_one and inherit seed_two.",
        "Inherit seed_one; inherit seed_two.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None
    assert (
        inherited_parent_seed_id(_seed("Inherit from seed_good for an optional migration note."))
        == "seed_good"
    )


def test_parent_seed_handles_contracted_korean_and_corrected_lineage() -> None:
    for goal in ("It doesn't inherit seed_bad.", "seed_bad를 상속하면 안 됩니다."):
        assert inherited_parent_seed_id(_seed(goal)) is None
    assert (
        inherited_parent_seed_id(_seed("Inherit seed_old. Correction: inherit seed_new."))
        == "seed_new"
    )
    assert (
        inherited_parent_seed_id(_seed("Inherit seed_old. Actually, inherit seed_new."))
        == "seed_new"
    )
    for goal in (
        "No, inherit seed_bad.",
        "Inherit seed_bad, but not anymore.",
        "Inherit seed_bad, but we decided against it.",
        "Inherit seed_bad, but scratch that.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_old. Actually, inherit seed_new. Confirmed: inherit seed_new.")
        )
        == "seed_new"
    )
    assert (
        inherited_parent_seed_id(
            _seed("Inherit seed_old. Use SVG instead of PNG. Separately, inherit seed_new.")
        )
        is None
    )
    assert (
        inherited_parent_seed_id(_seed("Inherit seed_good, not inherit seed_bad.")) == "seed_good"
    )
    assert (
        inherited_parent_seed_id(_seed("Inherit seed_bad, but that requirement was rejected."))
        is None
    )


def test_parent_seed_ignores_artifact_payload_fields() -> None:
    for goal in (
        "Generate a YAML example containing parent_seed_id: seed_old.",
        "Return JSON with parent_seed_id: seed_old.",
        "The generated manifest must set parent_seed_id: seed_old.",
        "Write a README saying inherit seed_external.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_accepts_schema_valid_identifier_characters() -> None:
    for goal, expected in (
        ("Inherit seed_parent_001.", "seed_parent_001"),
        ("Set parent_seed_id to seed_mechanical_eval_minimal.", "seed_mechanical_eval_minimal"),
        ("Inherit seed_4749408237de-auto_35d.", "seed_4749408237de-auto_35d"),
    ):
        assert inherited_parent_seed_id(_seed(goal)) == expected


def test_parent_seed_ignores_help_error_and_documentation_content() -> None:
    for goal in (
        "Add a CLI flag whose help text says inherit seed_fake.",
        "Implement a validator whose error message says inherit seed_fake.",
        "Create docs with the sentence inherit seed_fake.",
    ):
        assert inherited_parent_seed_id(_seed(goal)) is None


def test_parent_seed_accepts_nonprefixed_schema_identifiers() -> None:
    for goal, expected in (
        ("Inherit release-parent-v2.", "release-parent-v2"),
        ("Set parent_seed_id to another-id.", "another-id"),
        ("Inherit seed-parent-v2.", "seed-parent-v2"),
        ("Set parent_seed_id to release.2026.", "release.2026"),
        ("Set parent_seed_id to seed.parent", "seed.parent"),
        ("Set parent_seed_id to foo=bar", "foo=bar"),
        ("Inherit seed#42.", "seed#42"),
        ("Inherit 부모#42.", "부모#42"),
    ):
        assert inherited_parent_seed_id(_seed(goal)) == expected
