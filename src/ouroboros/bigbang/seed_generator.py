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
import re
from typing import Any

import structlog
import yaml

from ouroboros.bigbang.ambiguity import AMBIGUITY_THRESHOLD, AmbiguityScore
from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    InterviewState,
    initial_context_summary_missing,
    prompt_safe_initial_context,
)
from ouroboros.bigbang.requirement_distillation import (
    apply_requirement_distillation,
    build_promoted_reference_seed,
    build_requirement_distillation,
    is_reference_aware_distillation,
    seed_readiness_details,
)
from ouroboros.config import get_llm_model_for_role
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, LLMAdapter, Message, MessageRole

log = structlog.get_logger()

EXTRACTION_TEMPERATURE = 0.2
_MAX_EXTRACTION_RETRIES = 1
_AC_CONTRACT_FIELD_RE = re.compile(r"\s\|\s*(verify|artifacts|expect)\s*:", re.IGNORECASE)
_UNSUPPORTED_VERIFY_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?[A-Za-z_][\w-]*['\"]?")


def _parse_acceptance_criteria_contracts(
    raw_value: object,
) -> tuple[AcceptanceCriterionSpec | str, ...]:
    """Parse legacy AC prose or structured AC success-contract lines."""
    if isinstance(raw_value, list | tuple):
        entries = tuple(str(item).strip() for item in raw_value if str(item).strip())
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return ()
        if "\nAC:" in text or text.startswith("AC:"):
            entries = tuple(line.strip() for line in text.splitlines() if line.strip())
        else:
            entries = tuple(item.strip() for item in text.split("|") if item.strip())
    else:
        return ()

    parsed: list[AcceptanceCriterionSpec | str] = []
    for entry in entries:
        if not entry.startswith("AC:"):
            parsed.append(entry)
            continue
        spec = _parse_acceptance_criterion_contract(entry)
        parsed.append(spec if spec is not None else entry.removeprefix("AC:").strip())
    return tuple(parsed)


def _parse_acceptance_criterion_contract(line: str) -> AcceptanceCriterionSpec | None:
    """Parse one structured AC line, falling back to description-only on gaps."""
    if not line.startswith("AC:"):
        return None
    body = line.removeprefix("AC:").strip()
    matches = tuple(_AC_CONTRACT_FIELD_RE.finditer(body))
    description_end = matches[0].start() if matches else len(body)
    description = body[:description_end].strip()
    if not description:
        return None
    fields: dict[str, object] = {"description": description}
    seen_fields: set[str] = set()
    for index, match in enumerate(matches):
        normalized_key = match.group(1).lower()
        if normalized_key in seen_fields:
            raise ValueError(f"Duplicate {normalized_key} field in acceptance criterion")
        seen_fields.add(normalized_key)
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        normalized_value = body[value_start:value_end].strip()
        if normalized_key == "artifacts" and not normalized_value:
            raise ValueError("Empty artifacts field; use artifacts: NONE")
        if not normalized_value or normalized_value.upper() == "NONE":
            continue
        if normalized_key == "verify":
            fields["verify_command"] = normalized_value
        elif normalized_key == "artifacts":
            artifact_entries = normalized_value.split(",")
            if any(not item.strip() for item in artifact_entries):
                raise ValueError("Malformed artifacts field contains an empty path")
            fields["expected_artifacts"] = tuple(item.strip() for item in artifact_entries)
        elif normalized_key == "expect":
            fields["output_assertion"] = normalized_value
    return AcceptanceCriterionSpec.model_validate(fields)


def _unsupported_verify_command_reason(command: str) -> str | None:
    if "\n" in command or "\r" in command:
        return "verify_command must be a single-line command"
    if _UNSUPPORTED_VERIFY_HEREDOC_RE.search(command):
        return "verify_command uses heredoc/multiline shell syntax; use python -c or pytest instead"
    return None


def _validate_acceptance_criteria_contract_lines(lines: object) -> None:
    if not isinstance(lines, list | tuple):
        return
    for index, raw_line in enumerate(lines, start=1):
        line = str(raw_line).strip()
        if not line.startswith("AC:"):
            continue
        spec = _parse_acceptance_criterion_contract(line)
        if spec is None or not spec.verify_command:
            continue
        reason = _unsupported_verify_command_reason(spec.verify_command)
        if reason:
            raise ValueError(
                f"Unsupported verify_command in acceptance criterion {index}: {reason}. "
                "The Seed AC format is one line; use a complete single-line command."
            )


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

    llm_adapter: LLMAdapter
    model: str | None = None
    model_is_explicit: bool = field(default=False, init=False)
    temperature: float = EXTRACTION_TEMPERATURE
    max_tokens: int = 4096
    output_dir: Path = field(default_factory=lambda: Path.home() / ".ouroboros" / "seeds")

    def __post_init__(self) -> None:
        """Ensure output directory exists."""
        self.model_is_explicit = self.model is not None
        if self.model is None:
            self.model = get_llm_model_for_role("seed_generation")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        state: InterviewState,
        ambiguity_score: AmbiguityScore,
        parent_seed: Seed | None = None,
        reflect_output: Any | None = None,
        *,
        force: bool = False,
    ) -> Result[Seed, ValidationError | ProviderError]:
        """Generate an immutable Seed from interview state or reflect output.

        Two modes:
        - Gen 1 (reflect_output=None): Extract from interview, gate on ambiguity.
        - Gen 2+ (reflect_output provided): Use refined ACs and ontology mutations
          from ReflectEngine. Skip ambiguity gating.

        When ``force=True``, the ambiguity threshold gate is bypassed but every
        other validation (initial-context summary, extraction, build) still runs.
        The real ``ambiguity_score.overall_score`` is recorded in metadata so
        forced seeds carry truthful provenance.

        Args:
            state: Completed interview state.
            ambiguity_score: The ambiguity score for the interview.
            parent_seed: Optional parent seed for evolutionary lineage.
            reflect_output: Optional ReflectOutput for Gen 2+ evolution.
            force: When True, bypass the ambiguity threshold gate. The real
                score is still stamped into ``SeedMetadata.ambiguity_score``.
                Defaults to False (gate enforced).

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

        # Gate on ambiguity score (skipped when force=True)
        if force:
            log.warning(
                "seed.generation.ambiguity_gate_bypassed",
                interview_id=state.interview_id,
                ambiguity_score=ambiguity_score.overall_score,
                threshold=AMBIGUITY_THRESHOLD,
            )
        elif not ambiguity_score.is_ready_for_seed:
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

        if initial_context_summary_missing(state):
            return Result.err(
                ValidationError(
                    "Initial context summary required before seed generation",
                    field="initial_context",
                    details={"interview_id": state.interview_id},
                )
            )

        distillation = build_requirement_distillation(state)
        preflight = apply_requirement_distillation({}, distillation)
        if preflight.promotion.blockers:
            return Result.err(
                ValidationError(
                    "Interview must be reopened before Seed generation",
                    field="requirement_distillation",
                    details=seed_readiness_details(preflight.promotion),
                )
            )
        state.requirement_distillation = distillation
        if is_reference_aware_distillation(distillation):
            return Result.ok(
                build_promoted_reference_seed(
                    state,
                    distillation,
                    ambiguity_score=ambiguity_score.overall_score,
                )
            )

        # Extract structured requirements from interview
        extraction_result = await self._extract_requirements(state)

        if extraction_result.is_err:
            return Result.err(extraction_result.error)

        applied = apply_requirement_distillation(extraction_result.value, distillation)
        if applied.promotion.blockers:
            return Result.err(
                ValidationError(
                    "Interview must be reopened before Seed generation",
                    field="requirement_distillation",
                    details=seed_readiness_details(applied.promotion),
                )
            )
        requirements = applied.requirements

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
                brownfield_context=parent_seed.brownfield_context,
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

        Retries once with a clarified prompt on parse failure.

        Args:
            state: The interview state.

        Returns:
            Result containing extracted requirements dict or error.
        """
        context = self._build_interview_context(state)
        is_brownfield = (
            state.is_brownfield
            or bool(state.codebase_context.strip())
            or bool(state.codebase_paths)
        )
        system_prompt = self._build_extraction_system_prompt()
        user_prompt = self._build_extraction_user_prompt(context, is_brownfield=is_brownfield)

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        assert self.model is not None
        config = CompletionConfig(
            model=self.model,
            role="seed_generation",
            model_is_explicit=self.model_is_explicit,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        last_error = ""
        last_response = ""

        for attempt in range(_MAX_EXTRACTION_RETRIES + 1):
            result = await self.llm_adapter.complete(messages, config)

            if result.is_err:
                log.warning(
                    "seed.extraction.failed",
                    interview_id=state.interview_id,
                    error=str(result.error),
                    attempt=attempt + 1,
                )
                return Result.err(result.error)

            last_response = result.value.content

            try:
                requirements = self._parse_extraction_response(last_response)
                if attempt > 0:
                    log.info(
                        "seed.extraction.retry_succeeded",
                        interview_id=state.interview_id,
                        attempt=attempt + 1,
                    )
                return Result.ok(requirements)
            except (ValueError, KeyError) as e:
                last_error = str(e)
                log.warning(
                    "seed.extraction.parse_failed",
                    interview_id=state.interview_id,
                    error=last_error,
                    response=last_response[:500],
                    attempt=attempt + 1,
                )

                if attempt < _MAX_EXTRACTION_RETRIES:
                    # Retry with clarified prompt
                    messages = [
                        Message(role=MessageRole.SYSTEM, content=system_prompt),
                        Message(
                            role=MessageRole.USER,
                            content=self._build_retry_prompt(
                                context,
                                last_response,
                                last_error,
                                is_brownfield=is_brownfield,
                            ),
                        ),
                    ]

        return Result.err(
            ProviderError(
                f"Failed to parse extraction response after "
                f"{_MAX_EXTRACTION_RETRIES + 1} attempts: {last_error}",
                details={"response_preview": last_response[:200]},
            )
        )

    def _build_retry_prompt(
        self,
        context: str,
        failed_response: str,
        error: str,
        *,
        is_brownfield: bool = False,
    ) -> str:
        """Build a retry prompt after extraction parse failure.

        Args:
            context: Original interview context.
            failed_response: The response that failed to parse.
            error: The parse error message.
            is_brownfield: Whether the interview targets an existing codebase.

        Returns:
            Retry prompt string.
        """
        return f"""Your previous response could not be parsed. Error: {error}

Your response was:
---
{failed_response[:1000]}
---

Please try again. Extract requirements from this interview:
---
{context}
---

You MUST respond with ONLY the following format, one field per line, no other text:

ACCEPTANCE_CRITERIA rule: produce 3-7 outcome-level criteria. Each is one independently valuable, user-visible outcome — NOT an implementation step. Do not pre-decompose into sub-tasks; the execution engine splits work at runtime.
ACCEPTANCE_CRITERIA verify rule: `verify` must be one complete single-line shell command. Never use heredoc or multiline syntax (`<<`, `<<'PY'`, `cat <<EOF`, line-continuation scripts); use `python -c "..."`, `python3 -c "..."`, or `python -m pytest -q` instead.
ACCEPTANCE_CRITERIA artifacts rule: every `artifacts` entry must be an exact file or directory path relative to the run workspace; the runner resolves it literally and requires it to exist. Prefix a top-level path containing spaces with `./`. Never use a descriptive label. If no exact path is known, use `artifacts: NONE` and provide a concrete `verify` command instead.
ACCEPTANCE_CRITERIA expect rule: `expect` is ONLY a literal string printed verbatim in the combined stdout and stderr of `verify`, such as `OK` or `5 passed`. Use `expect: NONE` for exit-code/status conditions like `exit code 0`, `success`, `passed`, or `no errors`; exit-code 0 is already verified separately.

GOAL: <clear goal statement>
CONSTRAINTS: <constraint 1> | <constraint 2> | ...
ACCEPTANCE_CRITERIA:
AC: <description> | verify: <command or NONE> | artifacts: <comma-list or NONE> | expect: <output assertion or NONE>
AC: <description> | verify: <command or NONE> | artifacts: <comma-list or NONE> | expect: <output assertion or NONE>
ONTOLOGY_NAME: <name>
ONTOLOGY_DESCRIPTION: <description>
ONTOLOGY_FIELDS: <name>:<type>:<description> | ...
EVALUATION_PRINCIPLES: <name>:<description>:<weight> | ...
EXIT_CONDITIONS: <name>:<description>:<criteria> | ...
{self._project_type_template(is_brownfield=is_brownfield)}"""

    @staticmethod
    def _project_type_template(*, is_brownfield: bool) -> str:
        """Return the PROJECT_TYPE trailer for the extraction format.

        Greenfield interviews keep today's single ``PROJECT_TYPE: greenfield``
        line. Brownfield interviews declare the type and request the three
        brownfield keys the parser already recognizes so the resulting Seed
        carries a populated ``brownfield_context``.
        """
        if not is_brownfield:
            return "PROJECT_TYPE: greenfield"
        return (
            "PROJECT_TYPE: brownfield\n"
            "CONTEXT_REFERENCES: <path>:<role primary|reference>:<summary> | ...\n"
            "EXISTING_PATTERNS: <pattern 1> | <pattern 2> | ...\n"
            "EXISTING_DEPENDENCIES: <dependency 1> | <dependency 2> | ..."
        )

    def _build_interview_context(self, state: InterviewState) -> str:
        """Build context string from interview state.

        Args:
            state: The interview state.

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {prompt_safe_initial_context(state)}"]

        # Brownfield priming: carry the auto-explore codebase summary and the
        # referenced paths into the extraction context so the seed architect
        # can populate CONTEXT_REFERENCES / EXISTING_PATTERNS / EXISTING_DEPENDENCIES
        # instead of defaulting the seed to greenfield.
        if state.codebase_context.strip():
            parts.append(f"\nCodebase Context:\n{state.codebase_context.strip()}")
        if state.codebase_paths:
            rendered_paths = "; ".join(
                f"{entry.get('path', '')} ({entry.get('role', 'reference')})"
                for entry in state.codebase_paths
                if entry.get("path")
            )
            if rendered_paths:
                parts.append(f"\nCodebase Paths: {rendered_paths}")

        for round_data in state.rounds:
            if round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION:
                continue
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

    def _build_extraction_user_prompt(self, context: str, *, is_brownfield: bool = False) -> str:
        """Build user prompt with interview context.

        Args:
            context: Formatted interview context.
            is_brownfield: Whether the interview targets an existing codebase.
                When True, the format requests brownfield keys the parser
                already understands; greenfield keeps today's template output.

        Returns:
            User prompt string.
        """
        return f"""Extract structured requirements from the following interview conversation.

---
{context}
---

Respond ONLY with the structured format below. Do NOT add explanations, questions, commentary, or prose. Do NOT wrap in markdown code blocks.

ACCEPTANCE_CRITERIA rule: produce 3-7 outcome-level criteria. Each is one independently valuable, user-visible outcome — NOT an implementation step. Do not pre-decompose into sub-tasks; the execution engine splits work at runtime. If you would list more than 7, merge criteria that share a user-visible outcome before responding. An AC that is a sub-step of a sibling AC is a defect, as severe as a missing requirement.
ACCEPTANCE_CRITERIA verify rule: `verify` must be one complete single-line shell command. Never use heredoc or multiline syntax (`<<`, `<<'PY'`, `cat <<EOF`, line-continuation scripts); use `python -c "..."`, `python3 -c "..."`, or `python -m pytest -q` instead.
ACCEPTANCE_CRITERIA artifacts rule: every `artifacts` entry must be an exact file or directory path relative to the run workspace; the runner resolves it literally and requires it to exist. Prefix a top-level path containing spaces with `./`. Never use a descriptive label. If no exact path is known, use `artifacts: NONE` and provide a concrete `verify` command instead.
ACCEPTANCE_CRITERIA expect rule: `expect` is ONLY a literal string printed verbatim in the combined stdout and stderr of `verify`, such as `OK` or `5 passed`. Use `expect: NONE` for exit-code/status conditions like `exit code 0`, `success`, `passed`, or `no errors`; exit-code 0 is already verified separately.

GOAL: <clear goal statement>
CONSTRAINTS: <constraint 1> | <constraint 2> | ...
ACCEPTANCE_CRITERIA:
AC: <description> | verify: <command or NONE> | artifacts: <comma-list or NONE> | expect: <output assertion or NONE>
AC: <description> | verify: <command or NONE> | artifacts: <comma-list or NONE> | expect: <output assertion or NONE>
ONTOLOGY_NAME: <name>
ONTOLOGY_DESCRIPTION: <description>
ONTOLOGY_FIELDS: <name>:<type>:<description> | ...
EVALUATION_PRINCIPLES: <name>:<description>:<weight> | ...
EXIT_CONDITIONS: <name>:<description>:<criteria> | ...
{self._project_type_template(is_brownfield=is_brownfield)}"""

    _KNOWN_PREFIXES = (
        "GOAL:",
        "CONSTRAINTS:",
        "ACCEPTANCE_CRITERIA:",
        "ONTOLOGY_NAME:",
        "ONTOLOGY_DESCRIPTION:",
        "ONTOLOGY_FIELDS:",
        "EVALUATION_PRINCIPLES:",
        "EXIT_CONDITIONS:",
        "PROJECT_TYPE:",
        "CONTEXT_REFERENCES:",
        "EXISTING_PATTERNS:",
        "EXISTING_DEPENDENCIES:",
    )

    def _preprocess_response(self, response: str) -> str:
        """Strip markdown code blocks and conversational preamble.

        Args:
            response: Raw LLM response text.

        Returns:
            Cleaned response starting from first recognized prefix.
        """
        text = response.strip()

        # Strip markdown code block markers
        code_block_match = re.search(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
        if code_block_match:
            text = code_block_match.group(1).strip()

        # Find first recognized prefix and discard preamble
        lines = text.split("\n")
        start_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if any(stripped.startswith(p) for p in self._KNOWN_PREFIXES):
                start_idx = i
                break

        return "\n".join(lines[start_idx:])

    def _parse_extraction_response(self, response: str) -> dict[str, Any]:
        """Parse LLM response into requirements dictionary.

        Args:
            response: Raw LLM response text.

        Returns:
            Parsed requirements dictionary.

        Raises:
            ValueError: If response cannot be parsed.
        """
        cleaned = self._preprocess_response(response)
        lines = cleaned.strip().split("\n")
        requirements: dict[str, Any] = {}

        current_multiline_key: str | None = None
        multiline_values: dict[str, list[str]] = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            matched_prefix = False
            for prefix in self._KNOWN_PREFIXES:
                if line.startswith(prefix):
                    key = prefix[:-1].lower()  # Remove colon and lowercase
                    value = line[len(prefix) :].strip()
                    if key == "acceptance_criteria" and not value:
                        current_multiline_key = key
                        multiline_values.setdefault(key, [])
                    else:
                        requirements[key] = value
                        current_multiline_key = None
                    matched_prefix = True
                    break
            if matched_prefix:
                continue
            if current_multiline_key == "acceptance_criteria" and line.startswith("AC:"):
                multiline_values.setdefault(current_multiline_key, []).append(line)

        if multiline_values.get("acceptance_criteria"):
            requirements["acceptance_criteria"] = multiline_values["acceptance_criteria"]
            _validate_acceptance_criteria_contract_lines(requirements["acceptance_criteria"])

        # Validate required fields
        required_fields = [
            "goal",
            "ontology_name",
            "ontology_description",
        ]

        for field_name in required_fields:
            if field_name not in requirements:
                raise ValueError(
                    f"Missing required field: {field_name}. "
                    f"Found: {list(requirements.keys())}. "
                    f"Response preview: {response[:200]}"
                )

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
        acceptance_criteria: tuple[AcceptanceCriterionSpec | str, ...] = ()
        if "acceptance_criteria" in requirements and requirements["acceptance_criteria"]:
            acceptance_criteria = _parse_acceptance_criteria_contracts(
                requirements["acceptance_criteria"]
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

        # Parse brownfield context
        brownfield_context = BrownfieldContext()
        project_type = requirements.get("project_type", "greenfield").strip().lower()
        if project_type == "brownfield":
            # Parse context references: path:role:summary | ...
            context_refs: list[ContextReference] = []
            if "context_references" in requirements and requirements["context_references"]:
                for ref_str in requirements["context_references"].split("|"):
                    ref_str = ref_str.strip()
                    if not ref_str:
                        continue
                    parts = ref_str.split(":")
                    if len(parts) >= 2:
                        context_refs.append(
                            ContextReference(
                                path=parts[0].strip(),
                                role=parts[1].strip() if len(parts) > 1 else "reference",
                                summary=":".join(parts[2:]).strip() if len(parts) > 2 else "",
                            )
                        )

            # Parse existing patterns
            existing_patterns: tuple[str, ...] = ()
            if "existing_patterns" in requirements and requirements["existing_patterns"]:
                existing_patterns = tuple(
                    p.strip() for p in requirements["existing_patterns"].split("|") if p.strip()
                )

            # Parse existing dependencies
            existing_deps: tuple[str, ...] = ()
            if "existing_dependencies" in requirements and requirements["existing_dependencies"]:
                existing_deps = tuple(
                    d.strip() for d in requirements["existing_dependencies"].split("|") if d.strip()
                )

            brownfield_context = BrownfieldContext(
                project_type="brownfield",
                context_references=tuple(context_refs),
                existing_patterns=existing_patterns,
                existing_dependencies=existing_deps,
            )

        return Seed(
            goal=requirements["goal"],
            brownfield_context=brownfield_context,
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
