"""Unit tests for ouroboros.core.seed module.

Tests the immutable Seed schema and related types.
"""

from datetime import UTC, datetime
import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError
import pytest

from ouroboros.core.seed import (
    MAX_AC_SUCCESS_CONTRACT_ARTIFACT_CHARS,
    MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES,
    MAX_AC_SUCCESS_CONTRACT_ARTIFACTS,
    MAX_AC_SUCCESS_CONTRACT_CHARS,
    AcceptanceCriterionSpec,
    EvaluationPrinciple,
    ExitCondition,
    InvestmentSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_texts,
    expected_artifact_path_error,
    expected_artifact_workspace_path_error,
    parse_expected_artifact_list,
)


def _multibyte_artifact_at_byte_budget(*, overflow_bytes: int = 0) -> str:
    """Build a component-valid UTF-8 path at the total artifact budget."""
    path = "/".join(("😀" * 62, "a" * (6 + overflow_bytes)))
    assert len(path.encode("utf-8")) == MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES + overflow_bytes
    return path


class TestSeedMetadata:
    """Test SeedMetadata model."""

    def test_metadata_generates_seed_id(self) -> None:
        """SeedMetadata generates a unique seed_id if not provided."""
        metadata = SeedMetadata(ambiguity_score=0.15)

        assert metadata.seed_id.startswith("seed_")
        assert len(metadata.seed_id) > 5

    def test_metadata_with_explicit_seed_id(self) -> None:
        """SeedMetadata accepts explicit seed_id."""
        metadata = SeedMetadata(seed_id="seed_custom123", ambiguity_score=0.10)

        assert metadata.seed_id == "seed_custom123"

    def test_metadata_default_version(self) -> None:
        """SeedMetadata has default version 1.0.0."""
        metadata = SeedMetadata(ambiguity_score=0.15)

        assert metadata.version == "1.0.0"

    def test_metadata_generates_created_at(self) -> None:
        """SeedMetadata generates created_at timestamp."""
        before = datetime.now(UTC)
        metadata = SeedMetadata(ambiguity_score=0.15)
        after = datetime.now(UTC)

        assert before <= metadata.created_at <= after

    def test_metadata_stores_ambiguity_score(self) -> None:
        """SeedMetadata stores ambiguity_score."""
        metadata = SeedMetadata(ambiguity_score=0.18)

        assert metadata.ambiguity_score == 0.18

    def test_metadata_optional_interview_id(self) -> None:
        """SeedMetadata interview_id is optional."""
        metadata = SeedMetadata(ambiguity_score=0.15)
        assert metadata.interview_id is None

        metadata_with_id = SeedMetadata(
            ambiguity_score=0.15,
            interview_id="interview_123",
        )
        assert metadata_with_id.interview_id == "interview_123"

    def test_metadata_validates_ambiguity_score_range(self) -> None:
        """SeedMetadata validates ambiguity_score is between 0 and 1."""
        with pytest.raises(PydanticValidationError):
            SeedMetadata(ambiguity_score=-0.1)

        with pytest.raises(PydanticValidationError):
            SeedMetadata(ambiguity_score=1.5)

    def test_metadata_is_frozen(self) -> None:
        """SeedMetadata is immutable (frozen=True)."""
        metadata = SeedMetadata(ambiguity_score=0.15)

        with pytest.raises(PydanticValidationError):
            metadata.ambiguity_score = 0.20  # type: ignore[misc]


class TestOntologyField:
    """Test OntologyField model."""

    def test_ontology_field_required_attributes(self) -> None:
        """OntologyField has required name, field_type, description."""
        field = OntologyField(
            name="tasks",
            field_type="array",
            description="List of tasks",
        )

        assert field.name == "tasks"
        assert field.field_type == "array"
        assert field.description == "List of tasks"

    def test_ontology_field_default_required(self) -> None:
        """OntologyField defaults to required=True."""
        field = OntologyField(
            name="id",
            field_type="string",
            description="Task identifier",
        )

        assert field.required is True

    def test_ontology_field_explicit_required_false(self) -> None:
        """OntologyField can be set to required=False."""
        field = OntologyField(
            name="description",
            field_type="string",
            description="Optional description",
            required=False,
        )

        assert field.required is False

    def test_ontology_field_is_frozen(self) -> None:
        """OntologyField is immutable (frozen=True)."""
        field = OntologyField(
            name="tasks",
            field_type="array",
            description="List of tasks",
        )

        with pytest.raises(PydanticValidationError):
            field.name = "new_name"  # type: ignore[misc]


