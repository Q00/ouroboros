"""Unit tests for ouroboros.core.seed module.

Tests the immutable Seed schema and related types.
"""

from datetime import UTC, datetime

from pydantic import ValidationError as PydanticValidationError
import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


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
        assert len(full_seed.ontology_schema.fields) == 2
        assert len(full_seed.evaluation_principles) == 2
        assert len(full_seed.exit_conditions) == 1

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
        assert seed_dict["acceptance_criteria"] == list(full_seed.acceptance_criteria)
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

    def test_seed_roundtrip_serialization(self, full_seed: Seed) -> None:
        """Seed can roundtrip through dict serialization."""
        seed_dict = full_seed.to_dict()
        reconstructed = Seed.from_dict(seed_dict)
        second_dict = reconstructed.to_dict()

        assert seed_dict == second_dict

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
