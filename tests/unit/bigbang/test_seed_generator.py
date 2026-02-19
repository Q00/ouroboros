"""Unit tests for ouroboros.bigbang.seed_generator module."""

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

import pytest
import yaml

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.seed_generator import (
    SeedGenerator,
    load_seed,
    save_seed_sync,
)
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, UsageInfo


def create_mock_completion_response(
    content: str,
    model: str = "openrouter/google/gemini-2.0-flash-001",
) -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model=model,
        usage=UsageInfo(prompt_tokens=200, completion_tokens=100, total_tokens=300),
        finish_reason="stop",
    )


def create_valid_extraction_response(
    goal: str = "Build a CLI task manager with project grouping",
    constraints: str = "Python 3.14+ | No external database | Single-file storage",
    acceptance_criteria: str = "Tasks can be created | Tasks can be listed | Tasks can be deleted",
    ontology_name: str = "TaskManager",
    ontology_description: str = "Task management domain model",
    ontology_fields: str = "tasks:array:List of task objects | projects:array:List of project objects",
    evaluation_principles: str = "completeness:All requirements implemented:0.4 | quality:Code meets standards:0.3",
    exit_conditions: str = "all_criteria_met:All acceptance criteria pass:100% criteria satisfied",
) -> str:
    """Create a valid LLM extraction response string."""
    return f"""GOAL: {goal}
CONSTRAINTS: {constraints}
ACCEPTANCE_CRITERIA: {acceptance_criteria}
ONTOLOGY_NAME: {ontology_name}
ONTOLOGY_DESCRIPTION: {ontology_description}
ONTOLOGY_FIELDS: {ontology_fields}
EVALUATION_PRINCIPLES: {evaluation_principles}
EXIT_CONDITIONS: {exit_conditions}"""


def create_interview_state_with_rounds(
    interview_id: str = "test_001",
    initial_context: str = "Build a CLI tool for task management",
    rounds: int = 3,
) -> InterviewState:
    """Create an interview state with specified number of rounds."""
    state = InterviewState(
        interview_id=interview_id,
        initial_context=initial_context,
    )
    for i in range(rounds):
        state.rounds.append(
            InterviewRound(
                round_number=i + 1,
                question=f"Question {i + 1}?",
                user_response=f"Answer {i + 1}",
            )
        )
    return state


def create_low_ambiguity_score(score: float = 0.15) -> AmbiguityScore:
    """Create an AmbiguityScore below the threshold (ready for seed)."""
    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="Goal Clarity",
            clarity_score=0.9,
            weight=0.4,
            justification="Goal is well-defined.",
        ),
        constraint_clarity=ComponentScore(
            name="Constraint Clarity",
            clarity_score=0.85,
            weight=0.3,
            justification="Constraints are clear.",
        ),
        success_criteria_clarity=ComponentScore(
            name="Success Criteria Clarity",
            clarity_score=0.85,
            weight=0.3,
            justification="Success criteria are measurable.",
        ),
    )
    return AmbiguityScore(overall_score=score, breakdown=breakdown)


def create_high_ambiguity_score(score: float = 0.45) -> AmbiguityScore:
    """Create an AmbiguityScore above the threshold (not ready for seed)."""
    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="Goal Clarity",
            clarity_score=0.5,
            weight=0.4,
            justification="Goal is vague.",
        ),
        constraint_clarity=ComponentScore(
            name="Constraint Clarity",
            clarity_score=0.6,
            weight=0.3,
            justification="Constraints need clarification.",
        ),
        success_criteria_clarity=ComponentScore(
            name="Success Criteria Clarity",
            clarity_score=0.55,
            weight=0.3,
            justification="Success criteria not measurable.",
        ),
    )
    return AmbiguityScore(overall_score=score, breakdown=breakdown)