class TestOntologySchema:
    """Test OntologySchema model."""

    def test_ontology_schema_basic(self) -> None:
        """OntologySchema has name, description, and optional fields."""
        schema = OntologySchema(
            name="TaskManager",
            description="Task management domain model",
        )

        assert schema.name == "TaskManager"
        assert schema.description == "Task management domain model"
        assert schema.fields == ()

    def test_ontology_schema_with_fields(self) -> None:
        """OntologySchema can contain multiple fields."""
        fields = (
            OntologyField(name="id", field_type="string", description="Task ID"),
            OntologyField(name="title", field_type="string", description="Task title"),
            OntologyField(name="done", field_type="boolean", description="Completion status"),
        )

        schema = OntologySchema(
            name="Task",
            description="Task entity",
            fields=fields,
        )

        assert len(schema.fields) == 3
        assert schema.fields[0].name == "id"
        assert schema.fields[1].name == "title"
        assert schema.fields[2].name == "done"

    def test_ontology_schema_is_frozen(self) -> None:
        """OntologySchema is immutable (frozen=True)."""
        schema = OntologySchema(
            name="TaskManager",
            description="Task management domain model",
        )

        with pytest.raises(PydanticValidationError):
            schema.name = "NewName"  # type: ignore[misc]


class TestEvaluationPrinciple:
    """Test EvaluationPrinciple model."""

    def test_evaluation_principle_basic(self) -> None:
        """EvaluationPrinciple has name and description."""
        principle = EvaluationPrinciple(
            name="completeness",
            description="All requirements are implemented",
        )

        assert principle.name == "completeness"
        assert principle.description == "All requirements are implemented"

    def test_evaluation_principle_default_weight(self) -> None:
        """EvaluationPrinciple defaults to weight=1.0."""
        principle = EvaluationPrinciple(
            name="quality",
            description="Code meets quality standards",
        )

        assert principle.weight == 1.0

    def test_evaluation_principle_custom_weight(self) -> None:
        """EvaluationPrinciple accepts custom weight."""
        principle = EvaluationPrinciple(
            name="performance",
            description="Performance is acceptable",
            weight=0.5,
        )

        assert principle.weight == 0.5

    def test_evaluation_principle_weight_validation(self) -> None:
        """EvaluationPrinciple validates weight is between 0 and 1."""
        with pytest.raises(PydanticValidationError):
            EvaluationPrinciple(
                name="test",
                description="test",
                weight=-0.1,
            )

        with pytest.raises(PydanticValidationError):
            EvaluationPrinciple(
                name="test",
                description="test",
                weight=1.5,
            )

    def test_evaluation_principle_is_frozen(self) -> None:
        """EvaluationPrinciple is immutable (frozen=True)."""
        principle = EvaluationPrinciple(
            name="completeness",
            description="All requirements are implemented",
        )

        with pytest.raises(PydanticValidationError):
            principle.weight = 0.5  # type: ignore[misc]


class TestExitCondition:
    """Test ExitCondition model."""

    def test_exit_condition_required_fields(self) -> None:
        """ExitCondition has name, description, evaluation_criteria."""
        condition = ExitCondition(
            name="all_criteria_met",
            description="All acceptance criteria are satisfied",
            evaluation_criteria="100% criteria pass",
        )

        assert condition.name == "all_criteria_met"
        assert condition.description == "All acceptance criteria are satisfied"
        assert condition.evaluation_criteria == "100% criteria pass"

    def test_exit_condition_is_frozen(self) -> None:
        """ExitCondition is immutable (frozen=True)."""
        condition = ExitCondition(
            name="max_iterations",
            description="Maximum iterations reached",
            evaluation_criteria="iterations >= 10",
        )

        with pytest.raises(PydanticValidationError):
            condition.name = "new_name"  # type: ignore[misc]


