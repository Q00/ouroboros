from ouroboros.core.task_type import explicit_task_type_from_goal


def test_explicit_task_type_ignores_question_and_uses_answer() -> None:
    transcript = "Q: Should task_type be code or document?\nA: task_type must be document."

    assert explicit_task_type_from_goal(transcript) == "document"


def test_explicit_task_type_uses_final_correction() -> None:
    for goal in (
        "task_type must be code. Correction: task_type must be document.",
        "task_type must be code. Actually, task_type must be document.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_rejects_direct_and_trailing_retractions() -> None:
    for goal in (
        "No, task_type: document.",
        "task_type: document, but not anymore.",
        "task_type: document, but we decided against it.",
        "task_type: document, but scratch that.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_keeps_correction_after_duplicate_confirmation() -> None:
    goal = "task_type: code. Actually, task_type: document. Confirmed: task_type: document."

    assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_scopes_predicate_to_binding_clause() -> None:
    assert (
        explicit_task_type_from_goal(
            "Without changing the existing task type, task_type must remain document."
        )
        == "document"
    )
    assert (
        explicit_task_type_from_goal("We may revise the title, and task_type: document.")
        == "document"
    )
    for goal in (
        "task_type: code. Actually, keep the title short. Separately, task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_ignores_superseded_clause() -> None:
    goal = "Ignore superseded task_type: code. task_type: research."

    assert explicit_task_type_from_goal(goal) == "research"


def test_explicit_task_type_ignores_non_binding_mentions() -> None:
    for goal in (
        "Should task_type: document?",
        "Document the literal example `task_type: code` for users.",
        "Do not use task_type: document.",
        "We cannot use task_type: document.",
        "We can't use task_type: document.",
        "Without using task_type: document, describe the migration.",
        "The task_type: document value is not allowed.",
        "The task_type: document value must not be used.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_uses_positive_correction_after_negated_candidate() -> None:
    goal = "Do not use task_type: code, instead use task_type: document."

    assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_scopes_conjunction_negation_to_each_candidate() -> None:
    for goal in (
        "Use task_type: document and do not modify source code.",
        "Do not use task_type: code and instead use task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_survives_adjacent_negative_constraint() -> None:
    for goal in (
        "task_type must be document without changing repository files.",
        "Use task_type: document although we must not produce code.",
        "Use task_type: document because source code must not change.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_ignores_causal_prefix_and_historical_contracts() -> None:
    assert (
        explicit_task_type_from_goal(
            "Do not modify source code because task_type must be document."
        )
        == "document"
    )
    for goal in (
        "We discussed task_type: document in the rejected proposal. Build a CLI.",
        'The phrase "task_type: document" is an example, not a requirement.',
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_scopes_quotes_and_historical_governors_to_contract() -> None:
    assert (
        explicit_task_type_from_goal(
            "The previous proposal was rejected, but its task_type: document "
            "should be recorded for audit."
        )
        is None
    )
    for goal in (
        "We'll use task_type: document for the final plan.",
        "Use task_type: document for the historical archive.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_rejects_ordinary_negative_contract_language() -> None:
    for goal in (
        "The task_type: document requirement was rejected.",
        "Use the default code task instead of task_type: document.",
        "Rather than use task_type: document, keep the code task.",
        "We no longer use task_type: document.",
        "In the previous proposal, task_type: document.",
        "It is not true that task_type: document.",
        "We are not using task_type: document.",
        "task_type: document will not be used.",
        "task_type: document is not required for this Seed.",
        "task_type: document isn't required for this Seed.",
        "task_type: document is unnecessary for this Seed.",
        "task_type: document, but not required.",
        "task_type: document, but no longer needed.",
        "task_type: document, but unnecessary.",
        "Use task_type: document, pending approval.",
        "Use task_type: document, unless the user asks for code.",
        "Use task_type: document, but maybe code.",
        "task_type: document need not be used.",
        "The task type is document only if requested.",
        "The task type is document only when explicitly requested.",
        "task_type: document is no longer required.",
        "We won't use task_type: document.",
        "We did not select task_type: document.",
        "We have not selected task_type: document.",
        "There is no requirement that task_type: document.",
        "The team declined to use task_type: document.",
        "We didn't select task_type: document.",
        "We didn’t select task_type: document.",
        "We decided not to use task_type: document.",
        "The requirement to use task_type: document was declined.",
        "We rejected task_type: document.",
        "We abandoned task_type: document.",
        "task_type: document, which was rejected.",
        "We ruled out task_type: document.",
        "We opted not to use task_type: document.",
        "The task type isn't document.",
    ):
        assert explicit_task_type_from_goal(goal) is None
    assert explicit_task_type_from_goal("Use task_type: document rather than code.") == "document"


def test_explicit_task_type_resets_governors_at_punctuation_boundary() -> None:
    assert (
        explicit_task_type_from_goal(
            "Rather than change source code, write a plan; task_type: document."
        )
        == "document"
    )


def test_explicit_task_type_accepts_ordinary_positive_wording() -> None:
    for goal in (
        "The task type is document.",
        "Set the task type to document.",
        "The task type must remain document.",
        "Keep the task type as document.",
        "Use document as the task type.",
        "The task type must be a document.",
        "The task type should be a document.",
        "Implement this as task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"
    assert (
        explicit_task_type_from_goal("Use task_type: document for the final proposal.")
        == "document"
    )


def test_explicit_task_type_rejects_typographic_quoted_and_conditional_language() -> None:
    for goal in (
        "We won’t use task_type: document.",
        "task_type: document should be avoided.",
        "If task_type is document, write a guide; otherwise keep code.",
        "Choose between task_type: document or code later.",
        'The docs currently say "Set task_type: document for exports." Replace that guidance.',
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_rejects_explanatory_optional_and_single_quoted_text() -> None:
    assert (
        explicit_task_type_from_goal(
            "Set the task type to document. Add a section explaining task_type: code."
        )
        == "document"
    )
    for goal in (
        "Only if approved, use task_type: document.",
        "The docs say 'task_type: code' for legacy exports.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_scopes_mixed_clause_authority_to_contract() -> None:
    assert (
        explicit_task_type_from_goal(
            "Whether to include charts or tables is undecided, but task_type: document."
        )
        == "document"
    )


def test_explicit_task_type_accepts_documentation_as_a_supported_value() -> None:
    assert explicit_task_type_from_goal("Set the task type to documentation.") == "documentation"
    assert (
        explicit_task_type_from_goal("Use task_type: document to create documentation.")
        == "document"
    )


def test_explicit_task_type_ignores_unrelated_same_clause_details() -> None:
    assert (
        explicit_task_type_from_goal(
            "Use task_type: document to explain why the old proposal was rejected."
        )
        == "document"
    )


def test_explicit_task_type_ignores_reference_examples_after_binding() -> None:
    assert (
        explicit_task_type_from_goal(
            "task_type: document. For reference, task_type: code is an example."
        )
        == "document"
    )
    assert (
        explicit_task_type_from_goal(
            "task_type: document. The docs show task_type: code as an example."
        )
        == "document"
    )
    assert (
        explicit_task_type_from_goal("Use task_type: document for either PDF or DOCX output.")
        == "document"
    )


def test_explicit_task_type_preserves_comma_separated_governors() -> None:
    for reference in ("For example", "For reference", "As an example", "e.g."):
        assert explicit_task_type_from_goal(f"{reference}, task_type: code.") is None
        assert (
            explicit_task_type_from_goal(
                f"The task type must be document. {reference}, task_type: code."
            )
            == "document"
        )
    for negated in (
        "Do not, under any circumstances, use task_type: document.",
        "Do not, ever, set task_type: document.",
        "Never, even temporarily, use task_type: document.",
    ):
        assert explicit_task_type_from_goal(negated) is None


def test_explicit_task_type_scopes_descriptive_and_reference_prose() -> None:
    assert (
        explicit_task_type_from_goal("Create a guide explaining setup with task_type: document.")
        == "document"
    )
    assert (
        explicit_task_type_from_goal("Summarize the rejected proposal with task_type: document.")
        == "document"
    )
    assert (
        explicit_task_type_from_goal(
            "Use task_type: document. As a reference, task_type: code appears in old docs."
        )
        == "document"
    )


def test_explicit_task_type_rejects_modal_contracts() -> None:
    assert explicit_task_type_from_goal("We may use task_type: document.") is None
    assert explicit_task_type_from_goal("The task type does not need to be document.") is None
    assert (
        explicit_task_type_from_goal("Use task_type: document for an optional appendix.")
        == "document"
    )


def test_explicit_task_type_rejects_contracted_korean_and_conflicting_negatives() -> None:
    for goal in (
        "The system doesn't use task_type: document.",
        "task_type은 document로 설정하지 마세요.",
        "task_type: research and task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) is None
    assert (
        explicit_task_type_from_goal("task_type: research. Correction: task_type: document.")
        == "document"
    )
    assert (
        explicit_task_type_from_goal("Use task_type: document, not task_type: code.") == "document"
    )
    assert (
        explicit_task_type_from_goal("task_type: document, but that requirement was rejected.")
        is None
    )


def test_explicit_task_type_ignores_descriptive_mentions_and_comparisons() -> None:
    for goal in (
        "Use task_type: document. Mention task_type: code in the appendix.",
        "Use task_type: document. Compare it with task_type: code.",
        "Use task_type: document. This document mentions task_type: code in prose.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_keeps_affirmative_tail_and_rejects_conditional_conjunction() -> None:
    assert explicit_task_type_from_goal("task_type: document should be used.") == "document"
    assert (
        explicit_task_type_from_goal(
            "If approved and we use task_type: document, generate the report."
        )
        is None
    )


def test_explicit_task_type_honors_later_standalone_retractions() -> None:
    for goal in (
        "task_type: document. Actually, scratch that.",
        "task_type: document. We decided against it.",
        "task_type: document. That requirement was rejected.",
        "task_type: document. Never mind.",
        "task_type: document. Forget that.",
        "task_type: document. Cancel that requirement.",
        "task_type: document. I take that back.",
        "task_type: document. However, cancel that requirement.",
        "task_type: document. But cancel that requirement.",
        "task_type: document. That was only an example.",
        "task_type: document. I was only giving an example.",
        "task_type: document. Please disregard that requirement.",
        "task_type: document. Withdraw that requirement.",
        "task_type: document. Retract that requirement.",
        "task_type: document. Cancel this requirement.",
        "task_type: document. Forget it.",
        "task_type: document. I take it back.",
        "task_type: document. Cancel it.",
        "task_type: document. Cancel the task type requirement.",
        "task_type: document; cancel that requirement.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_scopes_retraction_to_nearest_contract() -> None:
    assert (
        explicit_task_type_from_goal(
            "task_type: document. Inherit seed_old. Cancel that requirement."
        )
        == "document"
    )
    assert (
        explicit_task_type_from_goal(
            "Inherit seed_old. task_type: document. Cancel that requirement."
        )
        is None
    )


def test_explicit_task_type_accepts_replacement_after_cancellation() -> None:
    assert (
        explicit_task_type_from_goal(
            "task_type: code. Cancel that requirement. task_type: document."
        )
        == "document"
    )


def test_explicit_task_type_ignores_unrelated_cancellation_and_output_prose() -> None:
    assert explicit_task_type_from_goal("Use task_type: document. Cancel lunch.") == "document"
    assert (
        explicit_task_type_from_goal("Use task_type: document. Cancel the parent requirement.")
        == "document"
    )
    assert (
        explicit_task_type_from_goal(
            "Write a Python validator that emits the line task_type: document."
        )
        is None
    )
    assert (
        explicit_task_type_from_goal(
            "Build a CLI that explains why task_type: document is unsupported."
        )
        is None
    )
    assert (
        explicit_task_type_from_goal("Build a parser that validates task_type: document.") is None
    )
    for goal in (
        "Write unit tests asserting task_type: document.",
        "Create documentation showing how to set task_type: document.",
        "Build a linter that rejects task_type: document in configuration.",
        "Build a validator that warns when task_type: document is selected.",
    ):
        assert explicit_task_type_from_goal(goal) is None
    for goal in (
        "Use task_type: document. The report should say task_type: code.",
        "Use task_type: document. A test fixture contains task_type: code.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"
    assert (
        explicit_task_type_from_goal(
            "Inherit seed_old. task_type: document. Cancel the parent requirement."
        )
        == "document"
    )


def test_explicit_task_type_ignores_unrelated_bare_retraction_details() -> None:
    for goal in (
        "Use task_type: document. Never mind the earlier color choice.",
        "Use task_type: document. Scratch that old heading.",
    ):
        assert explicit_task_type_from_goal(goal) == "document"


def test_explicit_task_type_ignores_artifact_payload_fields() -> None:
    for goal in (
        "Generate a YAML example containing task_type: document.",
        "Return JSON with task_type: document.",
        "The generated manifest must set task_type: document.",
        "Build a CLI whose README says task_type: document.",
        "Implement a validator whose error says task_type: document.",
        "Generate a TOML file with task_type: document.",
        "Write tests where the expected string is task_type: document.",
        "The YAML file must set task_type: document.",
        "Write a README saying task_type: document.",
        "Add support for task_type: document in the Seed API.",
        "Update the parser to accept task_type: document.",
        "Add a test for task_type: document.",
        "Analyze why the API accepts task_type: document.",
        "Explain how the schema stores task_type: document.",
        "Rename task_type: document to artifact in the API.",
        "Refactor task_type: document handling.",
        "Deprecate task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_ignores_api_schema_and_parser_content() -> None:
    for goal in (
        "The API returns a Seed where task_type: document.",
        "The schema property task_type is document.",
        "Ensure the parser preserves strings where task_type: document.",
        "Persist task_type: document in the database.",
        "Store task_type: document in the session state.",
        "Expose task_type: document in the CLI output.",
        "The config field task_type: document controls rendering.",
        "Route task_type: document requests to artifact workers.",
        "Map PDFs to task_type: document in the routing table.",
        "Read task_type: document from the config.",
        "When the user asks for a document, set task_type: document in the generated Seed.",
        "When task_type: document, render Markdown output in the existing Python service.",
        "Handle task_type: document by rendering Markdown while keeping this Python CLI implementation.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_ignores_help_error_and_documentation_content() -> None:
    for goal in (
        "Add a CLI flag whose help text says task_type: document.",
        "Implement a validator whose error message says task_type: document.",
        "Create docs with the sentence task_type: document.",
    ):
        assert explicit_task_type_from_goal(goal) is None


def test_explicit_task_type_ignores_implementation_inheritance_prose() -> None:
    assert explicit_task_type_from_goal("Fix handling when users inherit seed_demo.") is None