class TestSeedGeneratorConstruction:
    """Test SeedGenerator construction and initialization."""

    def test_seed_generator_creates_output_dir(self) -> None:
        """SeedGenerator creates output directory on initialization."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "seeds"
            mock_adapter = AsyncMock()

            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=output_dir,
            )

            assert output_dir.exists()
            assert generator.output_dir == output_dir

    def test_seed_generator_default_settings(self) -> None:
        """SeedGenerator has reasonable default settings."""
        mock_adapter = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            assert generator.model == "openrouter/google/gemini-2.0-flash-001"
            assert generator.temperature == 0.2
            assert generator.max_tokens == 4096


class TestSeedGeneratorAmbiguityGating:
    """Test that SeedGenerator gates on ambiguity score."""

    @pytest.mark.asyncio
    async def test_generate_fails_when_ambiguity_too_high(self) -> None:
        """SeedGenerator.generate() returns error when ambiguity > threshold."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        high_ambiguity = create_high_ambiguity_score(0.45)

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, high_ambiguity)

            assert result.is_err
            assert isinstance(result.error, ValidationError)
            assert "exceeds threshold" in result.error.message
            assert result.error.value == 0.45

    @pytest.mark.asyncio
    async def test_generate_fails_at_exact_threshold(self) -> None:
        """SeedGenerator.generate() fails at exact threshold (> not >=)."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        # Score exactly at threshold is allowed (score <= 0.2)
        at_threshold = create_low_ambiguity_score(AMBIGUITY_THRESHOLD)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, at_threshold)

            # At threshold (0.2) should succeed
            assert result.is_ok

    @pytest.mark.asyncio
    async def test_generate_fails_above_threshold(self) -> None:
        """SeedGenerator.generate() fails when slightly above threshold."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        above_threshold = create_high_ambiguity_score(AMBIGUITY_THRESHOLD + 0.01)

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, above_threshold)

            assert result.is_err

    @pytest.mark.asyncio
    async def test_generate_succeeds_when_ambiguity_low(self) -> None:
        """SeedGenerator.generate() succeeds when ambiguity <= threshold."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score(0.15)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert isinstance(result.value, Seed)


class TestSeedGeneratorExtraction:
    """Test SeedGenerator requirement extraction."""

    @pytest.mark.asyncio
    async def test_generate_extracts_goal(self) -> None:
        """SeedGenerator extracts goal from interview."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            goal="Build a task management CLI tool"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.goal == "Build a task management CLI tool"

    @pytest.mark.asyncio
    async def test_generate_extracts_constraints(self) -> None:
        """SeedGenerator extracts constraints as tuple."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            constraints="Python 3.14+ | No external database | Must be cross-platform"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert len(result.value.constraints) == 3
            assert "Python 3.14+" in result.value.constraints
            assert "No external database" in result.value.constraints

    @pytest.mark.asyncio
    async def test_generate_extracts_acceptance_criteria(self) -> None:
        """SeedGenerator extracts acceptance_criteria as tuple."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            acceptance_criteria="Tasks can be created | Tasks can be listed"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert len(result.value.acceptance_criteria) == 2

    @pytest.mark.asyncio
    async def test_generate_extracts_ontology_schema(self) -> None:
        """SeedGenerator extracts ontology schema with fields."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            ontology_name="TaskManager",
            ontology_description="Domain model for task management",
            ontology_fields="tasks:array:List of tasks | status:string:Task status",
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.ontology_schema.name == "TaskManager"
            assert len(result.value.ontology_schema.fields) == 2

    @pytest.mark.asyncio
    async def test_generate_extracts_evaluation_principles(self) -> None:
        """SeedGenerator extracts evaluation principles with weights."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            evaluation_principles="completeness:All requirements met:0.5 | quality:High quality:0.3"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert len(result.value.evaluation_principles) == 2
            assert result.value.evaluation_principles[0].name == "completeness"
            assert result.value.evaluation_principles[0].weight == 0.5

    @pytest.mark.asyncio
    async def test_generate_extracts_exit_conditions(self) -> None:
        """SeedGenerator extracts exit conditions."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            exit_conditions="done:All done:100% pass | timeout:Max time:10 iterations"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert len(result.value.exit_conditions) == 2


class TestSeedGeneratorMetadata:
    """Test SeedGenerator metadata handling."""

    @pytest.mark.asyncio
    async def test_generate_includes_metadata(self) -> None:
        """Generated Seed includes proper metadata."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds(interview_id="interview_123")
        low_ambiguity = create_low_ambiguity_score(0.18)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            seed = result.value
            assert seed.metadata.ambiguity_score == 0.18
            assert seed.metadata.interview_id == "interview_123"
            assert seed.metadata.version == "1.0.0"
            assert seed.metadata.seed_id.startswith("seed_")
            assert seed.metadata.created_at is not None


class TestSeedGeneratorErrorHandling:
    """Test SeedGenerator error handling."""

    @pytest.mark.asyncio
    async def test_generate_handles_llm_error(self) -> None:
        """SeedGenerator returns error when LLM call fails."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        mock_adapter.complete = AsyncMock(
            return_value=Result.err(ProviderError("Rate limit exceeded"))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_err
            assert isinstance(result.error, ProviderError)

    @pytest.mark.asyncio
    async def test_generate_handles_malformed_response(self) -> None:
        """SeedGenerator returns error for malformed LLM response."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        # Missing required fields
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response("INVALID: missing fields"))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_err