class TestSeed:
    """Test Seed model - the main immutable specification."""

    @pytest.fixture
    def minimal_seed(self) -> Seed:
        """Create a minimal valid Seed for testing."""
        return Seed(
            goal="Build a CLI task manager",
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

    @pytest.fixture
    def full_seed(self) -> Seed:
        """Create a fully populated Seed for testing."""
        return Seed(
            goal="Build a CLI task management tool with project grouping",
            constraints=(
                "Python 3.14+",
                "No external database",
                "Single-file storage",
            ),
            acceptance_criteria=(
                "Tasks can be created",
                "Tasks can be listed",
                "Tasks can be marked complete",
                "Tasks can be grouped by project",
            ),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain model",
                fields=(
                    OntologyField(
                        name="tasks",
                        field_type="array",
                        description="List of task objects",
                    ),
                    OntologyField(
                        name="projects",
                        field_type="array",
                        description="List of project objects",
                    ),
                ),
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="completeness",
                    description="All requirements are implemented",
                    weight=0.4,
                ),
                EvaluationPrinciple(
                    name="usability",
                    description="CLI is intuitive",
                    weight=0.3,
                ),
            ),
            exit_conditions=(
                ExitCondition(
                    name="all_criteria_met",
                    description="All acceptance criteria pass",
                    evaluation_criteria="100% criteria satisfied",
                ),
            ),
            metadata=SeedMetadata(
                ambiguity_score=0.12,
                interview_id="interview_123",
            ),
        )

    def test_seed_minimal_required_fields(self, minimal_seed: Seed) -> None:
        """Seed requires goal, ontology_schema, and metadata."""
        assert minimal_seed.goal == "Build a CLI task manager"
        assert minimal_seed.ontology_schema.name == "TaskManager"
        assert minimal_seed.metadata.ambiguity_score == 0.15

    def test_seed_defaults_empty_collections(self, minimal_seed: Seed) -> None:
        """Seed defaults to empty tuples for optional collections."""
        assert minimal_seed.constraints == ()
        assert minimal_seed.acceptance_criteria == ()
        assert minimal_seed.evaluation_principles == ()
        assert minimal_seed.exit_conditions == ()

    def test_seed_full_population(self, full_seed: Seed) -> None:
        """Seed stores all provided fields correctly."""
        assert full_seed.goal.startswith("Build a CLI task management")
        assert len(full_seed.constraints) == 3
        assert len(full_seed.acceptance_criteria) == 4
        assert ac_texts(full_seed.acceptance_criteria) == (
            "Tasks can be created",
            "Tasks can be listed",
            "Tasks can be marked complete",
            "Tasks can be grouped by project",
        )
        assert len(full_seed.ontology_schema.fields) == 2
        assert len(full_seed.evaluation_principles) == 2
        assert len(full_seed.exit_conditions) == 1

    def test_seed_coerces_string_acceptance_criteria(self) -> None:
        """Legacy string ACs are lifted into structured specs."""
        seed = Seed(
            goal="Build a CLI task manager",
            acceptance_criteria=("Tasks can be created",),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        assert isinstance(seed.acceptance_criteria[0], AcceptanceCriterionSpec)
        assert seed.acceptance_criteria[0].description == "Tasks can be created"
        assert seed.acceptance_criteria[0].verify_command is None
        assert seed.acceptance_criteria[0].expected_artifacts == ()

    def test_seed_acceptance_criterion_spec_fields_are_optional(self) -> None:
        """Structured ACs can carry any subset of success-contract fields."""
        seed = Seed(
            goal="Build a CLI task manager",
            acceptance_criteria=(
                {
                    "description": "Task list command exits 0",
                    "verify_command": "uv run task list",
                    "expected_artifacts": "tasks.json, logs/task.log",
                    "output_assertion": "No tasks",
                },
                {"description": "Tasks can be created"},
            ),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        first, second = seed.acceptance_criteria
        assert first.verify_command == "uv run task list"
        assert first.expected_artifacts == ("tasks.json", "logs/task.log")
        assert first.output_assertion == "No tasks"
        assert second.description == "Tasks can be created"
        assert second.verify_command is None

    def test_expected_artifacts_schema_documents_literal_workspace_paths(self) -> None:
        field = AcceptanceCriterionSpec.model_fields["expected_artifacts"]

        assert field.description is not None
        assert "Exact file or directory paths" in field.description
        assert "relative to the run workspace" in field.description
        assert "resolved literally" in field.description
        assert "255 UTF-8 bytes" in field.description
        assert "complete canonical path at most 255" in field.description
        assert "prefix top-level paths containing spaces with ./" in field.description

    def test_expected_artifact_path_grammar_is_portable(self) -> None:
        for artifact in (
            ".",
            "./",
            "././",
            ".//",
            r".\.",
            ".\\",
            "bad\x00path",
            "../outside",
            r"D:outside",
            r"\outside.txt",
            r"\\server\share\outside.txt",
            r"docs\User Guide.md",
            "schema v2 outputs.json",
            "reports/summary,v2.json",
            "NUL",
            "nul.txt",
            "dir/CON",
            "COM1.log",
            "foo.",
            "docs/a:b",
            "docs/a?.txt",
            "a" * 256,
            "docs/" + ("\u00e9" * 128),
            "NONE",
            "none",
        ):
            assert expected_artifact_path_error(artifact) is not None

        for artifact in (
            "README.md",
            "docs",
            "docs/User Guide.md",
            "./Build Outputs",
            "é" * 127 + "a",
            _multibyte_artifact_at_byte_budget(),
            "./" + _multibyte_artifact_at_byte_budget(),
        ):
            assert expected_artifact_path_error(artifact) is None

    def test_expected_artifact_path_rejects_component_and_total_byte_overflow(self) -> None:
        component_overflow = "é" * 128
        total_overflow = _multibyte_artifact_at_byte_budget(overflow_bytes=1)

        assert len(component_overflow.encode("utf-8")) == 256
        assert "component longer than 255 filesystem bytes" in (
            expected_artifact_path_error(component_overflow) or ""
        )
        assert "canonical path longer than 255 portable filesystem bytes" in (
            expected_artifact_path_error(total_overflow) or ""
        )

    @pytest.mark.parametrize(
        ("platform", "workspace", "artifact", "capacity", "expected_error"),
        (
            ("posix", "/" + "w" * 900, "a" * 200, 1_024, "POSIX capacity"),
            ("windows", "C:\\" + "w" * 250, "out.txt", 260, "Windows capacity"),
        ),
    )
    def test_expected_artifact_workspace_capacity_fails_closed_by_platform(
        self,
        platform: str,
        workspace: str,
        artifact: str,
        capacity: int,
        expected_error: str,
    ) -> None:
        error = expected_artifact_workspace_path_error(
            artifact,
            workspace,
            platform=platform,  # type: ignore[arg-type]
            path_capacity=capacity,
        )

        assert expected_error in (error or "")

    def test_expected_artifact_list_uses_comma_whitespace_delimiter(self) -> None:
        assert parse_expected_artifact_list("tasks.json, logs/task.log, ./Build Outputs") == (
            "tasks.json",
            "logs/task.log",
            "./Build Outputs",
        )

    def test_success_contract_accepts_exact_capsule_artifact_limit(self) -> None:
        spec = AcceptanceCriterionSpec(
            description="bounded artifact contract",
            expected_artifacts=tuple(
                f"out/artifact-{index}.txt" for index in range(MAX_AC_SUCCESS_CONTRACT_ARTIFACTS)
            ),
        )

        assert len(spec.expected_artifacts) == MAX_AC_SUCCESS_CONTRACT_ARTIFACTS

    def test_success_contract_rejects_artifacts_beyond_capsule_limit(self) -> None:
        with pytest.raises(PydanticValidationError, match="artifact limit exceeded"):
            AcceptanceCriterionSpec(
                description="oversized artifact contract",
                expected_artifacts=tuple(
                    f"out/artifact-{index}.txt"
                    for index in range(MAX_AC_SUCCESS_CONTRACT_ARTIFACTS + 1)
                ),
            )

    def test_success_contract_rejects_portable_path_overflow_before_capsule_locator(self) -> None:
        artifact = "/".join(
            "a" * 250 for _ in range(MAX_AC_SUCCESS_CONTRACT_ARTIFACT_CHARS // 251 + 1)
        )
        assert len(artifact) > MAX_AC_SUCCESS_CONTRACT_ARTIFACT_CHARS

        with pytest.raises(
            PydanticValidationError,
            match="canonical path longer than 255 portable filesystem bytes",
        ):
            AcceptanceCriterionSpec(
                description="oversized artifact locator",
                expected_artifacts=(artifact,),
            )

    def test_success_contract_accepts_exact_multibyte_artifact_byte_budget(self) -> None:
        artifact = _multibyte_artifact_at_byte_budget()

        spec = AcceptanceCriterionSpec(
            description="maximum encoded artifact locator",
            expected_artifacts=(artifact,),
        )

        assert spec.expected_artifacts == (artifact,)

    def test_success_contract_rejects_multibyte_artifact_byte_overflow(self) -> None:
        artifact = _multibyte_artifact_at_byte_budget(overflow_bytes=1)

        with pytest.raises(
            PydanticValidationError,
            match="canonical path longer than 255 portable filesystem bytes",
        ):
            AcceptanceCriterionSpec(
                description="oversized encoded artifact locator",
                expected_artifacts=(artifact,),
            )

    def test_success_contract_rejects_total_capsule_character_overflow(self) -> None:
        with pytest.raises(PydanticValidationError, match="character budget exceeded"):
            AcceptanceCriterionSpec(
                description="oversized contract",
                verify_command="x" * (MAX_AC_SUCCESS_CONTRACT_CHARS + 1),
            )

    @pytest.mark.parametrize(
        "artifact",
        (
            ".",
            "../outside",
            r"D:outside",
            r"\\server\share\outside.txt",
            "schema v2 outputs.json",
            "reports/summary,v2.json",
            "NUL",
            "nul.txt",
            "dir/CON",
            "foo.",
            "docs/a:b",
            r"docs\User Guide.md",
        ),
    )
    def test_expected_artifacts_reject_nonportable_paths_at_schema_ingress(
        self,
        artifact: str,
    ) -> None:
        with pytest.raises(PydanticValidationError, match="portable workspace-relative paths"):
            AcceptanceCriterionSpec(
                description="artifact contract",
                expected_artifacts=(artifact,),
            )

    @pytest.mark.parametrize(
        "artifacts",
        [
            ("",),
            ("report.txt\n",),
            ("report.txt\t",),
            ("report.txt\x7f",),
            ("reports/summary,v2.json",),
            (r"docs\User Guide.md",),
            (None,),
            ("NONE", "report.txt"),
            "report.txt\n",
            "reports/summary,v2.json",
            "NONE, report.txt",
            "report.txt, NONE",
        ],
    )
    def test_expected_artifacts_reject_malformed_raw_entries(self, artifacts: object) -> None:
        with pytest.raises(PydanticValidationError):
            AcceptanceCriterionSpec(
                description="artifact contract",
                expected_artifacts=artifacts,  # type: ignore[arg-type]
            )

    def test_output_assertion_requires_verify_command_at_schema_ingress(self) -> None:
        with pytest.raises(PydanticValidationError, match="requires verify_command"):
            AcceptanceCriterionSpec(
                description="Command output contains READY",
                output_assertion="READY",
            )

    def test_output_assertion_condition_phrase_requires_raw_verify_command(self) -> None:
        with pytest.raises(PydanticValidationError, match="requires verify_command"):
            AcceptanceCriterionSpec(
                description="Command exits successfully",
                output_assertion="success",
            )

        with pytest.raises(PydanticValidationError, match="requires verify_command"):
            AcceptanceCriterionSpec(
                description="Command exits successfully",
                verify_command="NONE",
                output_assertion="exit code 0",
            )

    def test_output_assertion_schema_documents_combined_command_output(self) -> None:
        field = AcceptanceCriterionSpec.model_fields["output_assertion"]

        assert field.description is not None
        assert "combined stdout and stderr" in field.description

    def test_output_assertion_normalizes_exit_status_conditions_to_none(self) -> None:
        """Exit-code success phrases are redundant with verify_command exit status."""
        for phrase in (
            "exit code 0",
            "exit status 0",
            "exit 0",
            "returns 0",
            "return 0",
            "success",
            "succeeds",
            "passed",
            "passes",
            "ok exit",
            "no error",
            "no errors",
        ):
            spec = AcceptanceCriterionSpec(
                description="Command exits successfully",
                verify_command="python -c 'pass'",
                output_assertion=phrase,
            )
            assert spec.output_assertion is None
            assert spec.to_seed_value() == {
                "description": "Command exits successfully",
                "verify_command": "python -c 'pass'",
            }

    def test_output_assertion_preserves_distinctive_stdout_literals(self) -> None:
        """Real stdout literals survive normalization."""
        ok = AcceptanceCriterionSpec(
            description="Command prints OK",
            verify_command="python -c \"print('OK')\"",
            output_assertion="OK",
        )
        pytest_literal = AcceptanceCriterionSpec(
            description="Pytest summary is asserted",
            verify_command="pytest -q",
            output_assertion="5 passed",
        )

        assert ok.output_assertion == "OK"
        assert pytest_literal.output_assertion == "5 passed"

    def test_seed_acceptance_criteria_materialize_semantic_keys(self) -> None:
        """Legacy description-only ACs gain deterministic persisted identities."""
        seed = Seed(
            goal="Build a CLI task manager",
            acceptance_criteria=(
                "Tasks can be created",
                AcceptanceCriterionSpec(description="Tasks can be listed"),
            ),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        serialized = seed.to_dict()["acceptance_criteria"]
        assert [item["description"] for item in serialized] == [
            "Tasks can be created",
            "Tasks can be listed",
        ]
        assert all(item["semantic_ac_key"].startswith("ac_") for item in serialized)
        assert Seed.from_dict(seed.to_dict()).to_dict() == seed.to_dict()
        assert all(criterion.investment is None for criterion in seed.acceptance_criteria)

    def test_seed_acceptance_criteria_serialize_optional_investment_metadata(self) -> None:
        """Investment metadata is additive and keeps authority explicit."""
        seed = Seed(
            goal="Build a CLI task manager",
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Rotate production payment signing keys",
                    investment=InvestmentSpec(
                        difficulty="medium",
                        stakes="high",
                        provenance="declared",
                        confidence="high",
                    ),
                ),
            ),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        criterion = seed.acceptance_criteria[0]
        assert criterion.investment is not None
        assert criterion.investment.stakes == "high"
        serialized = seed.to_dict()["acceptance_criteria"][0]
        assert serialized["description"] == "Rotate production payment signing keys"
        assert serialized["semantic_ac_key"].startswith("ac_")
        assert serialized["investment"] == {
            "confidence": "high",
            "difficulty": "medium",
            "provenance": "declared",
            "stakes": "high",
        }

    def test_investment_defaults_cannot_authorize_cheaper_execution(self) -> None:
        """Omitted confidence stays low rather than silently becoming trusted."""
        spec = InvestmentSpec(difficulty="low", stakes="low")

        assert spec.provenance == "declared"
        assert spec.confidence == "low"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("difficulty", "trivial"),
            ("stakes", "critical"),
            ("provenance", "guessed"),
            ("confidence", "certain"),
        ],
    )
    def test_investment_rejects_unbounded_vocabulary(self, field: str, value: str) -> None:
        data = {
            "difficulty": "low",
            "stakes": "low",
            "provenance": "declared",
            "confidence": "high",
            field: value,
        }

        with pytest.raises(PydanticValidationError):
            InvestmentSpec.model_validate(data)

    def test_seed_acceptance_criteria_serialize_structured_contract(self) -> None:
        """Contract-bearing ACs dump only populated fields."""
        seed = Seed(
            goal="Build a CLI task manager",
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Task list command exits 0",
                    verify_command="uv run task list",
                    expected_artifacts=("tasks.json",),
                ),
            ),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        assert seed.to_dict()["acceptance_criteria"] == [
            {
                "description": "Task list command exits 0",
                "expected_artifacts": ["tasks.json"],
                "semantic_ac_key": seed.acceptance_criteria[0].semantic_ac_key,
                "verify_command": "uv run task list",
            }
        ]

    def test_legacy_seed_json_materializes_semantic_keys_once(self) -> None:
        """An old bare-string fixture upgrades once and is then stable."""
        fixture = Path(__file__).parents[2] / "fixtures" / "seeds" / "legacy-seed-minimal.json"
        original = fixture.read_text()
        seed = Seed.from_dict(json.loads(original))

        round_tripped = json.dumps(seed.to_dict(), indent=2, sort_keys=True) + "\n"

        assert round_tripped != original
        assert all(
            item["semantic_ac_key"].startswith("ac_")
            for item in seed.to_dict()["acceptance_criteria"]
        )
        assert Seed.from_dict(json.loads(round_tripped)).to_dict() == seed.to_dict()

    def test_seed_coerces_string_evaluation_principles(self) -> None:
        """Hand-written seed principle string lists are lifted into objects."""
        seed = Seed(
            goal="Build a CLI task manager",
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            evaluation_principles=("Prefer simple code", "Keep UX clear"),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        assert seed.evaluation_principles[0].name == "principle_1"
        assert seed.evaluation_principles[0].description == "Prefer simple code"
        assert seed.evaluation_principles[0].weight == 1.0
        assert seed.evaluation_principles[1].name == "principle_2"

    def test_seed_coerces_string_exit_conditions(self) -> None:
        """Hand-written seed exit condition string lists are lifted into objects."""
        seed = Seed(
            goal="Build a CLI task manager",
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
            ),
            exit_conditions=("All invariant tests are green",),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        assert seed.exit_conditions[0].name == "condition_1"
        assert seed.exit_conditions[0].description == "All invariant tests are green"
        assert seed.exit_conditions[0].evaluation_criteria == "All invariant tests are green"

    def test_seed_goal_immutability(self, minimal_seed: Seed) -> None:
        """Seed.goal cannot be modified (frozen=True raises error)."""
        with pytest.raises(PydanticValidationError):
            minimal_seed.goal = "New goal"  # type: ignore[misc]

    def test_seed_constraints_immutability(self, full_seed: Seed) -> None:
        """Seed.constraints cannot be modified."""
        with pytest.raises(PydanticValidationError):
            full_seed.constraints = ("new constraint",)  # type: ignore[misc]

    def test_seed_acceptance_criteria_immutability(self, full_seed: Seed) -> None:
        """Seed.acceptance_criteria cannot be modified."""
        with pytest.raises(PydanticValidationError):
            full_seed.acceptance_criteria = ("new criterion",)  # type: ignore[misc]

    def test_seed_ontology_schema_immutability(self, minimal_seed: Seed) -> None:
        """Seed.ontology_schema cannot be modified."""
        new_schema = OntologySchema(name="New", description="New schema")
        with pytest.raises(PydanticValidationError):
            minimal_seed.ontology_schema = new_schema  # type: ignore[misc]

    def test_seed_evaluation_principles_immutability(self, full_seed: Seed) -> None:
        """Seed.evaluation_principles cannot be modified."""
        with pytest.raises(PydanticValidationError):
            full_seed.evaluation_principles = ()  # type: ignore[misc]

    def test_seed_exit_conditions_immutability(self, full_seed: Seed) -> None:
        """Seed.exit_conditions cannot be modified."""
        with pytest.raises(PydanticValidationError):
            full_seed.exit_conditions = ()  # type: ignore[misc]

    def test_seed_metadata_immutability(self, minimal_seed: Seed) -> None:
        """Seed.metadata cannot be modified."""
        new_metadata = SeedMetadata(ambiguity_score=0.05)
        with pytest.raises(PydanticValidationError):
            minimal_seed.metadata = new_metadata  # type: ignore[misc]

    def test_seed_to_dict(self, full_seed: Seed) -> None:
        """Seed.to_dict() returns serializable dictionary."""
        seed_dict = full_seed.to_dict()

        assert isinstance(seed_dict, dict)
        assert seed_dict["goal"] == full_seed.goal
        assert seed_dict["constraints"] == list(full_seed.constraints)
        assert [item["description"] for item in seed_dict["acceptance_criteria"]] == list(
            ac_texts(full_seed.acceptance_criteria)
        )
        assert all(
            item["semantic_ac_key"] == criterion.semantic_ac_key
            for item, criterion in zip(
                seed_dict["acceptance_criteria"],
                full_seed.acceptance_criteria,
                strict=True,
            )
        )
        assert seed_dict["ontology_schema"]["name"] == full_seed.ontology_schema.name
        assert seed_dict["metadata"]["ambiguity_score"] == full_seed.metadata.ambiguity_score

    def test_seed_from_dict(self, full_seed: Seed) -> None:
        """Seed.from_dict() creates Seed from dictionary."""
        seed_dict = full_seed.to_dict()

        reconstructed = Seed.from_dict(seed_dict)

        assert reconstructed.goal == full_seed.goal
        assert reconstructed.constraints == full_seed.constraints
        assert reconstructed.acceptance_criteria == full_seed.acceptance_criteria
        assert reconstructed.ontology_schema.name == full_seed.ontology_schema.name
        assert reconstructed.metadata.ambiguity_score == full_seed.metadata.ambiguity_score

    def test_seed_from_dict_coerces_string_principles_and_conditions(self) -> None:
        """Seed.from_dict() accepts prose lists from hand-written seed YAML."""
        reconstructed = Seed.from_dict(
            {
                "goal": "Build a CLI task manager",
                "ontology_schema": {
                    "name": "TaskManager",
                    "description": "Task management domain",
                },
                "evaluation_principles": ["Prefer simple code"],
                "exit_conditions": ["All invariant tests are green"],
                "metadata": {"ambiguity_score": 0.15},
            }
        )

        assert reconstructed.evaluation_principles[0].name == "principle_1"
        assert reconstructed.evaluation_principles[0].description == "Prefer simple code"
        assert reconstructed.evaluation_principles[0].weight == 1.0
        assert reconstructed.exit_conditions[0].name == "condition_1"
        assert reconstructed.exit_conditions[0].description == "All invariant tests are green"
        assert (
            reconstructed.exit_conditions[0].evaluation_criteria == "All invariant tests are green"
        )

    def test_seed_roundtrip_serialization(self, full_seed: Seed) -> None:
        """Seed can roundtrip through dict serialization."""
        seed_dict = full_seed.to_dict()
        reconstructed = Seed.from_dict(seed_dict)
        second_dict = reconstructed.to_dict()

        assert seed_dict == second_dict

    def test_seed_roundtrip_preserves_plugin_extra_fields(self, full_seed: Seed) -> None:
        """Seed preserves structured plugin-owned fields without core-specific hooks."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {
            "plugin": "example",
            "handoff_version": 1,
            "candidate_sequence": [{"name": "baseline"}],
        }

        reconstructed = Seed.from_dict(seed_dict)

        assert reconstructed.to_dict()["plugin_contract"] == seed_dict["plugin_contract"]

    def test_seed_plugin_extra_fields_are_deeply_immutable(self, full_seed: Seed) -> None:
        """Seed extras cannot drift through nested container mutation."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {
            "plugin": "example",
            "candidate_sequence": [{"name": "baseline"}],
        }
        reconstructed = Seed.from_dict(seed_dict)

        with pytest.raises(TypeError):
            reconstructed.plugin_contract["plugin"] = "mutated"
        with pytest.raises(TypeError):
            reconstructed.plugin_contract["candidate_sequence"][0]["name"] = "mutated"

        assert reconstructed.to_dict()["plugin_contract"] == seed_dict["plugin_contract"]

    def test_seed_model_extra_mapping_is_immutable(self, full_seed: Seed) -> None:
        """Seed.model_extra cannot bypass plugin extra validation."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {"plugin": "example"}
        reconstructed = Seed.from_dict(seed_dict)

        with pytest.raises(TypeError):
            reconstructed.model_extra["plugin_contract"] = {"plugin": "mutated"}  # type: ignore[index]
        with pytest.raises(TypeError):
            reconstructed.model_extra["bad"] = object()  # type: ignore[index]

        assert reconstructed.to_dict()["plugin_contract"] == seed_dict["plugin_contract"]
        assert "bad" not in reconstructed.to_dict()

    def test_seed_model_copy_preserves_immutable_plugin_extra_fields(self, full_seed: Seed) -> None:
        """Pydantic model_copy works after plugin extras are frozen."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {"plugin": "example"}
        reconstructed = Seed.from_dict(seed_dict)

        copied = reconstructed.model_copy(
            update={"acceptance_criteria": ("Updated observable criterion.",)}
        )

        assert copied.acceptance_criteria == ("Updated observable criterion.",)
        assert copied.to_dict()["plugin_contract"] == {"plugin": "example"}
        with pytest.raises(TypeError):
            copied.model_extra["plugin_contract"] = {"plugin": "mutated"}  # type: ignore[index]

    def test_seed_plugin_extra_fields_use_standard_pydantic_serialization(
        self, full_seed: Seed
    ) -> None:
        """Seed extras remain safe through the public Pydantic JSON API."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {
            "plugin": "example",
            "candidate_sequence": [{"name": "baseline"}],
        }
        reconstructed = Seed.from_dict(seed_dict)

        assert (
            reconstructed.model_dump(mode="json")["plugin_contract"] == seed_dict["plugin_contract"]
        )
        assert '"plugin_contract"' in reconstructed.model_dump_json()

    def test_seed_rejects_non_serializable_plugin_extra_fields(self, full_seed: Seed) -> None:
        """Seed extras fail early when plugin data cannot be persisted."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {"callback": object()}

        with pytest.raises(PydanticValidationError, match="JSON/YAML-serializable"):
            Seed.from_dict(seed_dict)

    def test_seed_rejects_non_finite_plugin_extra_float(self, full_seed: Seed) -> None:
        """Seed extras cannot contain floats that are invalid JSON values."""
        seed_dict = full_seed.to_dict()
        seed_dict["plugin_contract"] = {"score": float("nan")}

        with pytest.raises(PydanticValidationError, match="finite float"):
            Seed.from_dict(seed_dict)

    def test_seed_validation_empty_goal(self) -> None:
        """Seed validates goal is not empty."""
        with pytest.raises(PydanticValidationError):
            Seed(
                goal="",  # Empty goal should fail
                ontology_schema=OntologySchema(
                    name="Test",
                    description="Test",
                ),
                metadata=SeedMetadata(ambiguity_score=0.1),
            )


