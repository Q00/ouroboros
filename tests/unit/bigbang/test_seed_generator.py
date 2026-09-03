"""Unit tests for ouroboros.bigbang.seed_generator module."""

import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from ouroboros.auto.grading import GradeGate, SeedGrade
from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
)
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.seed_generator import (
    SeedGenerator,
    _iter_outer_ac_field_markers,
    _parse_acceptance_criterion_contract,
    _parse_context_references,
    _parse_evaluation_principles,
    _parse_exit_conditions,
    _parse_extracted_acceptance_criteria,
    _parse_ontology_fields,
    _parse_string_array_values,
    load_seed,
    save_seed_sync,
)
from ouroboros.config.loader import get_clarification_model
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.seed import (
    MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES,
    AcceptanceCriterionSpec,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_texts,
)
from ouroboros.core.types import Result
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo

_ADJACENT_SINGLE_QUOTED_VERIFY_COMMANDS = (
    "printf'%s| artifacts: literal'",
    "echo foo'| artifacts: literal'",
    "python -c'print(\"| verify: literal\")'",
)


class _RuntimeVerifyAdapter:
    def __init__(self, working_directory: str) -> None:
        self.runtime_backend = "claude"
        self.self_governs_rate_limit = True
        self.working_directory = working_directory
        self.permission_mode = "acceptEdits"


def create_runtime_verify_executor(working_directory: str) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=_RuntimeVerifyAdapter(working_directory),  # type: ignore[arg-type]
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        verify_command_timeout_seconds=5,
        ac_retry_attempts=0,
    )