class TestSeedGeneratorSaveAndLoad:
    """Test SeedGenerator save_seed and load_seed functions."""

    @pytest.fixture
    def sample_seed(self) -> Seed:
        """Create a sample seed for testing."""
        return Seed(
            goal="Build a task manager",
            constraints=("Python 3.14+", "No database"),
            acceptance_criteria=("Tasks can be created", "Tasks can be listed"),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management domain",
                fields=(
                    OntologyField(
                        name="tasks",
                        field_type="array",
                        description="List of tasks",
                    ),
                ),
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="completeness",
                    description="All requirements met",
                ),
            ),
            exit_conditions=(
                ExitCondition(
                    name="done",
                    description="All done",
                    evaluation_criteria="100% pass",
                ),
            ),
            metadata=SeedMetadata(
                seed_id="seed_test123",
                ambiguity_score=0.15,
                interview_id="interview_123",
            ),
        )

    @pytest.mark.asyncio
    async def test_save_seed_creates_yaml_file(self, sample_seed: Seed) -> None:
        """SeedGenerator.save_seed() creates YAML file."""
        mock_adapter = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            file_path = Path(tmp_dir) / "test_seed.yaml"
            result = await generator.save_seed(sample_seed, file_path)

            assert result.is_ok
            assert result.value == file_path
            assert file_path.exists()

    @pytest.mark.asyncio
    async def test_save_seed_content_is_valid_yaml(self, sample_seed: Seed) -> None:
        """Saved seed file contains valid YAML."""
        mock_adapter = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            file_path = Path(tmp_dir) / "test_seed.yaml"
            await generator.save_seed(sample_seed, file_path)

            content = file_path.read_text()
            data = yaml.safe_load(content)

            assert data["goal"] == sample_seed.goal
            assert data["constraints"] == list(sample_seed.constraints)

    @pytest.mark.asyncio
    async def test_save_seed_default_path(self, sample_seed: Seed) -> None:
        """SeedGenerator.save_seed() uses default path if not specified."""
        mock_adapter = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "seeds"
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=output_dir,
            )

            result = await generator.save_seed(sample_seed)

            assert result.is_ok
            expected_path = output_dir / f"{sample_seed.metadata.seed_id}.yaml"
            assert result.value == expected_path
            assert expected_path.exists()

    @pytest.mark.asyncio
    async def test_load_seed_from_yaml(self, sample_seed: Seed) -> None:
        """load_seed() loads seed from YAML file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "test_seed.yaml"

            # Save the seed first
            content = yaml.dump(sample_seed.to_dict(), default_flow_style=False)
            file_path.write_text(content)

            # Load it back
            result = await load_seed(file_path)

            assert result.is_ok
            loaded_seed = result.value
            assert loaded_seed.goal == sample_seed.goal
            assert loaded_seed.constraints == sample_seed.constraints
            assert loaded_seed.metadata.seed_id == sample_seed.metadata.seed_id

    @pytest.mark.asyncio
    async def test_load_seed_file_not_found(self) -> None:
        """load_seed() returns error for non-existent file."""
        result = await load_seed(Path("/non/existent/path.yaml"))

        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "not found" in result.error.message

    @pytest.mark.asyncio
    async def test_load_seed_invalid_yaml(self) -> None:
        """load_seed() returns error for invalid YAML."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "invalid.yaml"
            file_path.write_text("{ invalid yaml: [")

            result = await load_seed(file_path)

            assert result.is_err

    def test_save_seed_sync(self, sample_seed: Seed) -> None:
        """save_seed_sync() saves seed synchronously."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "test_seed.yaml"

            result = save_seed_sync(sample_seed, file_path)

            assert result.is_ok
            assert file_path.exists()

            # Verify content
            content = file_path.read_text()
            data = yaml.safe_load(content)
            assert data["goal"] == sample_seed.goal


class TestSeedGeneratorRoundtrip:
    """Test complete roundtrip: generate -> save -> load."""

    @pytest.mark.asyncio
    async def test_full_roundtrip(self) -> None:
        """Seed survives complete generate -> save -> load roundtrip."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score(0.12)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            # Generate
            gen_result = await generator.generate(state, low_ambiguity)
            assert gen_result.is_ok
            original_seed = gen_result.value

            # Save
            file_path = Path(tmp_dir) / "roundtrip_seed.yaml"
            save_result = await generator.save_seed(original_seed, file_path)
            assert save_result.is_ok

            # Load
            load_result = await load_seed(file_path)
            assert load_result.is_ok
            loaded_seed = load_result.value

            # Verify
            assert loaded_seed.goal == original_seed.goal
            assert loaded_seed.constraints == original_seed.constraints
            assert loaded_seed.acceptance_criteria == original_seed.acceptance_criteria
            assert loaded_seed.ontology_schema.name == original_seed.ontology_schema.name
            assert loaded_seed.metadata.ambiguity_score == original_seed.metadata.ambiguity_score
            assert loaded_seed.metadata.interview_id == original_seed.metadata.interview_id


class TestGeneratedSeedImmutability:
    """Test that generated Seeds are immutable."""

    @pytest.mark.asyncio
    async def test_generated_seed_is_frozen(self) -> None:
        """Generated Seed is frozen (immutable)."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            seed = result.value

            # Verify immutability
            from pydantic import ValidationError as PydanticValidationError

            with pytest.raises(PydanticValidationError):
                seed.goal = "Modified goal"  # type: ignore[misc]

            with pytest.raises(PydanticValidationError):
                seed.constraints = ("new constraint",)  # type: ignore[misc]