class TestSeedImmutabilityComprehensive:
    """Comprehensive immutability tests per AC-5."""

    def test_all_seed_fields_are_frozen(self) -> None:
        """Verify every field on Seed raises error on modification attempt."""
        seed = Seed(
            goal="Test goal",
            constraints=("constraint1",),
            acceptance_criteria=("criterion1",),
            ontology_schema=OntologySchema(
                name="Test",
                description="Test schema",
                fields=(
                    OntologyField(
                        name="field1",
                        field_type="string",
                        description="A field",
                    ),
                ),
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="principle1",
                    description="A principle",
                ),
            ),
            exit_conditions=(
                ExitCondition(
                    name="condition1",
                    description="A condition",
                    evaluation_criteria="criteria",
                ),
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

        # Test each field
        fields_to_test = [
            ("goal", "new goal"),
            ("constraints", ("new",)),
            ("acceptance_criteria", ("new",)),
            ("ontology_schema", OntologySchema(name="New", description="New")),
            ("evaluation_principles", ()),
            ("exit_conditions", ()),
            ("metadata", SeedMetadata(ambiguity_score=0.2)),
        ]

        for field_name, new_value in fields_to_test:
            with pytest.raises(PydanticValidationError):
                setattr(seed, field_name, new_value)

    def test_nested_model_immutability(self) -> None:
        """Verify nested models within Seed are also frozen."""
        seed = Seed(
            goal="Test goal",
            ontology_schema=OntologySchema(
                name="Test",
                description="Test schema",
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="principle1",
                    description="A principle",
                ),
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

        # Try to modify nested ontology_schema
        with pytest.raises(PydanticValidationError):
            seed.ontology_schema.name = "Modified"  # type: ignore[misc]

        # Try to modify nested metadata
        with pytest.raises(PydanticValidationError):
            seed.metadata.ambiguity_score = 0.5  # type: ignore[misc]

        # Try to modify nested evaluation principle
        if seed.evaluation_principles:
            with pytest.raises(PydanticValidationError):
                seed.evaluation_principles[0].weight = 0.9  # type: ignore[misc]

    def test_tuple_immutability_for_collections(self) -> None:
        """Verify that tuples are used for collections, preventing in-place mutation."""
        seed = Seed(
            goal="Test goal",
            constraints=("constraint1", "constraint2"),
            ontology_schema=OntologySchema(
                name="Test",
                description="Test schema",
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

        # Tuples don't have append/extend methods
        assert isinstance(seed.constraints, tuple)
        assert not hasattr(seed.constraints, "append")
        assert not hasattr(seed.constraints, "extend")