def create_mock_completion_response(
    content: str,
    model: str = "claude-opus-4-6",
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
    constraints: str = '["Python 3.14+", "No external database", "Single-file storage"]',
    acceptance_criteria: str = "Tasks can be created | Tasks can be listed | Tasks can be deleted",
    ontology_name: str = "TaskManager",
    ontology_description: str = "Task management domain model",
    ontology_fields: str = (
        '[{"name": "tasks", "type": "array", "description": "List of task objects"},'
        ' {"name": "projects", "type": "array", "description": "List of project objects"}]'
    ),
    evaluation_principles: str = (
        '[{"name": "completeness", "description": "All requirements implemented",'
        ' "weight": 0.4},'
        ' {"name": "quality", "description": "Code meets standards", "weight": 0.3}]'
    ),
    exit_conditions: str = (
        '[{"name": "all_criteria_met", "description": "All acceptance criteria pass",'
        ' "criteria": "100% criteria satisfied"}]'
    ),
) -> str:
    """Create a valid LLM extraction response string."""
    if not acceptance_criteria.lstrip().startswith("["):
        try:
            if "AC:" in acceptance_criteria:
                specs = [
                    _parse_acceptance_criterion_contract(line.strip())
                    for line in acceptance_criteria.splitlines()
                    if line.strip()
                ]
                if any(spec is None for spec in specs):
                    raise ValueError("invalid legacy AC fixture")
                criteria_objects = [
                    {
                        "description": spec.description,
                        "verify": spec.verify_command or "NONE",
                        "artifacts": list(spec.expected_artifacts) or "NONE",
                        "expect": spec.output_assertion or "NONE",
                    }
                    for spec in specs
                    if spec is not None
                ]
            else:
                criteria_objects = [
                    {
                        "description": description.strip(),
                        "verify": "NONE",
                        "artifacts": "NONE",
                        "expect": "NONE",
                    }
                    for description in acceptance_criteria.split("|")
                    if description.strip()
                ]
            acceptance_criteria = json.dumps(criteria_objects)
        except ValueError:
            # Negative fixtures intentionally feed malformed legacy extraction
            # text so the production retry boundary can reject it.
            pass
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

            assert generator.model == get_clarification_model()
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

    @pytest.mark.asyncio
    async def test_generate_requires_summary_for_large_initial_context(self) -> None:
        """SeedGenerator.generate() fails when long initial_context has no summary."""
        mock_adapter = AsyncMock()
        state = InterviewState(
            interview_id="test_large_context",
            initial_context=("A" * 4_000) + "TAIL_MARKER",
        )
        low_ambiguity = create_low_ambiguity_score()
        generator = SeedGenerator(llm_adapter=mock_adapter)

        result = await generator.generate(state, low_ambiguity)

        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "summary required" in result.error.message

    @pytest.mark.asyncio
    async def test_generate_requires_summary_for_completed_large_initial_context(
        self,
    ) -> None:
        """Completed long-context interviews still enforce summary before seed generation."""
        mock_adapter = AsyncMock()
        state = InterviewState(
            interview_id="test_completed_large_context",
            initial_context=("A" * 4_000) + "TAIL_MARKER",
            status=InterviewStatus.COMPLETED,
        )
        low_ambiguity = create_low_ambiguity_score()
        generator = SeedGenerator(llm_adapter=mock_adapter)

        result = await generator.generate(state, low_ambiguity)

        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "summary required" in result.error.message
        mock_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_with_force_bypasses_gate_at_high_score(self) -> None:
        """force=True bypasses the ambiguity gate even at scores far above threshold."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        high_ambiguity = create_high_ambiguity_score(0.5)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, high_ambiguity, force=True)

            assert result.is_ok
            assert isinstance(result.value, Seed)
            # Provenance: forced seeds carry the real (high) score, not a fabricated one.
            assert result.value.metadata.ambiguity_score == 0.5

    @pytest.mark.asyncio
    async def test_generate_with_force_bypasses_just_above_threshold(self) -> None:
        """force=True succeeds at the boundary just above the gate (score=0.21)."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        boundary = create_high_ambiguity_score(AMBIGUITY_THRESHOLD + 0.01)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, boundary, force=True)

            assert result.is_ok

    @pytest.mark.asyncio
    async def test_generate_without_force_still_fails_above_threshold(self) -> None:
        """Default force=False preserves existing failure when score > threshold."""
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
            assert isinstance(result.error, ValidationError)
            assert "exceeds threshold" in result.error.message

    @pytest.mark.asyncio
    async def test_generate_with_force_logs_bypass_warning(self) -> None:
        """force=True emits a structured warning so bypass is auditable."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        high_ambiguity = create_high_ambiguity_score(0.5)

        extraction_response = create_valid_extraction_response()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            with patch("ouroboros.bigbang.seed_generator.log.warning") as mock_warning:
                result = await generator.generate(state, high_ambiguity, force=True)

            assert result.is_ok
            bypass_calls = [
                call
                for call in mock_warning.call_args_list
                if call.args and call.args[0] == "seed.generation.ambiguity_gate_bypassed"
            ]
            assert len(bypass_calls) == 1
            assert bypass_calls[0].kwargs["ambiguity_score"] == 0.5
            assert bypass_calls[0].kwargs["threshold"] == AMBIGUITY_THRESHOLD

    @pytest.mark.asyncio
    async def test_generate_with_force_still_requires_initial_context_summary(
        self,
    ) -> None:
        """force=True only bypasses the ambiguity gate, not the summary check."""
        mock_adapter = AsyncMock()
        state = InterviewState(
            interview_id="test_force_with_long_context",
            initial_context=("A" * 4_000) + "TAIL_MARKER",
        )
        high_ambiguity = create_high_ambiguity_score(0.5)
        generator = SeedGenerator(llm_adapter=mock_adapter)

        result = await generator.generate(state, high_ambiguity, force=True)

        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "summary required" in result.error.message


class TestSeedGeneratorExtraction:
    """Test SeedGenerator requirement extraction."""

    @pytest.mark.parametrize("description", ("Users' files exist", "James' file exists"))
    def test_ac_field_marker_scanner_treats_word_final_possessives_as_prose(
        self,
        description: str,
    ) -> None:
        body = f"{description} | verify: test -f output.txt | artifacts: output.txt | expect: NONE"

        markers = _iter_outer_ac_field_markers(body)

        assert tuple(marker.name for marker in markers) == ("verify", "artifacts", "expect")

    @pytest.mark.parametrize(
        "body",
        (
            (
                'Command works | verify: sh -c \'printf "%s\\n" '
                '"| artifacts: literal"\' | artifacts: out.log | expect: NONE'
            ),
            (
                "Command works | verify: bash -lc \"printf '%s\\n' "
                "'| verify: literal'"
                '" | artifacts: out.log | expect: NONE'
            ),
            (
                r"Command works | verify: printf \|\ artifacts:literal > out.log | "
                "artifacts: out.log | expect: NONE"
            ),
            (
                "Command works | verify: sh -c 'printf \"| artifacts literal\"' | "
                "artifacts: out.log | expect: NONE"
            ),
        ),
    )
    def test_ac_field_marker_scanner_preserves_quoted_and_escaped_pipe_payloads(
        self,
        body: str,
    ) -> None:
        markers = _iter_outer_ac_field_markers(body)

        assert tuple(marker.name for marker in markers) == ("verify", "artifacts", "expect")

    @pytest.mark.parametrize(
        ("body", "field_name"),
        (
            ("Files exist | verify: test -f output.txt | artifacts output.txt", "artifacts"),
            ("Pipeline works | verify: printf READY | ver ify --mode strict", "verify"),
            (r"Pipeline works | verify: printf READY | ver\ify --mode strict", "verify"),
            ("Pipeline works | verify: printf READY | ver'ify --mode strict", "verify"),
            ("Pipeline works | verify: printf READY | arti facts output.txt", "artifacts"),
            (r"Pipeline works | verify: printf READY | arti\facts output.txt", "artifacts"),
            ("Pipeline works | verify: printf READY | arti'facts output.txt", "artifacts"),
            ("Pipeline works | verify: printf READY | ex pect READY", "expect"),
            (r"Pipeline works | verify: printf READY | ex\pect READY", "expect"),
            ("Pipeline works | verify: printf READY | ex'pect READY", "expect"),
            ("Pipeline works | verify: printf READY | verify'': strict", "verify"),
            (r"Pipeline works | verify: printf READY | verify\:", "verify"),
            ('Pipeline works | verify: printf READY | verify"": strict', "verify"),
            ("Pipeline works | verify: printf READY | artifacts'': out.txt", "artifacts"),
            (r"Pipeline works | verify: printf READY | artifacts\:", "artifacts"),
            ('Pipeline works | verify: printf READY | artifacts"": out.txt', "artifacts"),
            ("Pipeline works | verify: printf READY | expect'': READY", "expect"),
            (r"Pipeline works | verify: printf READY | expect\:", "expect"),
            ('Pipeline works | verify: printf READY | expect"": READY', "expect"),
        ),
    )
    def test_ac_field_marker_scanner_rejects_malformed_or_obfuscated_operator(
        self,
        body: str,
        field_name: str,
    ) -> None:
        with pytest.raises(ValueError, match=rf"Malformed {field_name} field"):
            _iter_outer_ac_field_markers(body)

    @pytest.mark.parametrize(
        "line",
        (
            (
                'AC: Output file exists " | verify: true | '
                "artifacts: schema v2 outputs.json | expect: NONE"
            ),
            'AC: Output file exists " | artifacts: schema v2 outputs.json | expect: NONE',
            'AC: Output file exists " | artifacts schema v2 outputs.json',
            'AC: Output file exists " | verify: true | artifacts: outputs.json | expect: NONE \\',
        ),
    )
    def test_acceptance_contract_parser_rejects_unterminated_quote_hiding_reserved_fields(
        self,
        line: str,
    ) -> None:
        with pytest.raises(ValueError, match="Unterminated quoted or escaped"):
            _parse_acceptance_criterion_contract(line)

    @pytest.mark.parametrize(
        "line,field_name",
        (
            (
                r"AC: Output file exists \| artifacts: schema v2 outputs.json | expect: NONE",
                "artifacts",
            ),
            (r"AC: Output file exists \| artifacts schema v2 outputs.json", "artifacts"),
            (
                r"AC: Output file exists \| verify: true | artifacts: outputs.json | expect: NONE",
                "verify",
            ),
        ),
    )
    def test_acceptance_contract_parser_rejects_escaped_pre_marker_reserved_fields(
        self,
        line: str,
        field_name: str,
    ) -> None:
        with pytest.raises(ValueError, match=rf"Escaped {field_name} field"):
            _parse_acceptance_criterion_contract(line)

    @pytest.mark.parametrize(
        "line,field_name",
        (
            (
                'AC: Output file exists "| artifacts: schema v2 outputs.json"',
                "artifacts",
            ),
            ('AC: Output file exists "| artifacts schema v2 outputs.json"', "artifacts"),
            ('AC: Command status is enforced "| verify: exit 1"', "verify"),
            ('AC: Command output is checked "| expect: READY"', "expect"),
        ),
    )
    def test_acceptance_contract_parser_rejects_quoted_pre_marker_reserved_fields(
        self,
        line: str,
        field_name: str,
    ) -> None:
        with pytest.raises(ValueError, match=rf"Quoted {field_name} field"):
            _parse_acceptance_criterion_contract(line)

    @pytest.mark.parametrize(
        "line,field_name",
        (
            ("AC: Output file exists | arti facts: schema v2 outputs.json", "artifacts"),
            ('AC: Output file exists | arti"facts: schema v2 outputs.json', "artifacts"),
            (r"AC: Output file exists | arti\facts: schema v2 outputs.json", "artifacts"),
            ("AC: Command status is enforced | ver ify: exit 1", "verify"),
            ("AC: Command output is checked | ex pect: READY", "expect"),
        ),
    )
    def test_acceptance_contract_parser_rejects_split_pre_marker_reserved_fields(
        self,
        line: str,
        field_name: str,
    ) -> None:
        with pytest.raises(ValueError, match=rf"Malformed {field_name} field"):
            _parse_acceptance_criterion_contract(line)

    def test_acceptance_contract_parser_allows_unrelated_pipe_led_prose(self) -> None:
        criterion = _parse_acceptance_criterion_contract(
            "AC: Report preserves terminology | art facts: explanatory prose"
        )

        assert criterion is not None
        assert criterion.description == (
            "Report preserves terminology | art facts: explanatory prose"
        )
        assert criterion.has_success_contract is False

    def test_acceptance_contract_parser_preserves_apostrophes_in_structured_values(
        self,
    ) -> None:
        criterion = _parse_acceptance_criterion_contract(
            "AC: Operator documentation is ready | verify: test -f guide.md | "
            "artifacts: reports/user's guide.md | expect: operator's ready"
        )

        assert criterion is not None
        assert criterion.expected_artifacts == ("reports/user's guide.md",)
        assert criterion.output_assertion == "operator's ready"

    @pytest.mark.parametrize(
        "line,field_name",
        (
            (
                "AC: Command output is checked | verify: printf WAITING | ex pect: READY",
                "expect",
            ),
            (
                "AC: Output file exists | verify: true | arti facts: missing.txt",
                "artifacts",
            ),
            (
                "AC: Command status is enforced | verify: true | ver ify: exit 1",
                "verify",
            ),
        ),
    )
    def test_acceptance_contract_parser_rejects_split_reserved_fields_after_real_field(
        self,
        line: str,
        field_name: str,
    ) -> None:
        with pytest.raises(ValueError, match=rf"Malformed {field_name} field"):
            _parse_acceptance_criterion_contract(line)

    @pytest.mark.parametrize(
        "verify_command",
        (
            'sh -c \'printf "%s\\n" "| ex pect: literal"\'',
            "bash -lc \"printf '%s\\n' '| arti facts: literal'\"",
            r"printf \|\ ver\ ify:\ literal",
        ),
    )
    def test_acceptance_contract_parser_preserves_quoted_or_escaped_split_marker_literals(
        self,
        verify_command: str,
    ) -> None:
        criterion = _parse_acceptance_criterion_contract(
            "AC: Command payload is preserved | "
            f"verify: {verify_command} | artifacts: output.txt | expect: NONE"
        )

        assert criterion is not None
        assert criterion.verify_command == verify_command
        assert criterion.expected_artifacts == ("output.txt",)

    def test_acceptance_contract_parser_allows_closed_quote_without_reserved_marker(
        self,
    ) -> None:
        criterion = _parse_acceptance_criterion_contract(
            'AC: Output file is named "draft | final" | verify: true | '
            "artifacts: output.txt | expect: NONE"
        )

        assert criterion is not None
        assert criterion.description == 'Output file is named "draft | final"'
        assert criterion.verify_command == "true"
        assert criterion.expected_artifacts == ("output.txt",)

    def test_acceptance_contract_parser_allows_unmatched_quote_without_reserved_marker(
        self,
    ) -> None:
        criterion = _parse_acceptance_criterion_contract('AC: Output file is named "draft')

        assert criterion is not None
        assert criterion.description == 'Output file is named "draft'
        assert criterion.verify_command is None
        assert criterion.expected_artifacts == ()

    @pytest.mark.parametrize("representation", ("block", "inline"))
    @pytest.mark.parametrize(
        "ac_line,continuation,field_name",
        (
            ('AC: Output file exists "', "| artifacts: schema v2 outputs.json", "artifacts"),
            ('AC: Output file exists "', "| artifacts schema v2 outputs.json", "artifacts"),
            ("AC: Command status is enforced \\", "| verify: exit 1", "verify"),
            ('AC: Command output is checked "', "| expect: READY", "expect"),
            ('AC: Output file exists "', "| arti facts: schema v2 outputs.json", "artifacts"),
            ('AC: Output file exists "', '| arti"facts: schema v2 outputs.json', "artifacts"),
            ('AC: Output file exists "', r"| arti\facts: schema v2 outputs.json", "artifacts"),
            ("AC: Command status is enforced \\", "| ver ify: exit 1", "verify"),
            ('AC: Command output is checked "', "| ex pect: READY", "expect"),
        ),
    )
    def test_extraction_rejects_reserved_non_ac_continuation_lines(
        self,
        representation: str,
        ac_line: str,
        continuation: str,
        field_name: str,
    ) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        acceptance_criteria = f"{ac_line}\n{continuation}"
        if representation == "block":
            acceptance_criteria = f"\n{acceptance_criteria}\n"
        response = create_valid_extraction_response(acceptance_criteria=acceptance_criteria)

        with pytest.raises(ValueError, match="JSON array of objects|Unrecognized extraction line"):
            generator._parse_extraction_response(response)

    @pytest.mark.parametrize("representation", ("block", "inline"))
    def test_extraction_rejects_any_non_ac_acceptance_continuation(
        self,
        representation: str,
    ) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        acceptance_criteria = "AC: Output file exists\nwrapped prose continuation"
        if representation == "block":
            acceptance_criteria = f"\n{acceptance_criteria}\n"
        response = create_valid_extraction_response(acceptance_criteria=acceptance_criteria)

        with pytest.raises(ValueError, match="JSON array of objects|Unrecognized extraction line"):
            generator._parse_extraction_response(response)

    def test_extraction_allows_reserved_marker_text_in_other_fields(self) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        response = create_valid_extraction_response(
            acceptance_criteria="\nAC: Output file exists\n",
            ontology_description="Domain prose with | verify: text",
        )

        requirements = generator._parse_extraction_response(response)

        assert requirements["ontology_description"] == "Domain prose with | verify: text"

    def test_extraction_collects_inline_and_continued_ac_records(self) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        response = create_valid_extraction_response(
            acceptance_criteria=(
                "AC: First output exists | verify: NONE | artifacts: first.txt | expect: NONE\n"
                "AC: Second output exists | verify: NONE | artifacts: second.txt | expect: NONE"
            )
        )

        requirements = generator._parse_extraction_response(response)

        assert tuple(spec.description for spec in requirements["acceptance_criteria"]) == (
            "First output exists",
            "Second output exists",
        )
        assert tuple(spec.expected_artifacts for spec in requirements["acceptance_criteria"]) == (
            ("first.txt",),
            ("second.txt",),
        )

    def test_extraction_preserves_verify_execution_contract(self) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        response = create_valid_extraction_response(
            acceptance_criteria=(
                '[{"description":"UI tests pass","verify":"npm test",'
                '"cwd":"app","replay_safe":true,"artifacts":"NONE","expect":"NONE"}]'
            )
        )

        requirements = generator._parse_extraction_response(response)
        (criterion,) = requirements["acceptance_criteria"]

        assert criterion.verify_cwd == "app"
        assert criterion.verify_replay_safe is True

    @pytest.mark.parametrize(
        "acceptance_criteria",
        (
            '[{"description":"x","verify":"true","artifacts":"NONE"}]',
            '[{"description":"x","verify":"true","artifacts":"NONE","expect":"NONE","extra":true}]',
            '[{"description":"x","description":"y","verify":"true",'
            '"artifacts":"NONE","expect":"NONE"}]',
            '[{"description":"x","verify":1,"artifacts":"NONE","expect":"NONE"}]',
            '[{"description":"x","verify":"true","artifacts":"out.txt","expect":"NONE"}]',
            '[{"description":"x","verify":"cat <<EOF","artifacts":"NONE","expect":"NONE"}]',
            json.dumps(
                [
                    {
                        "description": "x",
                        "verify": 'printf "%s" "`unterminated"',
                        "artifacts": "NONE",
                        "expect": "NONE",
                    }
                ]
            ),
            '[{"description":"x","verify":"true","artifacts":["../out.txt"],"expect":"NONE"}]',
        ),
    )
    def test_extraction_rejects_invalid_acceptance_criteria_json_contract(
        self, acceptance_criteria: str
    ) -> None:
        generator = SeedGenerator(llm_adapter=AsyncMock())
        response = create_valid_extraction_response(acceptance_criteria=acceptance_criteria)

        with pytest.raises(ValueError):
            generator._parse_extraction_response(response)

    @pytest.mark.parametrize("malformed_kind", ("missing", "empty", "duplicate", "injected"))
    @pytest.mark.asyncio
    async def test_generate_rejects_required_acceptance_criteria_shape_on_both_attempts(
        self, malformed_kind: str
    ) -> None:
        response = create_valid_extraction_response(
            acceptance_criteria="[]" if malformed_kind == "empty" else "Outcome exists"
        )
        if malformed_kind == "missing":
            response = "\n".join(
                line
                for line in response.splitlines()
                if not line.startswith("ACCEPTANCE_CRITERIA:")
            )
        elif malformed_kind == "duplicate":
            acceptance_line = next(
                line for line in response.splitlines() if line.startswith("ACCEPTANCE_CRITERIA:")
            )
            response = response.replace(acceptance_line, f"{acceptance_line}\n{acceptance_line}")
        elif malformed_kind == "injected":
            response += "\nAC: injected | verify: true | artifacts: NONE | expect: NONE"

        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(response)),
                Result.ok(create_mock_completion_response(response)),
            ]
        )
        generator = SeedGenerator(llm_adapter=mock_adapter)

        result = await generator.generate(
            create_interview_state_with_rounds(), create_low_ambiguity_score()
        )

        assert result.is_err
        assert mock_adapter.complete.await_count == 2

    @pytest.mark.parametrize("verify_command", _ADJACENT_SINGLE_QUOTED_VERIFY_COMMANDS)
    def test_acceptance_contract_parser_preserves_adjacent_single_quoted_payloads(
        self,
        verify_command: str,
    ) -> None:
        criterion = _parse_acceptance_criterion_contract(
            "AC: Adjacent shell quote remains payload | "
            f"verify: {verify_command} | artifacts: out.txt | expect: OK"
        )

        assert criterion is not None
        assert criterion.verify_command == verify_command
        assert criterion.expected_artifacts == ("out.txt",)
        assert criterion.output_assertion == "OK"

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
            constraints='["Python 3.14+", "No external database", "Must be cross-platform"]'
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
    async def test_generate_preserves_literal_pipe_in_json_array_constraints(self) -> None:
        """JSON-array CONSTRAINTS keep literal pipes inside one constraint (#1696)."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            constraints='["--lang ko|en", "keep exact flag"]'
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
            assert result.value.constraints == ("--lang ko|en", "keep exact flag")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("representation", ("multiline", "inline"))
    @pytest.mark.parametrize(
        "malformed_artifacts",
        (
            ",",
            "safe.txt,,other.txt",
            "safe.txt | artifacts: other.txt",
            "safe.txt | artifacts other.txt",
            "schema v2 outputs.json",
            "reports/summary,v2.json",
            r"docs\guide.md",
            "NUL",
            "docs/a:b",
            "a" * 256,
            "docs/" + ("\u00e9" * 128),
            "NONE, report.txt",
            '["NONE", "report.txt"]',
        ),
    )
    async def test_generate_retries_on_malformed_artifact_fields(
        self,
        representation: str,
        malformed_artifacts: str,
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        bad_contract = (
            f"AC: Files exist | verify: pytest -q | artifacts: {malformed_artifacts} | expect: NONE"
        )
        repaired_contract = (
            "AC: Files exist | verify: pytest -q | "
            "artifacts: safe.txt, other.txt, ./Build Outputs | expect: NONE"
        )
        if representation == "multiline":
            bad_contract = f"\n{bad_contract}\n"
            repaired_contract = f"\n{repaired_contract}\n"
        bad_response = create_valid_extraction_response(acceptance_criteria=bad_contract)
        repaired_response = create_valid_extraction_response(acceptance_criteria=repaired_contract)
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.expected_artifacts == ("safe.txt", "other.txt", "./Build Outputs")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("representation", ("multiline", "inline"))
    async def test_generate_retries_on_contract_marker_without_leading_whitespace(
        self,
        representation: str,
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        bad_contract = "AC: Files exist|artifacts: NUL"
        repaired_contract = "AC: Files exist|artifacts: reports/summary.json, ./Build Outputs"
        if representation == "multiline":
            bad_contract = f"\n{bad_contract}\n"
            repaired_contract = f"\n{repaired_contract}\n"
        bad_response = create_valid_extraction_response(acceptance_criteria=bad_contract)
        repaired_response = create_valid_extraction_response(acceptance_criteria=repaired_contract)
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.expected_artifacts == ("reports/summary.json", "./Build Outputs")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expectation", ("success", "exit code 0"))
    async def test_generate_retries_when_status_expectation_lacks_verify_command(
        self,
        expectation: str,
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        bad_contract = f"AC: CLI succeeds | verify: NONE | artifacts: NONE | expect: {expectation}"
        repaired_contract = (
            "AC: CLI succeeds | verify: python -m app | artifacts: NONE | expect: NONE"
        )
        bad_response = create_valid_extraction_response(acceptance_criteria=f"\n{bad_contract}\n")
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=f"\n{repaired_contract}\n"
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.verify_command == "python -m app"
        assert criterion.output_assertion is None

    @pytest.mark.asyncio
    async def test_generate_retries_bracket_prose_and_accepts_reformatted_json(self) -> None:
        """At extraction time bracket prose triggers retry; the reformatted array wins."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        prose_response = create_valid_extraction_response(
            constraints="[P0] Must work offline | [P1] Fast startup"
        )
        reformatted_response = create_valid_extraction_response(
            constraints='["[P0] Must work offline", "[P1] Fast startup"]'
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(prose_response)),
                Result.ok(create_mock_completion_response(reformatted_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.constraints == (
                "[P0] Must work offline",
                "[P1] Fast startup",
            )
            assert mock_adapter.complete.await_count == 2

    def test_strict_rejects_any_bracket_led_value_that_is_not_a_json_string_array(self) -> None:
        """Strict (extraction-time) mode: [-led values must be JSON string arrays."""
        malformed_cases = (
            '["--lang ko|en",]',  # trailing comma
            '["--lang ko|en"',  # truncated, quote-led
            "[--lang ko|en",  # truncated, unquoted
            '["--lang ko|en"] stray text',  # valid array + trailing garbage
            '[null, "x"] stray text',  # JSON-parseable group + trailing garbage
            "[--lang ko|en, keep exact flag] trailing note",  # unquoted array shape
            "[ko] preserve ko|en flag",  # word-tag shape from malformed array output
            "[P0] Must work offline | [P1] Fast startup",  # prose retries at extraction
            "--lang ko|en | keep exact flag",  # plain pipe list, no brackets
            "Must work offline",  # plain prose, not an array
            '"just a JSON string"',  # valid JSON but not an array
        )
        for malformed in malformed_cases:
            with pytest.raises(ValueError, match="JSON array"):
                _parse_string_array_values(malformed, field_label="CONSTRAINTS", strict=True)

    def test_strict_rejects_non_string_json_entries(self) -> None:
        """Strict mode: JSON arrays must contain only strings."""
        for malformed in ("[null]", '[1, "x"]'):
            with pytest.raises(ValueError, match="only strings"):
                _parse_string_array_values(malformed, field_label="CONSTRAINTS", strict=True)

    def test_lenient_keeps_legacy_bracket_prose_split(self) -> None:
        """Lenient (stored-data) mode: bracket prose keeps the historical split."""
        assert _parse_string_array_values(
            "[P0] Must work offline | [P1] Fast startup", field_label="CONSTRAINTS"
        ) == (
            "[P0] Must work offline",
            "[P1] Fast startup",
        )
        assert _parse_string_array_values(
            "[v2.1-rc] Must ship | [2026-01] Later", field_label="CONSTRAINTS"
        ) == (
            "[v2.1-rc] Must ship",
            "[2026-01] Later",
        )

    def test_lenient_never_raises_on_malformed_json(self) -> None:
        """Lenient mode: stored data has no retry path, so it pipe-splits instead."""
        assert _parse_string_array_values('["--lang ko|en",]', field_label="CONSTRAINTS") == (
            '["--lang ko',
            'en",]',
        )
        assert _parse_string_array_values("[null]", field_label="CONSTRAINTS") == ("None",)
        assert _parse_string_array_values(
            "Python 3.14+ | No external database", field_label="CONSTRAINTS"
        ) == (
            "Python 3.14+",
            "No external database",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malformed_constraints",
        (
            '["--lang ko|en", "keep exact flag",]',
            "[--lang ko|en | keep exact flag",
            '["--lang ko|en"] stray text',
            "[null]",
            '[null, "x"] stray text',
            "[--lang ko|en, keep exact flag] trailing note",
            "[ko] preserve ko|en flag",
            "--lang ko|en | keep exact flag",
        ),
    )
    async def test_generate_retries_on_malformed_json_constraints(
        self, malformed_constraints: str
    ) -> None:
        """Malformed JSON-intent CONSTRAINTS trigger the extraction retry path."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        malformed_response = create_valid_extraction_response(constraints=malformed_constraints)
        valid_response = create_valid_extraction_response(
            constraints='["--lang ko|en", "keep exact flag"]'
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed_response)),
                Result.ok(create_mock_completion_response(valid_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.constraints == ("--lang ko|en", "keep exact flag")
            assert mock_adapter.complete.await_count == 2

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
            assert ac_texts(result.value.acceptance_criteria) == (
                "Tasks can be created",
                "Tasks can be listed",
            )

    @pytest.mark.asyncio
    async def test_generate_extracts_structured_acceptance_criteria_contracts(self) -> None:
        """SeedGenerator extracts AC success contracts from multiline architect output."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                'AC: CLI lists tasks | verify: bash -lc "pytest -q | tee out.log" | '
                "artifacts: tasks.json, logs/task.log, ./Build Outputs | expect: No tasks\n"
                "AC: Docs explain usage | verify: NONE | artifacts: README.md | expect: NONE\n"
                "AC: Legacy line without contract"
            )
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
            first, second, third = result.value.acceptance_criteria
            assert isinstance(first, AcceptanceCriterionSpec)
            assert first.description == "CLI lists tasks"
            assert first.verify_command == 'bash -lc "pytest -q | tee out.log"'
            assert first.expected_artifacts == ("tasks.json", "logs/task.log", "./Build Outputs")
            assert first.output_assertion == "No tasks"
            assert second.description == "Docs explain usage"
            assert second.verify_command is None
            assert second.expected_artifacts == ("README.md",)
            assert third.description == "Legacy line without contract"
            assert third.verify_command is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("verify_command", "expected_artifact"),
        (
            (
                """bash -lc "printf '%s\\n' '| artifacts: literal' | tee out.log" """.strip(),
                "out.log",
            ),
            (
                """sh -c 'printf "%s\\n" "| verify: literal" | tee reports/verify.log'""",
                "reports/verify.log",
            ),
            (
                r"""python -c "print(\"| expect: literal\")" > escaped-quote.txt""",
                "escaped-quote.txt",
            ),
            (
                r"""printf \|\ artifacts:literal > escaped-pipe.txt""",
                "escaped-pipe.txt",
            ),
        ),
    )
    async def test_generate_preserves_quoted_reserved_pipe_literals_in_contracts(
        self,
        verify_command: str,
        expected_artifact: str,
    ) -> None:
        """Quoted/escaped reserved pipe text is verify payload, not an outer AC field."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                f"AC: Command preserves reserved pipe literal | verify: {verify_command} | "
                f"artifacts: {expected_artifact} | expect: NONE\n"
            )
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
            assert mock_adapter.complete.await_count == 1
            (criterion,) = result.value.acceptance_criteria
            assert isinstance(criterion, AcceptanceCriterionSpec)
            assert criterion.verify_command == verify_command
            assert criterion.expected_artifacts == (expected_artifact,)
            assert criterion.output_assertion is None
            assert Seed.model_validate(result.value.model_dump()) == result.value

    @pytest.mark.asyncio
    async def test_generate_preserves_apostrophes_through_seed_schema_and_grade(self) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Users don't lose files | verify: test -f guide.md | "
                "artifacts: reports/user's guide.md | expect: operator's ready\n"
            )
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
        (criterion,) = result.value.acceptance_criteria
        assert criterion.description == "Users don't lose files"
        assert criterion.expected_artifacts == ("reports/user's guide.md",)
        assert criterion.output_assertion == "operator's ready"
        assert Seed.model_validate(result.value.model_dump()) == result.value
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    async def test_generate_does_not_treat_possessive_description_as_quote_bypass(
        self,
    ) -> None:
        """A possessive apostrophe cannot hide malformed structured AC fields."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bypass_attempt = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: User's file exists | verify: test -f user.txt | "
                "artifacts: schema v2 outputs.json | expect: NONE\n"
            )
        )
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: User's file exists | verify: test -f user.txt | "
                "artifacts: user.txt | expect: NONE\n"
            )
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bypass_attempt)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.description == "User's file exists"
        assert criterion.verify_command == "test -f user.txt"
        assert criterion.expected_artifacts == ("user.txt",)
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    async def test_generate_does_not_treat_word_final_possessive_as_quote_bypass(
        self,
    ) -> None:
        """A word-final possessive cannot hide malformed structured AC fields."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bypass_attempt = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Users' files exist | verify: test -f users.txt | "
                "artifacts: schema v2 outputs.json | expect: NONE\n"
            )
        )
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Users' files exist | verify: test -f users.txt | "
                "artifacts: users.txt | expect: NONE\n"
            )
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bypass_attempt)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.description == "Users' files exist"
        assert criterion.verify_command == "test -f users.txt"
        assert criterion.expected_artifacts == ("users.txt",)
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bypass_attempt,repaired_contract,expected_verify,expected_artifacts",
        (
            (
                (
                    'AC: Output file exists " | verify: true | '
                    "artifacts: schema v2 outputs.json | expect: NONE"
                ),
                "AC: Output file exists | verify: true | artifacts: outputs.json | expect: NONE",
                "true",
                ("outputs.json",),
            ),
            (
                'AC: Output file exists " | artifacts schema v2 outputs.json',
                "AC: Output file exists | verify: test -f outputs.json | "
                "artifacts: outputs.json | expect: NONE",
                "test -f outputs.json",
                ("outputs.json",),
            ),
            (
                'AC: Output file exists " | artifacts: schema v2 outputs.json | expect: NONE',
                "AC: Output file exists | verify: NONE | artifacts: outputs.json | expect: NONE",
                None,
                ("outputs.json",),
            ),
        ),
    )
    async def test_generate_retries_unterminated_quote_hiding_success_contracts_before_grading(
        self,
        bypass_attempt: str,
        repaired_contract: str,
        expected_verify: str | None,
        expected_artifacts: tuple[str, ...],
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bad_response = create_valid_extraction_response(acceptance_criteria=f"\n{bypass_attempt}\n")
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=f"\n{repaired_contract}\n"
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.description == "Output file exists"
        assert criterion.verify_command == expected_verify
        assert criterion.expected_artifacts == expected_artifacts
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bypass_attempt,repaired_contract,expected_verify,expected_artifacts",
        (
            (
                r"AC: Output file exists \| artifacts: schema v2 outputs.json | expect: NONE",
                "AC: Output file exists | verify: NONE | artifacts: outputs.json | expect: NONE",
                None,
                ("outputs.json",),
            ),
            (
                r"AC: Output file exists \| artifacts schema v2 outputs.json",
                "AC: Output file exists | verify: test -f outputs.json | "
                "artifacts: outputs.json | expect: NONE",
                "test -f outputs.json",
                ("outputs.json",),
            ),
            (
                r"AC: Output file exists \| verify: true | artifacts: outputs.json | expect: NONE",
                "AC: Output file exists | verify: true | artifacts: outputs.json | expect: NONE",
                "true",
                ("outputs.json",),
            ),
        ),
    )
    async def test_generate_retries_escaped_pre_marker_success_contracts_before_grading(
        self,
        bypass_attempt: str,
        repaired_contract: str,
        expected_verify: str | None,
        expected_artifacts: tuple[str, ...],
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bad_response = create_valid_extraction_response(acceptance_criteria=f"\n{bypass_attempt}\n")
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=f"\n{repaired_contract}\n"
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.description == "Output file exists"
        assert criterion.verify_command == expected_verify
        assert criterion.expected_artifacts == expected_artifacts
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("representation", ("block", "inline"))
    @pytest.mark.parametrize(
        "bypass_attempt,repaired_contract,expected_gate_error",
        (
            (
                'AC: Command status is enforced "| verify: exit 1"',
                "AC: Command status is enforced | verify: exit 1 | artifacts: NONE | expect: NONE",
                "status 1",
            ),
            (
                'AC: Output file exists "| artifacts: schema v2 outputs.json"',
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                'AC: Output file exists "| artifacts schema v2 outputs.json"',
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                'AC: Command output is checked "| expect: READY"',
                "AC: Command output is checked | verify: printf WAITING | "
                "artifacts: NONE | expect: READY",
                "output_assertion",
            ),
            (
                "AC: Command status is enforced \\\n| verify: exit 1",
                "AC: Command status is enforced | verify: exit 1 | artifacts: NONE | expect: NONE",
                "status 1",
            ),
            (
                'AC: Output file exists "\n| artifacts: schema v2 outputs.json',
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                'AC: Command output is checked "\n| expect: READY',
                "AC: Command output is checked | verify: printf WAITING | "
                "artifacts: NONE | expect: READY",
                "output_assertion",
            ),
            (
                "AC: Output file exists | arti facts: schema v2 outputs.json",
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                'AC: Output file exists | arti"facts: schema v2 outputs.json',
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                r"AC: Output file exists | arti\facts: schema v2 outputs.json",
                "AC: Output file exists | verify: NONE | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                "AC: Command status is enforced | ver ify: exit 1",
                "AC: Command status is enforced | verify: exit 1 | artifacts: NONE | expect: NONE",
                "status 1",
            ),
            (
                "AC: Command output is checked | ex pect: READY",
                "AC: Command output is checked | verify: printf WAITING | "
                "artifacts: NONE | expect: READY",
                "output_assertion",
            ),
            (
                "AC: Command output is checked | verify: printf WAITING | ex pect: READY",
                "AC: Command output is checked | verify: printf WAITING | "
                "artifacts: NONE | expect: READY",
                "output_assertion",
            ),
            (
                "AC: Output file exists | verify: true | arti facts: missing.txt",
                "AC: Output file exists | verify: true | artifacts: missing.txt | expect: NONE",
                "expected_artifacts missing",
            ),
            (
                "AC: Command status is enforced | verify: true | ver ify: exit 1",
                "AC: Command status is enforced | verify: exit 1 | artifacts: NONE | expect: NONE",
                "status 1",
            ),
        ),
    )
    async def test_generate_retries_reserved_contract_bypasses_before_runtime_gate(
        self,
        representation: str,
        bypass_attempt: str,
        repaired_contract: str,
        expected_gate_error: str,
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        bad_acceptance_criteria = bypass_attempt
        repaired_acceptance_criteria = repaired_contract
        if representation == "block":
            bad_acceptance_criteria = f"\n{bypass_attempt}\n"
            repaired_acceptance_criteria = f"\n{repaired_contract}\n"
        bad_response = create_valid_extraction_response(acceptance_criteria=bad_acceptance_criteria)
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=repaired_acceptance_criteria
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            seed = result.value
            assert isinstance(seed, Seed)
            assert mock_adapter.complete.await_count == 2
            (criterion,) = seed.acceptance_criteria
            assert isinstance(criterion, AcceptanceCriterionSpec)
            assert criterion.has_success_contract

            grade = GradeGate().grade_seed(seed)
            assert grade.grade is SeedGrade.A
            assert grade.may_run is True

            executor = create_runtime_verify_executor(tmp_dir)
            gated = await executor._apply_verify_gate(
                seed=seed,
                ac_index=0,
                result=ACExecutionResult(
                    ac_index=0,
                    ac_content=criterion.description,
                    success=True,
                ),
                session_id="quoted-pre-marker-test",
                execution_id="quoted-pre-marker-test",
            )

        assert gated.success is False
        assert gated.outcome is ACExecutionOutcome.FAILED
        assert gated.verify_gate_outcome is not None
        assert gated.verify_gate_outcome.passed is False
        assert expected_gate_error in (gated.error or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verify_command", _ADJACENT_SINGLE_QUOTED_VERIFY_COMMANDS)
    async def test_generate_preserves_adjacent_single_quoted_verify_payloads(
        self,
        verify_command: str,
    ) -> None:
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Adjacent shell quote remains payload | "
                f"verify: {verify_command} | artifacts: out.txt | expect: OK\n"
            )
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
        assert mock_adapter.complete.await_count == 1
        (criterion,) = result.value.acceptance_criteria
        assert isinstance(criterion, AcceptanceCriterionSpec)
        assert criterion.verify_command == verify_command
        assert criterion.expected_artifacts == ("out.txt",)
        assert criterion.output_assertion == "OK"
        grade = GradeGate().grade_seed(result.value)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

    @pytest.mark.asyncio
    async def test_stored_legacy_unquoted_backtick_case_survives_live_verify(self) -> None:
        """Legacy line contracts keep shell syntax inside backtick substitutions."""
        if not Path("/bin/sh").exists():
            pytest.skip("POSIX shell regression")
        verify_command = "printf '%s\\n' `case x in x | artifacts:) printf case-ok;; esac`"
        syntax = subprocess.run(
            ["/bin/sh", "-n", "-c", verify_command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stderr

        criterion = _parse_acceptance_criterion_contract(
            "AC: Legacy backtick case runs | "
            f"verify: {verify_command} | artifacts: NONE | expect: NONE"
        )
        assert criterion is not None
        assert criterion.verify_command == verify_command

        generator = SeedGenerator(llm_adapter=AsyncMock())
        requirements = generator._parse_extraction_response(create_valid_extraction_response())
        requirements["acceptance_criteria"] = (criterion,)
        seed = generator._build_seed(requirements, SeedMetadata(interview_id="legacy-backtick"))
        assert Seed.model_validate(seed.model_dump()) == seed
        grade = GradeGate().grade_seed(seed)
        assert grade.grade is SeedGrade.A
        assert grade.may_run is True

        with tempfile.TemporaryDirectory() as tmp_dir:
            gated = await create_runtime_verify_executor(tmp_dir)._apply_verify_gate(
                seed=seed,
                ac_index=0,
                result=ACExecutionResult(
                    ac_index=0,
                    ac_content=criterion.description,
                    success=True,
                ),
                session_id="legacy-backtick-test",
                execution_id="legacy-backtick-test",
            )

        assert gated.success is True
        assert gated.verify_gate_outcome is not None
        assert gated.verify_gate_outcome.passed is True
        assert gated.verify_gate_outcome.output_tail == "case-ok\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("verify_command", "expected_output"),
        (
            (
                """printf '%s\\n' "$(case y in (x) printf no;; """
                '''(y | artifacts:) printf yes;; esac)"''',
                "yes\n",
            ),
            (
                "printf '%s\\n' $(case z in (x) :;; (''esac) printf no;; "
                "(z | artifacts:) printf yes;; esac)",
                "yes\n",
            ),
            (
                "case y in x) printf no;; y | artifacts:) printf '%s\\n' yes;; esac",
                "yes\n",
            ),
            (
                "while case y in (x) true;; (y | artifacts:) false;; esac; do :; done; printf ok",
                "ok",
            ),
            (
                "if case y in (x) true;; (y | verify:) true;; esac; then printf ok; fi",
                "ok",
            ),
            ("f() { case y in (y | artifacts:) printf yes;; esac; }; f", "yes"),
            ("f() case y in (y | artifacts:) printf yes;; esac; f", "yes"),
            ("(case y in (y | verify:) printf yes;; esac)", "yes"),
            ("case esac in (esac'') printf yes;; esac", "yes"),
            (
                "for case in y; do printf '%s' 'ok | artifacts: literal'; done",
                "ok | artifacts: literal",
            ),
            ("for case in y; do :; done; printf ok", "ok"),
        ),
    )
    async def test_extracted_case_survives_seed_grade_and_live_verify(
        self, verify_command: str, expected_output: str
    ) -> None:
        """Top-level and substituted case bodies survive through the live shell gate."""
        if not Path("/bin/sh").exists():
            pytest.skip("POSIX shell regression")
        syntax = subprocess.run(
            ["/bin/sh", "-n", "-c", verify_command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stderr

        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    create_valid_extraction_response(
                        acceptance_criteria=(
                            "\nAC: Dollar case runs | "
                            f"verify: {verify_command} | artifacts: NONE | expect: NONE\n"
                        )
                    )
                )
            )
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            generated = await generator.generate(
                create_interview_state_with_rounds(), create_low_ambiguity_score()
            )
            assert generated.is_ok
            seed = generated.value
            assert Seed.model_validate(seed.model_dump()) == seed
            grade = GradeGate().grade_seed(seed)
            assert grade.grade is SeedGrade.A
            assert grade.may_run is True

            gated = await create_runtime_verify_executor(tmp_dir)._apply_verify_gate(
                seed=seed,
                ac_index=0,
                result=ACExecutionResult(
                    ac_index=0,
                    ac_content=seed.acceptance_criteria[0].description,
                    success=True,
                ),
                session_id="dollar-case-test",
                execution_id="dollar-case-test",
            )

        assert gated.success is True
        assert gated.verify_gate_outcome is not None
        assert gated.verify_gate_outcome.passed is True
        assert gated.verify_gate_outcome.output_tail == expected_output

    @pytest.mark.parametrize(
        "verify_command",
        (
            "printf '%s\\n' $(case y in x) printf no;; y | artifacts:) printf yes;; esac)",
            """printf '%s\\n' "$(case y in (x) printf no;; """
            '''(y | artifacts:) (printf nested);; esac)"''',
            """printf '%s\\n' "$(case y in (x) printf no;; (y | artifacts:) """
            '''unset z; printf '%s' "${z:-$(printf nested)}";; esac)"''',
            """printf '%s\\n' "$(case y in (x) printf no;; (y | artifacts:) """
            '''printf '%s' `printf backtick`;; esac)"''',
            '''printf '%s\\n' "$(case y in (y) printf '%s' esac;; esac)"''',
            """printf '%s\\n' "$(case y in (y) case z in """
            '''(z | artifacts:) printf nested;; esac;; esac)"''',
            "case y in (y) case z in (z | artifacts:) printf nested;; esac;; esac",
            "case z in (x) :;; (''esac) printf no;; (z | artifacts:) printf yes;; esac",
            "printf '%s\\n' $(case z in (x) :;; (''esac) printf no;; "
            "(z | artifacts:) printf yes;; esac)",
            r"printf '%s\n' $(case esac in (\esac) printf escaped;; esac)",
            "printf '%s\\n' ''case",
            "printf '%s\\n' ''if ''while ''until ''for ''then ''do ''case ''esac",
            "printf '%s\\n' case'' esac'' if-x while: until_name then.word do/value",
            """printf '%s\\n' "$(f() { case y in """
            '''(y | artifacts:) printf nested;; esac; }; f)"''',
            "until case y in (y | artifacts:) false;; esac; do :; done",
            "for x in y; do case $x in (y | artifacts:) :;; esac; done",
            "for x; do case $x in (y | artifacts:) :;; esac; done",
            "for 'case' in y; do case $case in (y | artifacts:) :;; esac; done",
            r"for \case in y; do case $case in (y | artifacts:) :;; esac; done",
            "for case in y; do for esac in z; do case $case in "
            "(y | artifacts:) :;; esac; done; done",
            "{ case y in (y | artifacts:) :;; esac; }",
            "true && case y in (y | artifacts:) :;; esac",
            "false || case y in (y | artifacts:) :;; esac",
            "printf x | case x in (x | artifacts:) :;; esac",
            '''printf '%s\\n' "$(printf '%s' ordinary)"''',
        ),
    )
    def test_dollar_case_boundaries_preserve_shell_frames(self, verify_command: str) -> None:
        criterion = _parse_acceptance_criterion_contract(
            f"AC: Dollar case boundary | verify: {verify_command} | artifacts: NONE | expect: NONE"
        )

        assert criterion is not None
        assert criterion.verify_command == verify_command

    @pytest.mark.parametrize(
        "verify_command",
        (
            "printf $(case y in x) printf hidden;; y | artifacts:) printf hidden;;",
            "printf $(case y in x) printf hidden;; y | artifacts:) printf $(unclosed",
            "printf $(case y in x) printf hidden;; y | artifacts:) printf `unclosed",
            "case y in x) printf hidden;; y | artifacts:) printf hidden;;",
            "for in y; do :; done",
        ),
    )
    def test_unclosed_case_frames_cannot_hide_outer_fields(self, verify_command: str) -> None:
        with pytest.raises(ValueError, match="Unterminated quoted or escaped"):
            _parse_acceptance_criterion_contract(
                f"AC: Dollar case boundary | verify: {verify_command} | "
                "artifacts: NONE | expect: NONE"
            )

    @pytest.mark.parametrize(
        "verify_command",
        (
            '''printf '%s\n' "`case x in x | artifacts:) printf case-ok;; esac`"''',
            r"""printf '%s\n' `printf '%s' \`case x in x | artifacts:) printf nested;; esac\``""",
            r"printf '%s\n' '` | artifacts: literal'",
        ),
    )
    def test_legacy_backtick_boundaries_preserve_inner_reserved_pipes(
        self, verify_command: str
    ) -> None:
        if Path("/bin/sh").exists():
            syntax = subprocess.run(
                ["/bin/sh", "-n", "-c", verify_command],
                check=False,
                capture_output=True,
                text=True,
            )
            assert syntax.returncode == 0, syntax.stderr
        criterion = _parse_acceptance_criterion_contract(
            f"AC: Backtick boundary | verify: {verify_command} | artifacts: NONE | expect: NONE"
        )

        assert criterion is not None
        assert criterion.verify_command == verify_command

    def test_legacy_unclosed_backtick_cannot_hide_outer_fields(self) -> None:
        with pytest.raises(ValueError, match="Unterminated quoted or escaped"):
            _parse_acceptance_criterion_contract(
                "AC: Backtick boundary | verify: printf `case x in x | artifacts:) "
                "printf hidden;; esac | artifacts: NONE | expect: NONE"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("verify_command", "expected_output"),
        (
            (
                r"printf '%s\n' 'trailing\' | grep -F 'trailing\'",
                "trailing\\\n",
            ),
            (
                r"printf '%s\n' foo're| artifacts: literal' | "
                r"grep -F 'foore| artifacts: literal'",
                "foore| artifacts: literal\n",
            ),
            (r"printf '%s\n' foo're' | grep -Fx foore", "foore\n"),
            ("true # don't care", ""),
            ("true;# user's note", ""),
            (
                r"printf '%s\n' foo#don't' | grep -Fx 'foo#dont'",
                "foo#dont\n",
            ),
            (
                r"printf '%s\n' foo\ #\| expect: forged",
                "foo #|\nexpect:\nforged\n",
            ),
            (
                r"printf '%s\n' foo\;#\| expect: forged",
                "foo;#|\nexpect:\nforged\n",
            ),
            (
                r"printf '%s\n' foo\&#\| expect: forged",
                "foo&#|\nexpect:\nforged\n",
            ),
            (
                r"printf '%s\n' foo\|#\| expect: forged",
                "foo|#|\nexpect:\nforged\n",
            ),
            (
                r"printf '%s\n' foo\(#\| expect: forged",
                "foo(#|\nexpect:\nforged\n",
            ),
            (
                r"printf '%s\n' foo\)#\| expect: forged",
                "foo)#|\nexpect:\nforged\n",
            ),
            (
                r'''printf '%s\n' "$(printf "%s | artifacts: literal" hi)"''',
                "hi | artifacts: literal\n",
            ),
            (
                r'''printf '%s\n' "$(printf '%s' "$(printf '%s | verify: literal' hi)")"''',
                "hi | verify: literal\n",
            ),
            (
                r'''unset a b; printf '%s\n' "${a:-"${b:-nested | artifacts: literal}"}"''',
                "nested | artifacts: literal\n",
            ),
            (
                r'''printf '%s\n' "$(printf '%s' escaped\))"''',
                "escaped)\n",
            ),
            (
                '''printf '%s\\n' "`printf "%s | artifacts: literal" hi`"''',
                "hi | artifacts: literal\n",
            ),
            (
                '''printf '%s\\n' "`printf '%s' "$(printf '%s | verify: literal' hi)"`"''',
                "hi | verify: literal\n",
            ),
            (
                '''printf '%s\\n' "`printf '%s' '\\`'`"''',
                "`\n",
            ),
            (
                "case x in x | artifacts:) printf '%s\\n' case-ok;; esac",
                "case-ok\n",
            ),
            (
                "f() { printf '%s\\n' 'function | verify: literal'; }; f",
                "function | verify: literal\n",
            ),
            ("python -c 'print(1 << 2)'", "4\n"),
            ("printf '%s\\n' 'ordinary << comparison text'", "ordinary << comparison text\n"),
            (
                '''printf "%s\\n" "$(python -c 'print(1 << 2)')"''',
                "4\n",
            ),
            (r'''printf '%s\n' "plain <<EOF"''', "plain <<EOF\n"),
            (r'''printf '%s\n' "\`literal <<EOF"''', "`literal <<EOF\n"),
            (r"""printf '%s\n' $((1 << 2))""", "4\n"),
            (r'''printf '%s\n' "$((1 << 2))"''', "4\n"),
            (
                r'''unset x; printf '%s\n' "${x:-literal << text}"''',
                "literal << text\n",
            ),
        ),
    )
    async def test_generate_preserves_posix_single_quote_tokens_through_live_verify(
        self,
        verify_command: str,
        expected_output: str,
    ) -> None:
        """Concrete `/bin/sh` forms survive extraction, Seed grading, and execution.

        These expand the markdown-truncated review examples into complete
        commands: one ends a single-quoted value with a literal backslash; the
        other begins an adjacent quoted segment with contraction-like ``re``.
        Comment forms prove apostrophes are inert after a token-boundary ``#``;
        the token-internal control proves ``#`` does not always start a comment.
        Escaped whitespace and control operators prove raw boundary spelling
        cannot forge comment state or promote an escaped pipe into a DSL marker.
        Balanced command/parameter expansions prove nested quotes, escapes,
        delimiters, and reserved-looking pipes stay inside the verify command.
        """
        if not Path("/bin/sh").exists():
            pytest.skip("POSIX shell regression")
        syntax = subprocess.run(
            ["/bin/sh", "-n", "-c", verify_command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stderr
        mock_adapter = AsyncMock()
        extraction_response = create_valid_extraction_response(
            acceptance_criteria=json.dumps(
                [
                    {
                        "description": "POSIX shell quoting survives",
                        "verify": verify_command,
                        "artifacts": "NONE",
                        "expect": "NONE",
                    }
                ]
            )
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(extraction_response))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(
                create_interview_state_with_rounds(),
                create_low_ambiguity_score(),
            )

            assert result.is_ok
            (criterion,) = result.value.acceptance_criteria
            assert isinstance(criterion, AcceptanceCriterionSpec)
            assert criterion.verify_command == verify_command
            assert Seed.model_validate(result.value.model_dump()) == result.value
            grade = GradeGate().grade_seed(result.value)
            assert grade.grade is SeedGrade.A
            assert grade.may_run is True

            gated = await create_runtime_verify_executor(tmp_dir)._apply_verify_gate(
                seed=result.value,
                ac_index=0,
                result=ACExecutionResult(
                    ac_index=0,
                    ac_content=criterion.description,
                    success=True,
                ),
                session_id="posix-quote-test",
                execution_id="posix-quote-test",
            )

            assert gated.success is True
            assert gated.verify_gate_outcome is not None
            assert gated.verify_gate_outcome.passed is True
            assert gated.verify_gate_outcome.output_tail == expected_output

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "verify_invocation",
        (
            "verify --mode strict",
            'verify "$mode"',
            "verify 'strict'",
            r"verify \--mode",
        ),
    )
    async def test_generate_preserves_reserved_command_name_in_verify_pipeline(
        self,
        verify_invocation: str,
    ) -> None:
        """Arguments after a complete reserved command token remain payload."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()
        verify_command = f"verify(){{ cat; }}; mode=strict; printf READY | {verify_invocation}"
        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Verify pipeline succeeds | "
                f"verify: {verify_command} | artifacts: NONE | expect: READY\n"
            )
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
            assert mock_adapter.complete.await_count == 1
            (criterion,) = result.value.acceptance_criteria
            assert isinstance(criterion, AcceptanceCriterionSpec)
            assert criterion.verify_command == verify_command
            assert criterion.expected_artifacts == ()
            assert criterion.output_assertion == "READY"
            assert Seed.model_validate(result.value.model_dump()) == result.value

            grade = GradeGate().grade_seed(result.value)
            assert grade.grade is SeedGrade.A
            assert grade.may_run is True

            executor = create_runtime_verify_executor(tmp_dir)
            gated = await executor._apply_verify_gate(
                seed=result.value,
                ac_index=0,
                result=ACExecutionResult(
                    ac_index=0,
                    ac_content=criterion.description,
                    success=True,
                ),
                session_id="reserved-pipeline-test",
                execution_id="reserved-pipeline-test",
            )

        assert gated.success is True
        assert gated.verify_gate_outcome is not None
        assert gated.verify_gate_outcome.passed is True

    @pytest.mark.asyncio
    async def test_generate_normalizes_output_assertion_condition_phrases(self) -> None:
        """Generated contract assertions must be literal stdout, not status prose."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: CLI exits successfully | verify: python -m app | artifacts: NONE | "
                "expect: exit code 0\n"
                "AC: Test summary is visible | verify: pytest -q | artifacts: NONE | "
                "expect: 5 passed\n"
            )
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
            first, second = result.value.acceptance_criteria
            assert isinstance(first, AcceptanceCriterionSpec)
            assert first.output_assertion is None
            assert first.to_seed_value() == {
                "description": "CLI exits successfully",
                "semantic_ac_key": first.semantic_ac_key,
                "verify_command": "python -m app",
            }
            assert isinstance(second, AcceptanceCriterionSpec)
            assert second.output_assertion == "5 passed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "heredoc_command",
        (
            r"cat <<\EOF",
            r"cat <<-\EOF",
            'cat <<E"OF"',
            "cat <<'E'OF",
            r'''printf '%s' "`cat <<\EOF`"''',
            r"""printf '%s\n' $(( $(cat <<\EOF; printf 1) + 1 ))""",
            r"""unset x; printf '%s' ${x:-$(cat <<\EOF)}""",
            r"""unset x; printf '%s' ${x:-`cat <<\EOF`}""",
            r'''unset x; printf '%s' "${x:-$(cat <<\EOF)}"''',
            r'''printf '%s\n' "$(( $(cat <<\EOF; printf 1) + 1 ))"''',
        ),
    )
    async def test_generate_retries_when_verify_command_uses_heredoc(
        self, heredoc_command: str
    ) -> None:
        """Single-line AC contracts must reject heredoc commands before Seed build."""
        syntax = subprocess.run(
            ["/bin/sh", "-n", "-c", heredoc_command],
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stderr
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bad_response = create_valid_extraction_response(
            acceptance_criteria=json.dumps(
                [
                    {
                        "description": "Import check prints OK",
                        "verify": heredoc_command,
                        "artifacts": ["hello.py"],
                        "expect": "NONE",
                    }
                ]
            )
        )
        repaired_response = create_valid_extraction_response(
            acceptance_criteria=(
                "\n"
                "AC: Import check prints OK | "
                'verify: python -c "from hello import greet; '
                "assert greet('Alice') == 'Hello, Alice'; print('OK')\" | "
                "artifacts: hello.py | expect: OK\n"
            )
        )
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(bad_response)),
                Result.ok(create_mock_completion_response(repaired_response)),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert mock_adapter.complete.await_count == 2
            (criterion,) = result.value.acceptance_criteria
            assert isinstance(criterion, AcceptanceCriterionSpec)
            assert criterion.verify_command is not None
            assert "python -c" in criterion.verify_command

    @pytest.mark.asyncio
    async def test_generate_rejects_heredoc_on_initial_and_retry_extraction(self) -> None:
        mock_adapter = AsyncMock()
        bad_responses = [
            create_valid_extraction_response(
                acceptance_criteria=json.dumps(
                    [
                        {
                            "description": "Output exists",
                            "verify": command,
                            "artifacts": "NONE",
                            "expect": "NONE",
                        }
                    ]
                )
            )
            for command in (r"cat <<\EOF", r"cat <<-\EOF")
        ]
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(response)) for response in bad_responses
            ]
        )
        generator = SeedGenerator(llm_adapter=mock_adapter)

        result = await generator.generate(
            create_interview_state_with_rounds(), create_low_ambiguity_score()
        )

        assert result.is_err
        assert mock_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_generate_extracts_ontology_schema(self) -> None:
        """SeedGenerator extracts ontology schema with fields."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        extraction_response = create_valid_extraction_response(
            ontology_name="TaskManager",
            ontology_description="Domain model for task management",
            ontology_fields=(
                '[{"name": "tasks", "type": "array", "description": "List of tasks"},'
                ' {"name": "status", "type": "string", "description": "Task status"}]'
            ),
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
            evaluation_principles=(
                '[{"name": "completeness", "description": "All requirements met",'
                ' "weight": 0.5},'
                ' {"name": "quality", "description": "High quality", "weight": 0.3}]'
            )
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
            exit_conditions=(
                '[{"name": "done", "description": "All done", "criteria": "100% pass"},'
                ' {"name": "timeout", "description": "Max time", "criteria": "10 iterations"}]'
            )
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


class TestSeedGeneratorInterviewContext:
    """Test SeedGenerator._build_interview_context."""

    def test_context_uses_prompt_safe_initial_context_summary(self) -> None:
        """_build_interview_context avoids oversized raw initial_context."""
        mock_adapter = AsyncMock()
        generator = SeedGenerator(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_large_context",
            initial_context=("A" * 4_000) + "TAIL_MARKER",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                user_response="Short project summary",
            )
        )

        context = generator._build_interview_context(state)

        assert "Short project summary" in context
        assert "TAIL_MARKER" not in context
        assert INITIAL_CONTEXT_SUMMARY_QUESTION not in context

    def test_context_caps_oversized_initial_context_summary(self) -> None:
        """_build_interview_context does not serialize oversized summary rounds raw."""
        mock_adapter = AsyncMock()
        generator = SeedGenerator(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_large_summary",
            initial_context=("A" * 4_000) + "RAW_TAIL",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                user_response=("B" * 4_000) + "SUMMARY_TAIL",
            )
        )

        context = generator._build_interview_context(state)

        assert "Context truncated for prompt safety" in context
        assert "RAW_TAIL" not in context
        assert "SUMMARY_TAIL" not in context
        assert INITIAL_CONTEXT_SUMMARY_QUESTION not in context


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
        """SeedGenerator returns error for malformed LLM response after retries."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        # Missing required fields — both initial and retry return bad data
        bad_response: Result = Result.ok(create_mock_completion_response("INVALID: missing fields"))
        mock_adapter.complete = AsyncMock(side_effect=[bad_response, bad_response])

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_err
            assert mock_adapter.complete.call_count == 2


