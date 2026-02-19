"""Seed generation module for transforming interview results to immutable Seeds.

This module implements the transformation from InterviewState to Seed,
gating on ambiguity score (must be <= 0.2) to ensure requirements are
clear enough for execution.

The SeedGenerator:
1. Validates ambiguity score is within threshold
2. Uses LLM to extract structured requirements from interview
3. Creates immutable Seed with proper metadata
4. Optionally saves to YAML file
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from ouroboros.bigbang.ambiguity import AMBIGUITY_THRESHOLD, AmbiguityScore
from ouroboros.bigbang.interview import InterviewState
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
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

log = structlog.get_logger()

# Default model moved to config.models.ClarificationConfig.default_model
_FALLBACK_MODEL = "openrouter/google/gemini-2.0-flash-001"
EXTRACTION_TEMPERATURE = 0.2


@dataclass
class SeedGenerator:
    """Generator for creating immutable Seeds from interview state.

    Transforms completed interviews with low ambiguity scores into
    structured, immutable Seed specifications.

    Example:
        generator = SeedGenerator(llm_adapter=LiteLLMAdapter())

        # Generate seed from interview
        result = await generator.generate(
            state=interview_state,
            ambiguity_score=ambiguity_result,
        )

        if result.is_ok:
            seed = result.value
            # Save to file
            save_result = await generator.save_seed(seed, Path("seed.yaml"))

    Note:
        The model can be configured via OuroborosConfig.clarification.default_model
        or passed directly to the constructor.
    """

    llm_adapter: LiteLLMAdapter
    model: str = _FALLBACK_MODEL
    temperature: float = EXTRACTION_TEMPERATURE
    max_tokens: int = 4096
    output_dir: Path = field(default_factory=lambda: Path.home() / ".ouroboros" / "seeds")

    def __post_init__(self) -> None:
        """Ensure output directory exists."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        state: InterviewState,
        ambiguity_score: AmbiguityScore,
        parent_seed: Seed | None = None,
        reflect_output: Any | None = None,
    ) -> Result[Seed, ValidationError | ProviderError]:
        """Generate an immutable Seed from interview state or reflect output.

        Two modes:
        - Gen 1 (reflect_output=None): Extract from interview, gate on ambiguity.
        - Gen 2+ (reflect_output provided): Use refined ACs and ontology mutations
          from ReflectEngine. Skip ambiguity gating.

        Args:
            state: Completed interview state.
            ambiguity_score: The ambiguity score for the interview.
            parent_seed: Optional parent seed for evolutionary lineage.
            reflect_output: Optional ReflectOutput for Gen 2+ evolution.

        Returns:
            Result containing the generated Seed or error.
        """
        # Gen 2+ path: use reflect output directly
        if reflect_output is not None and parent_seed is not None:
            return self.generate_from_reflect(parent_seed, reflect_output)

        log.info(
            "seed.generation.started",
            interview_id=state.interview_id,
            ambiguity_score=ambiguity_score.overall_score,
        )

        # Gate on ambiguity score
        if not ambiguity_score.is_ready_for_seed:
            log.warning(
                "seed.generation.ambiguity_too_high",
                interview_id=state.interview_id,
                ambiguity_score=ambiguity_score.overall_score,
                threshold=AMBIGUITY_THRESHOLD,
            )
            return Result.err(
                ValidationError(
                    f"Ambiguity score {ambiguity_score.overall_score:.2f} exceeds "
                    f"threshold {AMBIGUITY_THRESHOLD}. Cannot generate Seed.",
                    field="ambiguity_score",
                    value=ambiguity_score.overall_score,
                    details={
                        "threshold": AMBIGUITY_THRESHOLD,
                        "interview_id": state.interview_id,
                    },
                )
            )

        # Extract structured requirements from interview
        extraction_result = await self._extract_requirements(state)

        if extraction_result.is_err:
            return Result.err(extraction_result.error)

        requirements = extraction_result.value

        # Create metadata
        metadata = SeedMetadata(
            ambiguity_score=ambiguity_score.overall_score,
            interview_id=state.interview_id,
            parent_seed_id=parent_seed.metadata.seed_id if parent_seed else None,
        )

        # Build the seed
        try:
            seed = self._build_seed(requirements, metadata)

            log.info(
                "seed.generation.completed",
                interview_id=state.interview_id,
                seed_id=seed.metadata.seed_id,
                goal_length=len(seed.goal),
                constraint_count=len(seed.constraints),
                criteria_count=len(seed.acceptance_criteria),
            )

            return Result.ok(seed)

        except Exception as e:
            log.exception(
                "seed.generation.build_failed",
                interview_id=state.interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to build seed: {e}",
                    details={"interview_id": state.interview_id},
                )
            )

    def generate_from_reflect(
        self,
        parent_seed: Seed,
        reflect_output: Any,
    ) -> Result[Seed, ValidationError | ProviderError]:
        """Generate a new Seed from ReflectOutput (Gen 2+ path).

        Applies ontology mutations to parent's schema and uses refined
        ACs from the reflect phase. No ambiguity gating needed.

        Args:
            parent_seed: The parent seed to evolve from.
            reflect_output: ReflectOutput with refined goal/constraints/ACs/mutations.

        Returns:
            Result containing the evolved Seed.
        """
        log.info(
            "seed.generation.from_reflect",
            parent_seed_id=parent_seed.metadata.seed_id,
            mutation_count=len(reflect_output.ontology_mutations),
        )

        try:
            # Apply ontology mutations to parent's schema
            new_ontology = self._apply_mutations(
                parent_seed.ontology_schema,
                reflect_output.ontology_mutations,
            )

            metadata = SeedMetadata(
                ambiguity_score=parent_seed.metadata.ambiguity_score,
                interview_id=parent_seed.metadata.interview_id,
                parent_seed_id=parent_seed.metadata.seed_id,
            )

            seed = Seed(
                goal=reflect_output.refined_goal,
                task_type=parent_seed.task_type,
                constraints=reflect_output.refined_constraints,
                acceptance_criteria=reflect_output.refined_acs,
                ontology_schema=new_ontology,
                evaluation_principles=parent_seed.evaluation_principles,
                exit_conditions=parent_seed.exit_conditions,
                metadata=metadata,
            )

            log.info(
                "seed.generation.from_reflect.completed",
                seed_id=seed.metadata.seed_id,
                parent_seed_id=parent_seed.metadata.seed_id,
                field_count=len(new_ontology.fields),
            )

            return Result.ok(seed)

        except Exception as e:
            log.exception(
                "seed.generation.from_reflect.failed",
                parent_seed_id=parent_seed.metadata.seed_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to generate seed from reflect: {e}",
                    details={"parent_seed_id": parent_seed.metadata.seed_id},
                )
            )

    def _apply_mutations(
        self,
        schema: OntologySchema,
        mutations: tuple,
    ) -> OntologySchema:
        """Apply ontology mutations to produce a new schema.

        Args:
            schema: The parent ontology schema.
            mutations: Tuple of OntologyMutation instances.

        Returns:
            New OntologySchema with mutations applied.
        """
        fields_by_name = {f.name: f for f in schema.fields}

        for mutation in mutations:
            action = str(mutation.action)
            if action == "add" and mutation.field_name not in fields_by_name:
                fields_by_name[mutation.field_name] = OntologyField(
                    name=mutation.field_name,
                    field_type=mutation.field_type or "string",
                    description=mutation.description or mutation.reason,
                )
            elif action == "modify" and mutation.field_name in fields_by_name:
                old = fields_by_name[mutation.field_name]
                fields_by_name[mutation.field_name] = OntologyField(
                    name=mutation.field_name,
                    field_type=mutation.field_type or old.field_type,
                    description=mutation.description or old.description,
                    required=old.required,
                )
            elif action == "remove" and mutation.field_name in fields_by_name:
                del fields_by_name[mutation.field_name]

        return OntologySchema(
            name=schema.name,
            description=schema.description,
            fields=tuple(fields_by_name.values()),
        )

    async def _extract_requirements(
        self, state: InterviewState
    ) -> Result[dict[str, Any], ProviderError]:
        """Extract structured requirements from interview using LLM.

        Args:
            state: The interview state.

        Returns:
            Result containing extracted requirements dict or error.
        """
        context = self._build_interview_context(state)
        system_prompt = self._build_extraction_system_prompt()
        user_prompt = self._build_extraction_user_prompt(context)

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "seed.extraction.failed",
                interview_id=state.interview_id,
                error=str(result.error),
            )
            return Result.err(result.error)

        # Parse the response
        try:
            requirements = self._parse_extraction_response(result.value.content)
            return Result.ok(requirements)
        except (ValueError, KeyError) as e:
            log.warning(
                "seed.extraction.parse_failed",
                interview_id=state.interview_id,
                error=str(e),
                response=result.value.content[:500],
            )
            return Result.err(
                ProviderError(
                    f"Failed to parse extraction response: {e}",
                    details={"response_preview": result.value.content[:200]},
                )
            )

    def _build_interview_context(self, state: InterviewState) -> str:
        """Build context string from interview state.

        Args:
            state: The interview state.

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {state.initial_context}"]

        for round_data in state.rounds:
            parts.append(f"\nQ: {round_data.question}")
            if round_data.user_response:
                parts.append(f"A: {round_data.user_response}")

        return "\n".join(parts)

    def _build_extraction_system_prompt(self) -> str:
        """Build system prompt for requirement extraction.

        Returns:
            System prompt string.
        """
        from ouroboros.agents.loader import load_agent_prompt

        return load_agent_prompt("seed-architect")

    def _build_extraction_user_prompt(self, context: str) -> str:
        """Build user prompt with interview context.

        Args:
            context: Formatted interview context.

        Returns:
            User prompt string.
        """
        return f"""Please extract structured requirements from the following interview conversation:

---
{context}
---

Extract all components and provide them in the specified format."""

    def _parse_extraction_response(self, response: str) -> dict[str, Any]:
        """Parse LLM response into requirements dictionary.

        Args:
            response: Raw LLM response text.

        Returns:
            Parsed requirements dictionary.

        Raises:
            ValueError: If response cannot be parsed.
        """
        lines = response.strip().split("\n")
        requirements: dict[str, Any] = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            for prefix in [
                "GOAL:",
                "CONSTRAINTS:",
                "ACCEPTANCE_CRITERIA:",
                "ONTOLOGY_NAME:",
                "ONTOLOGY_DESCRIPTION:",
                "ONTOLOGY_FIELDS:",
                "EVALUATION_PRINCIPLES:",
                "EXIT_CONDITIONS:",
            ]:
                if line.startswith(prefix):
                    key = prefix[:-1].lower()  # Remove colon and lowercase
                    value = line[len(prefix) :].strip()
                    requirements[key] = value
                    break

        # Validate required fields
        required_fields = [
            "goal",
            "ontology_name",
            "ontology_description",
        ]

        for field_name in required_fields:
            if field_name not in requirements:
                raise ValueError(f"Missing required field: {field_name}")

        return requirements

    def _build_seed(self, requirements: dict[str, Any], metadata: SeedMetadata) -> Seed:
        """Build Seed from extracted requirements.

        Args:
            requirements: Extracted requirements dictionary.
            metadata: Seed metadata.

        Returns:
            Constructed Seed instance.
        """
        # Parse constraints
        constraints: tuple[str, ...] = ()
        if "constraints" in requirements and requirements["constraints"]:
            constraints = tuple(
                c.strip() for c in requirements["constraints"].split("|") if c.strip()
            )

        # Parse acceptance criteria
        acceptance_criteria: tuple[str, ...] = ()
        if "acceptance_criteria" in requirements and requirements["acceptance_criteria"]:
            acceptance_criteria = tuple(
                c.strip() for c in requirements["acceptance_criteria"].split("|") if c.strip()
            )

        # Parse ontology fields
        ontology_fields: list[OntologyField] = []
        if "ontology_fields" in requirements and requirements["ontology_fields"]:
            for field_str in requirements["ontology_fields"].split("|"):
                field_str = field_str.strip()
                if not field_str:
                    continue
                parts = field_str.split(":")
                if len(parts) >= 3:
                    ontology_fields.append(
                        OntologyField(
                            name=parts[0].strip(),
                            field_type=parts[1].strip(),
                            description=":".join(parts[2:]).strip(),
                        )
                    )

        # Build ontology schema
        ontology_schema = OntologySchema(
            name=requirements["ontology_name"],
            description=requirements["ontology_description"],
            fields=tuple(ontology_fields),
        )

        # Parse evaluation principles
        evaluation_principles: list[EvaluationPrinciple] = []
        if "evaluation_principles" in requirements and requirements["evaluation_principles"]:
            for principle_str in requirements["evaluation_principles"].split("|"):
                principle_str = principle_str.strip()
                if not principle_str:
                    continue
                parts = principle_str.split(":")
                if len(parts) >= 2:
                    weight = 1.0
                    if len(parts) >= 3:
                        try:
                            weight = float(parts[2].strip())
                        except ValueError:
                            weight = 1.0
                    evaluation_principles.append(
                        EvaluationPrinciple(
                            name=parts[0].strip(),
                            description=parts[1].strip(),
                            weight=min(1.0, max(0.0, weight)),
                        )
                    )

        # Parse exit conditions
        exit_conditions: list[ExitCondition] = []
        if "exit_conditions" in requirements and requirements["exit_conditions"]:
            for condition_str in requirements["exit_conditions"].split("|"):
                condition_str = condition_str.strip()
                if not condition_str:
                    continue
                parts = condition_str.split(":")
                if len(parts) >= 3:
                    exit_conditions.append(
                        ExitCondition(
                            name=parts[0].strip(),
                            description=parts[1].strip(),
                            evaluation_criteria=":".join(parts[2:]).strip(),
                        )
                    )

        return Seed(
            goal=requirements["goal"],
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            ontology_schema=ontology_schema,
            evaluation_principles=tuple(evaluation_principles),
            exit_conditions=tuple(exit_conditions),
            metadata=metadata,
        )

    async def save_seed(
        self,
        seed: Seed,
        file_path: Path | None = None,
    ) -> Result[Path, ValidationError]:
        """Save seed to YAML file.

        Args:
            seed: The seed to save.
            file_path: Optional path for the seed file.
                If not provided, uses output_dir/seed_{id}.yaml

        Returns:
            Result containing the file path or error.
        """
        if file_path is None:
            file_path = self.output_dir / f"{seed.metadata.seed_id}.yaml"

        log.info(
            "seed.saving",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        try:
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Convert to dict for YAML serialization
            seed_dict = seed.to_dict()

            # Write YAML with proper formatting
            content = yaml.dump(
                seed_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

            file_path.write_text(content, encoding="utf-8")

            log.info(
                "seed.saved",
                seed_id=seed.metadata.seed_id,
                file_path=str(file_path),
            )

            return Result.ok(file_path)

        except (OSError, yaml.YAMLError) as e:
            log.exception(
                "seed.save_failed",
                seed_id=seed.metadata.seed_id,
                file_path=str(file_path),
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to save seed: {e}",
                    details={
                        "seed_id": seed.metadata.seed_id,
                        "file_path": str(file_path),
                    },
                )
            )


async def load_seed(file_path: Path) -> Result[Seed, ValidationError]:
    """Load seed from YAML file.

    Args:
        file_path: Path to the seed YAML file.

    Returns:
        Result containing the loaded Seed or error.
    """
    if not file_path.exists():
        return Result.err(
            ValidationError(
                f"Seed file not found: {file_path}",
                field="file_path",
                value=str(file_path),
            )
        )

    try:
        content = file_path.read_text(encoding="utf-8")
        seed_dict = yaml.safe_load(content)

        # Validate and create Seed
        seed = Seed.from_dict(seed_dict)

        log.info(
            "seed.loaded",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        return Result.ok(seed)

    except (OSError, yaml.YAMLError, ValueError) as e:
        log.exception(
            "seed.load_failed",
            file_path=str(file_path),
            error=str(e),
        )
        return Result.err(
            ValidationError(
                f"Failed to load seed: {e}",
                field="file_path",
                value=str(file_path),
                details={"error": str(e)},
            )
        )


def save_seed_sync(seed: Seed, file_path: Path) -> Result[Path, ValidationError]:
    """Synchronous version of save_seed for convenience.

    Args:
        seed: The seed to save.
        file_path: Path for the seed file.

    Returns:
        Result containing the file path or error.
    """
    log.info(
        "seed.saving.sync",
        seed_id=seed.metadata.seed_id,
        file_path=str(file_path),
    )

    try:
        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict for YAML serialization
        seed_dict = seed.to_dict()

        # Write YAML with proper formatting
        content = yaml.dump(
            seed_dict,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

        file_path.write_text(content, encoding="utf-8")

        log.info(
            "seed.saved.sync",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
        )

        return Result.ok(file_path)

    except (OSError, yaml.YAMLError) as e:
        log.exception(
            "seed.save_failed.sync",
            seed_id=seed.metadata.seed_id,
            file_path=str(file_path),
            error=str(e),
        )
        return Result.err(
            ValidationError(
                f"Failed to save seed: {e}",
                details={
                    "seed_id": seed.metadata.seed_id,
                    "file_path": str(file_path),
                },
            )
        )