class TestSeedGeneratorRobustParsing:
    """Test SeedGenerator handles non-ideal LLM responses."""

    @pytest.mark.asyncio
    async def test_parse_response_with_conversational_preamble(self) -> None:
        """Parser handles LLM response with prose before structured output."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        response_with_preamble = (
            "Based on the interview, here are the extracted requirements:\n\n"
            + create_valid_extraction_response()
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(response_with_preamble))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.goal == "Build a CLI task manager with project grouping"

    @pytest.mark.asyncio
    async def test_parse_response_with_markdown_code_block(self) -> None:
        """Parser handles structured output wrapped in markdown code blocks."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        response_with_markdown = (
            "Here are the extracted requirements:\n\n```\n"
            + create_valid_extraction_response()
            + "\n```"
        )
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(response_with_markdown))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert result.value.goal == "Build a CLI task manager with project grouping"

    @pytest.mark.asyncio
    async def test_extraction_retries_on_parse_failure(self) -> None:
        """Extraction retries once with clarification prompt on parse failure."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        conversational: Result = Result.ok(
            create_mock_completion_response(
                "Let me explore the codebase to provide accurate context."
            )
        )
        valid: Result = Result.ok(
            create_mock_completion_response(create_valid_extraction_response())
        )
        mock_adapter.complete = AsyncMock(side_effect=[conversational, valid])

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_ok
            assert mock_adapter.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_extraction_fails_after_max_retries(self) -> None:
        """Extraction fails gracefully after all retry attempts exhausted."""
        mock_adapter = AsyncMock()
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        bad: Result = Result.ok(
            create_mock_completion_response("I'd be happy to help! Let me think about this...")
        )
        mock_adapter.complete = AsyncMock(side_effect=[bad, bad])

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )

            result = await generator.generate(state, low_ambiguity)

            assert result.is_err
            assert "after 2 attempts" in str(result.error)
            assert mock_adapter.complete.call_count == 2


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

    @pytest.mark.asyncio
    async def test_load_seed_rejects_persisted_legacy_multibyte_path_overflow(
        self,
        sample_seed: Seed,
    ) -> None:
        """Legacy replay fails explicitly when a persisted path is unmaterializable."""
        artifact = "/".join(("😀" * 62, "a" * 7))
        assert len(artifact.encode("utf-8")) == MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES + 1
        seed_data = sample_seed.to_dict()
        seed_data["acceptance_criteria"] = [
            {
                "description": "Persisted artifact exists",
                "expected_artifacts": [artifact],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "legacy_oversized_seed.yaml"
            file_path.write_text(yaml.safe_dump(seed_data), encoding="utf-8")

            result = await load_seed(file_path)

        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "canonical path longer than 255 portable filesystem bytes" in result.error.message

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


class TestAcceptanceCriteriaGranularityContract:
    """Guard every surface that carries the AC granularity contract — the
    extraction prompt, the retry prompt, the seed-architect contract, and the QA
    quality bar — against silent loss of it (the fix for Fable-5-style
    over-atomization at seed-gen time).

    The contract hands the model a distinction to reason with — a criterion is a
    state of the finished work, an implementation step is a means of reaching it
    — rather than a quantity to comply with. These tests hold that shape: the
    distinction must be stated, and the rule must carry no number, because a
    number is satisfiable without making the judgment the rule exists to elicit.
    """

    @staticmethod
    def _granularity_rule(prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("ACCEPTANCE_CRITERIA rule:"):
                return line
        raise AssertionError("prompt carries no ACCEPTANCE_CRITERIA granularity rule")

    def test_extraction_user_prompt_carries_granularity_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=AsyncMock(),
                output_dir=Path(tmp_dir) / "seeds",
            )
            prompt = generator._build_extraction_user_prompt("Q: goal?\nA: build a thing")

        rule = self._granularity_rule(prompt).lower()
        assert "acceptance criterion" in rule
        assert "implementation step" in rule
        # A criterion is weighed against its siblings, never against a quantity,
        # so the rule states no count of any kind.
        assert not any(char.isdigit() for char in rule)

    def test_extraction_user_prompt_requests_structured_ac_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=AsyncMock(),
                output_dir=Path(tmp_dir) / "seeds",
            )
            prompt = generator._build_extraction_user_prompt("Q: goal?\nA: build a thing")

        assert 'ACCEPTANCE_CRITERIA: [{"description":' in prompt
        assert '"verify": "<single-line command or NONE>"' in prompt
        assert '"artifacts": ["<path>"]' in prompt
        assert "heredoc" in prompt.lower()
        assert "python -c" in prompt
        assert "combined stdout and stderr" in prompt
        assert "JSON array of exact portable file or directory paths" in prompt
        assert "top-level path containing spaces with `./`" in prompt
        assert "ACCEPTANCE_CRITERIA: <criterion 1> | <criterion 2>" not in prompt

        retry_prompt = generator._build_retry_prompt("Q: goal?", "bad", "parse error")
        assert "combined stdout and stderr" in retry_prompt
        assert "JSON array of exact portable file or directory paths" in retry_prompt
        assert "top-level path containing spaces with `./`" in retry_prompt

    def test_extraction_retry_prompt_carries_granularity_contract(self) -> None:
        """The retry prompt is a parallel surface for the same contract: a parse
        failure must not be the moment the model stops being told what a
        criterion is, nor the crack a count creeps back in through."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=AsyncMock(),
                output_dir=Path(tmp_dir) / "seeds",
            )
            prompt = generator._build_retry_prompt(
                "Q: goal?\nA: build a thing",
                failed_response="not parseable",
                error="missing GOAL",
            )

        rule = self._granularity_rule(prompt).lower()
        assert "acceptance criterion" in rule
        assert "implementation step" in rule
        assert not any(char.isdigit() for char in rule)

    def test_seed_qa_quality_bar_carries_granularity_contract(self) -> None:
        """The QA quality bar is where the judgment actually lands once the
        deterministic gate stopped counting, so it carries the same distinction
        and, like the prompts, states no quantity."""
        quality_bar = next(
            line
            for line in Path("skills/seed/SKILL.md").read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("quality_bar:")
        ).lower()

        assert "acceptance_criteria" in quality_bar
        assert "implementation step" in quality_bar
        assert "siblings" in quality_bar
        assert not any(char.isdigit() for char in quality_bar)

    def test_seed_architect_agent_prompt_carries_granularity_contract(self) -> None:
        from ouroboros.agents.loader import load_agent_prompt

        system_prompt = load_agent_prompt("seed-architect")
        assert "**Granularity contract (read carefully):**" in system_prompt
        contract = system_prompt.split("**Granularity contract (read carefully):**", 1)[1]
        contract = contract.split("### ", 1)[0].lower()
        assert "acceptance criterion" in contract
        assert "implementation step" in contract
        assert not any(char.isdigit() for char in contract)
        assert "heredoc" in system_prompt.lower()
        assert "python -c" in system_prompt
        assert (
            "exact portable file or directory path relative to the run workspace" in system_prompt
        )
        assert "schema v2 outputs" in system_prompt
        assert "artifacts: NONE" in system_prompt
        assert "concrete `verify` command" in system_prompt

    def test_seed_architect_agent_prompt_uses_strict_json_ac_contract(self) -> None:
        from ouroboros.agents.loader import load_agent_prompt

        system_prompt = load_agent_prompt("seed-architect")
        assert 'ACCEPTANCE_CRITERIA: [{"description":' in system_prompt
        assert "one field on one line" in system_prompt
        assert "never emit nested `AC:` lines" in system_prompt
        assert "\nAC: <description>" not in system_prompt

        generator = SeedGenerator(llm_adapter=AsyncMock())
        documented = next(
            line
            for line in system_prompt.splitlines()
            if line.startswith('ACCEPTANCE_CRITERIA: [{"description":')
        )
        response = create_valid_extraction_response(
            acceptance_criteria=documented.removeprefix("ACCEPTANCE_CRITERIA: ")
        )
        parsed = generator._parse_extraction_response(response)
        assert parsed["acceptance_criteria"]

        multi_example = next(
            line.removeprefix("Multi-artifact example: `").removesuffix("`")
            for line in system_prompt.splitlines()
            if line.startswith("Multi-artifact example: `")
        )
        multi_response = create_valid_extraction_response(acceptance_criteria=multi_example)
        multi_parsed = generator._parse_extraction_response(multi_response)
        (criterion,) = multi_parsed["acceptance_criteria"]
        assert criterion.expected_artifacts == ("dist/app", "docs/User Guide.md")


class TestObjectArrayExtractionContract:
    """#1729 slice 2: EVALUATION_PRINCIPLES / EXIT_CONDITIONS as JSON object arrays.

    Extraction (strict) demands single-line JSON arrays of objects so colons
    and pipes inside names, descriptions, and criteria survive as data; stored
    legacy requirements (lenient) keep the historical colon/pipe split and
    never raise.
    """

    def test_strict_parses_json_object_arrays_with_delimiters_in_data(self) -> None:
        principles = _parse_evaluation_principles(
            '[{"name": "ratio", "description": "Keep a 1:1 request:response mapping | no drops",'
            ' "weight": 0.4}]',
            strict=True,
        )
        assert principles == (
            EvaluationPrinciple(
                name="ratio",
                description="Keep a 1:1 request:response mapping | no drops",
                weight=0.4,
            ),
        )
        conditions = _parse_exit_conditions(
            '[{"name": "done", "description": "All pass",'
            ' "criteria": "pytest exit 0: all green | no skips"}]',
            strict=True,
        )
        assert conditions == (
            ExitCondition(
                name="done",
                description="All pass",
                evaluation_criteria="pytest exit 0: all green | no skips",
            ),
        )

    def test_strict_accepts_evaluation_criteria_key_alias(self) -> None:
        conditions = _parse_exit_conditions(
            '[{"name": "done", "description": "All pass",'
            ' "evaluation_criteria": "coverage >= 80%"}]',
            strict=True,
        )
        assert conditions[0].evaluation_criteria == "coverage >= 80%"

    def test_strict_defaults_and_clamps_weight(self) -> None:
        principles = _parse_evaluation_principles(
            '[{"name": "a", "description": "no weight"},'
            ' {"name": "b", "description": "too big", "weight": 1.5},'
            ' {"name": "c", "description": "negative", "weight": -0.5}]',
            strict=True,
        )
        assert [p.weight for p in principles] == [1.0, 1.0, 0.0]

    def test_strict_rejects_blank_object_array_fields(self) -> None:
        with pytest.raises(ValueError, match="EVALUATION_PRINCIPLES"):
            _parse_evaluation_principles("", strict=True)
        with pytest.raises(ValueError, match="EXIT_CONDITIONS"):
            _parse_exit_conditions("   ", strict=True)

        assert _parse_evaluation_principles("[]", strict=True) == ()
        assert _parse_exit_conditions("[]", strict=True) == ()

    def test_strict_distinguishes_omitted_weight_from_explicit_null(self) -> None:
        omitted = _parse_evaluation_principles(
            '[{"name": "a", "description": "omitted weight"}]',
            strict=True,
        )
        assert omitted[0].weight == 1.0

        with pytest.raises(ValueError, match="weight"):
            _parse_evaluation_principles(
                '[{"name": "a", "description": "null weight", "weight": null}]',
                strict=True,
            )

    def test_strict_rejects_legacy_pipe_lines(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            _parse_evaluation_principles(
                "completeness:All requirements implemented:0.4 | quality:Meets standards:0.3",
                strict=True,
            )
        with pytest.raises(ValueError, match="JSON array"):
            _parse_exit_conditions(
                "all_criteria_met:All acceptance criteria pass:100% criteria satisfied",
                strict=True,
            )

    def test_strict_rejects_malformed_entries(self) -> None:
        malformed_principles = (
            '["not an object"]',
            '[{"description": "missing name"}]',
            '[{"name": "  ", "description": "blank name"}]',
            '[{"name": "x", "description": ""}]',
            '[{"name": "x", "description": "d", "weight": "high"}]',
            '[{"name": "x", "description": "d", "weight": true}]',
            '[{"name": "x", "description": "d", "weight": null}]',
        )
        for malformed in malformed_principles:
            with pytest.raises(ValueError, match="EVALUATION_PRINCIPLES"):
                _parse_evaluation_principles(malformed, strict=True)
        malformed_conditions = (
            "[42]",
            '[{"name": "x", "description": "d"}]',
            '[{"name": "x", "description": "d", "criteria": "   "}]',
        )
        for malformed in malformed_conditions:
            with pytest.raises(ValueError, match="EXIT_CONDITIONS"):
                _parse_exit_conditions(malformed, strict=True)

    def test_lenient_keeps_legacy_colon_pipe_split(self) -> None:
        principles = _parse_evaluation_principles(
            "completeness:All requirements implemented:0.4 | quality:Meets standards:bad | solo"
        )
        assert principles == (
            EvaluationPrinciple(
                name="completeness", description="All requirements implemented", weight=0.4
            ),
            EvaluationPrinciple(name="quality", description="Meets standards", weight=1.0),
        )
        conditions = _parse_exit_conditions(
            "all_met:All pass:100% satisfied:with margin | too:short"
        )
        assert conditions == (
            ExitCondition(
                name="all_met",
                description="All pass",
                evaluation_criteria="100% satisfied:with margin",
            ),
        )

    def test_lenient_skips_malformed_legacy_principle_entries_without_raising(self) -> None:
        assert _parse_evaluation_principles(
            ":description:0.5 | complete::0.4 | valid:Description:0.25"
        ) == (EvaluationPrinciple(name="valid", description="Description", weight=0.25),)

    def test_lenient_nonfinite_tokens_all_default_to_one(self) -> None:
        """#1766: every non-standard constant defaults to 1.0 in lenient mode.

        The token marker must be recognized before numeric conversion so a
        stored -Infinity literal is not conflated with genuine negative
        overflow (which keeps saturating to 0.0 by sign).
        """
        for token in ("NaN", "Infinity", "-Infinity"):
            principles = _parse_evaluation_principles(
                f'[{{"name": "x", "description": "d", "weight": {token}}}]'
            )
            assert principles[0].weight == 1.0, f"token {token} did not default to 1.0"
        overflow = _parse_evaluation_principles(
            '[{"name": "x", "description": "d", "weight": -1e999}]'
        )
        assert overflow[0].weight == 0.0

    def test_weights_beyond_interpreter_digit_limit_never_raise(self) -> None:
        huge = "9" * 5000
        lenient = _parse_evaluation_principles(
            f'[{{"name": "x", "description": "d", "weight": {huge}}}]'
        )
        assert lenient[0].weight == 1.0
        strict = _parse_evaluation_principles(
            f'[{{"name": "x", "description": "d", "weight": {huge}}}]', strict=True
        )
        assert strict[0].weight == 1.0
        negative = _parse_evaluation_principles(
            f'[{{"name": "x", "description": "d", "weight": -{huge}}}]', strict=True
        )
        assert negative[0].weight == 0.0

    def test_overflowed_negative_weights_keep_their_sign(self) -> None:
        legacy = _parse_evaluation_principles("x:d:-1e999 | y:d:1e999")
        assert [p.weight for p in legacy] == [0.0, 1.0]
        json_neg = _parse_evaluation_principles(
            '[{"name": "x", "description": "d", "weight": -1e999}]'
        )
        assert json_neg[0].weight == 0.0
        json_neg_strict = _parse_evaluation_principles(
            '[{"name": "x", "description": "d", "weight": -1e999}]', strict=True
        )
        assert json_neg_strict[0].weight == 0.0

    def test_context_refs_strict_parses_json_with_colons_in_paths(self) -> None:
        refs = _parse_context_references(
            '[{"path": "C:/repos/api", "role": "primary",'
            ' "summary": "API layer: handlers | middleware"}]',
            strict=True,
        )
        assert refs == (
            ContextReference(
                path="C:/repos/api",
                role="primary",
                summary="API layer: handlers | middleware",
            ),
        )
        no_summary = _parse_context_references(
            '[{"path": "/repo/lib", "role": "reference"}]', strict=True
        )
        assert no_summary[0].summary == ""

    def test_context_refs_strict_rejects_legacy_and_malformed(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            _parse_context_references("/repo/api:primary:API layer", strict=True)
        with pytest.raises(ValueError, match="JSON array"):
            _parse_context_references("", strict=True)
        assert _parse_context_references("[]", strict=True) == ()
        malformed = (
            '["not an object"]',
            '[{"role": "primary"}]',
            '[{"path": "/repo", "role": "  "}]',
            '[{"path": "/repo", "role": "primary", "summary": 42}]',
        )
        for case in malformed:
            with pytest.raises(ValueError, match="CONTEXT_REFERENCES"):
                _parse_context_references(case, strict=True)

    def test_context_refs_lenient_keeps_legacy_split_and_never_raises(self) -> None:
        legacy = _parse_context_references(
            "/repo/api:primary:API layer: v2 | /repo/lib:reference | :primary:bad | x"
        )
        assert legacy == (
            ContextReference(path="/repo/api", role="primary", summary="API layer: v2"),
            ContextReference(path="/repo/lib", role="reference", summary=""),
        )
        stored_json = _parse_context_references(
            '[{"path": "/a", "role": "primary", "summary": "s | t"}, {"role": "x"}, 7]'
        )
        assert len(stored_json) == 1
        assert stored_json[0].summary == "s | t"
        # Bracket-led malformed JSON falls back to the legacy split (junk
        # tolerated, never raises) — same contract as the other parsers.
        assert isinstance(_parse_context_references('[{"path":]'), tuple)
        assert _parse_context_references(None) == ()
        model = ContextReference(path="/m", role="reference", summary="")
        assert _parse_context_references([model]) == (model,)

    def test_ontology_strict_parses_json_object_arrays(self) -> None:
        fields = _parse_ontology_fields(
            '[{"name": "ratio", "type": "string",'
            ' "description": "A 1:1 request:response mapping | primary"}]',
            strict=True,
        )
        assert fields == (
            OntologyField(
                name="ratio",
                field_type="string",
                description="A 1:1 request:response mapping | primary",
            ),
        )
        aliased = _parse_ontology_fields(
            '[{"name": "n", "field_type": "number", "description": "d", "required": false}]',
            strict=True,
        )
        assert aliased[0].field_type == "number"
        assert aliased[0].required is False

    def test_ontology_strict_rejects_legacy_and_malformed(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            _parse_ontology_fields("tasks:array:List of tasks", strict=True)
        with pytest.raises(ValueError, match="JSON array"):
            _parse_ontology_fields("", strict=True)
        assert _parse_ontology_fields("[]", strict=True) == ()
        malformed = (
            '["not an object"]',
            '[{"type": "string", "description": "missing name"}]',
            '[{"name": "x", "description": "no type"}]',
            '[{"name": "x", "type": " ", "description": "blank type"}]',
            '[{"name": "x", "type": "string", "description": "d", "required": "yes"}]',
        )
        for case in malformed:
            with pytest.raises(ValueError, match="ONTOLOGY_FIELDS"):
                _parse_ontology_fields(case, strict=True)

    def test_ontology_lenient_keeps_legacy_split_and_never_raises(self) -> None:
        legacy = _parse_ontology_fields(
            "tasks:array:List of tasks | status:string:Status: open or closed | :string:d | x::d"
        )
        assert legacy == (
            OntologyField(name="tasks", field_type="array", description="List of tasks"),
            OntologyField(name="status", field_type="string", description="Status: open or closed"),
        )
        stored_json = _parse_ontology_fields(
            '[{"name": "a", "type": "string", "description": "d | note"},'
            ' {"name": "bad", "required": "yes"}, 42]'
        )
        assert len(stored_json) == 1
        assert stored_json[0].description == "d | note"
        assert _parse_ontology_fields('[{"name": "x",]') == ()
        assert _parse_ontology_fields(None) == ()
        model = OntologyField(name="m", field_type="string", description="d")
        assert _parse_ontology_fields((model,)) == (model,)

    @pytest.mark.asyncio
    async def test_ontology_json_fields_reach_seed_and_legacy_retries(self) -> None:
        malformed = create_valid_extraction_response(ontology_fields="tasks:array:List of tasks")
        reformatted = create_valid_extraction_response(
            ontology_fields=(
                '[{"name": "tasks", "type": "array",'
                ' "description": "Tasks with a 1:1 owner mapping | primary"}]'
            )
        )
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed)),
                Result.ok(create_mock_completion_response(reformatted)),
            ]
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        fields = result.value.ontology_schema.fields
        assert fields[0].description == "Tasks with a 1:1 owner mapping | primary"

    def test_lenient_null_weight_defaults_without_raising(self) -> None:
        principles = _parse_evaluation_principles(
            '[{"name": "x", "description": "d", "weight": null}]'
        )
        assert principles[0].weight == 1.0
        assert _parse_exit_conditions("done:desc:   ") == ()

    def test_lenient_skips_malformed_legacy_exit_condition_entries_without_raising(self) -> None:
        assert _parse_exit_conditions(
            "done::criteria | :description:criteria | valid:Description:criteria"
        ) == (
            ExitCondition(
                name="valid",
                description="Description",
                evaluation_criteria="criteria",
            ),
        )

    def test_lenient_parses_stored_json_object_arrays(self) -> None:
        principles = _parse_evaluation_principles(
            '[{"name": "ratio", "description": "1:1 mapping | no drops", "weight": 0.4}]'
        )
        assert principles[0].description == "1:1 mapping | no drops"

    def test_lenient_never_raises(self) -> None:
        # Bracket-led malformed JSON falls back to the historical colon/pipe
        # split (slice-1-consistent junk tolerance) instead of raising.
        fallback = _parse_evaluation_principles('[{"name": "x",]')
        assert isinstance(fallback, tuple)
        assert _parse_exit_conditions("[1, 2]") == ()
        assert _parse_evaluation_principles("[not json | at all") == ()
        assert _parse_evaluation_principles(None) == ()
        assert _parse_exit_conditions("") == ()

    def test_predecoded_sequences_pass_through(self) -> None:
        model = EvaluationPrinciple(name="a", description="b", weight=0.5)
        assert _parse_evaluation_principles((model,)) == (model,)
        assert _parse_evaluation_principles(
            [{"name": "a", "description": "b", "weight": 0.5}], strict=True
        ) == (model,)
        condition = ExitCondition(name="c", description="d", evaluation_criteria="e")
        assert _parse_exit_conditions([condition]) == (condition,)

    def test_strict_rejects_nonfinite_weights(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(ValueError, match="weight"):
                _parse_evaluation_principles(
                    f'[{{"name": "x", "description": "d", "weight": {token}}}]',
                    strict=True,
                )

    def test_oversized_integer_weights_clamp_without_crashing(self) -> None:
        huge = "9" * 400
        principles = _parse_evaluation_principles(
            f'[{{"name": "x", "description": "d", "weight": {huge}}}]', strict=True
        )
        assert principles[0].weight == 1.0
        principles = _parse_evaluation_principles(
            f'[{{"name": "x", "description": "d", "weight": -{huge}}}]', strict=True
        )
        assert principles[0].weight == 0.0

    def test_lenient_nonfinite_and_oversized_weights_never_raise(self) -> None:
        principles = _parse_evaluation_principles(
            '[{"name": "a", "description": "d", "weight": NaN},'
            ' {"name": "b", "description": "d", "weight": Infinity},'
            f' {{"name": "c", "description": "d", "weight": {"9" * 400}}}]'
        )
        assert [p.weight for p in principles] == [1.0, 1.0, 1.0]
        legacy = _parse_evaluation_principles("a:d:inf | b:d:nan | c:d:1e999")
        assert [p.weight for p in legacy] == [1.0, 1.0, 1.0]

    @pytest.mark.asyncio
    async def test_nonfinite_weight_triggers_extraction_retry_not_crash(self) -> None:
        malformed = create_valid_extraction_response(
            evaluation_principles='[{"name": "x", "description": "d", "weight": NaN}]'
        )
        reformatted = create_valid_extraction_response()
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed)),
                Result.ok(create_mock_completion_response(reformatted)),
            ]
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field_name", "field_value"),
        (
            ("evaluation_principles", ""),
            ("exit_conditions", "   "),
        ),
    )
    async def test_blank_object_array_field_triggers_extraction_retry(
        self, field_name: str, field_value: str
    ) -> None:
        malformed = create_valid_extraction_response(**{field_name: field_value})
        reformatted = create_valid_extraction_response(
            evaluation_principles="[]",
            exit_conditions="[]",
        )
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed)),
                Result.ok(create_mock_completion_response(reformatted)),
            ]
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert result.value.evaluation_principles == ()
        assert result.value.exit_conditions == ()
        assert mock_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_null_weight_triggers_extraction_retry(self) -> None:
        malformed = create_valid_extraction_response(
            evaluation_principles='[{"name": "x", "description": "d", "weight": null}]'
        )
        reformatted = create_valid_extraction_response(
            evaluation_principles='[{"name": "x", "description": "d"}]'
        )
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed)),
                Result.ok(create_mock_completion_response(reformatted)),
            ]
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert result.value.evaluation_principles[0].weight == 1.0
        assert mock_adapter.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_oversized_weight_does_not_crash_seed_generation(self) -> None:
        response = create_valid_extraction_response(
            evaluation_principles=(f'[{{"name": "x", "description": "d", "weight": {"9" * 400}}}]')
        )
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(response))
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert result.value.evaluation_principles[0].weight == 1.0

    def test_strict_string_array_rejects_predecoded_non_strings(self) -> None:
        with pytest.raises(ValueError, match="only strings"):
            _parse_string_array_values([1, "x"], field_label="CONSTRAINTS", strict=True)
        assert _parse_string_array_values([1, "x"], field_label="CONSTRAINTS") == ("1", "x")

    @pytest.mark.asyncio
    async def test_extraction_json_object_arrays_reach_seed(self) -> None:
        response = create_valid_extraction_response(
            evaluation_principles=(
                '[{"name": "ratio", "description": "Keep a 1:1 mapping | no drops", "weight": 0.4}]'
            ),
            exit_conditions=(
                '[{"name": "done", "description": "All pass",'
                ' "criteria": "pytest exit 0: all green"}]'
            ),
        )
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(create_mock_completion_response(response))
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert result.value.evaluation_principles == (
            EvaluationPrinciple(
                name="ratio", description="Keep a 1:1 mapping | no drops", weight=0.4
            ),
        )
        assert result.value.exit_conditions == (
            ExitCondition(
                name="done", description="All pass", evaluation_criteria="pytest exit 0: all green"
            ),
        )

    @pytest.mark.asyncio
    async def test_malformed_object_array_triggers_extraction_retry(self) -> None:
        malformed = create_valid_extraction_response(
            evaluation_principles="completeness:All requirements implemented:0.4"
        )
        reformatted = create_valid_extraction_response()
        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(create_mock_completion_response(malformed)),
                Result.ok(create_mock_completion_response(reformatted)),
            ]
        )
        state = create_interview_state_with_rounds()
        low_ambiguity = create_low_ambiguity_score()

        with tempfile.TemporaryDirectory() as tmp_dir:
            generator = SeedGenerator(
                llm_adapter=mock_adapter,
                output_dir=Path(tmp_dir) / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok
        assert mock_adapter.complete.await_count == 2
        assert result.value.evaluation_principles[0].name == "completeness"


class TestVerifyExemptionParsing:
    """Optional `exempt` key — the verify-command gate's per-AC escape hatch."""

    def test_exempt_reason_is_carried_into_the_spec(self) -> None:
        criteria = _parse_extracted_acceptance_criteria(
            json.dumps(
                [
                    {
                        "description": "The onboarding copy reads naturally",
                        "verify": "NONE",
                        "artifacts": "NONE",
                        "expect": "NONE",
                        "exempt": "Prose quality has no deterministic command",
                    }
                ]
            )
        )

        assert len(criteria) == 1
        assert criteria[0].verify_command is None
        assert criteria[0].verify_exemption_reason == "Prose quality has no deterministic command"

    def test_none_sentinel_exempt_is_dropped(self) -> None:
        criteria = _parse_extracted_acceptance_criteria(
            json.dumps(
                [
                    {
                        "description": "Tests pass",
                        "verify": "pytest -q",
                        "artifacts": "NONE",
                        "expect": "NONE",
                        "exempt": "NONE",
                    }
                ]
            )
        )

        assert criteria[0].verify_exemption_reason is None

    def test_exempt_reason_cannot_accompany_verify_command(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _parse_extracted_acceptance_criteria(
                json.dumps(
                    [
                        {
                            "description": "Tests pass",
                            "verify": "pytest -q",
                            "artifacts": "NONE",
                            "expect": "NONE",
                            "exempt": "Browser-only check",
                        }
                    ]
                )
            )

    def test_criteria_without_the_key_stay_valid(self) -> None:
        criteria = _parse_extracted_acceptance_criteria(
            json.dumps(
                [
                    {
                        "description": "Tests pass",
                        "verify": "pytest -q",
                        "artifacts": "NONE",
                        "expect": "NONE",
                    }
                ]
            )
        )

        assert criteria[0].verify_exemption_reason is None

    def test_non_string_exempt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exempt must be a string"):
            _parse_extracted_acceptance_criteria(
                json.dumps(
                    [
                        {
                            "description": "Tests pass",
                            "verify": "NONE",
                            "artifacts": "NONE",
                            "expect": "NONE",
                            "exempt": 3,
                        }
                    ]
                )
            )
