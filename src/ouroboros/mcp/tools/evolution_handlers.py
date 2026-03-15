"""Evolution-related tool handlers for MCP server.

Contains handlers for evolutionary loop operations:
- EvolveStepHandler: Run one generation of the evolutionary loop
- EvolveRewindHandler: Rewind a lineage to a specific generation
- LineageStatusHandler: Query lineage state without running a generation
- StartEvolveStepHandler: Start an evolve_step asynchronously (background job)
"""

from dataclasses import dataclass, field
import os
from typing import Any

import structlog
import yaml

from ouroboros.core.seed import Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)


@dataclass
class EvolveStepHandler:
    """Handler for the ouroboros_evolve_step tool.

    Runs exactly ONE generation of the evolutionary loop.
    Designed for Ralph integration: stateless between calls,
    all state reconstructed from events.
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)

    TIMEOUT_SECONDS: int = int(
        os.environ.get("OUROBOROS_GENERATION_TIMEOUT", "7200")
    )  # Override MCP adapter's default 30s

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_step",
            description=(
                "Run exactly ONE generation of the evolutionary loop. "
                "For Gen 1: provide lineage_id and seed_content (YAML). "
                "For Gen 2+: provide lineage_id only (state reconstructed from events). "
                "Returns generation result, convergence signal, and next action "
                "(continue/converged/stagnated/exhausted/failed)."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="Lineage ID to continue or new ID for Gen 1",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description=(
                        "Seed YAML content for Gen 1. "
                        "Omit for Gen 2+ (seed reconstructed from events)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="execute",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run seed execution and evaluation. "
                        "True (default): full pipeline with Execute→Validate→Evaluate. "
                        "False: ontology-only evolution (fast, no execution)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="parallel",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run ACs in parallel. "
                        "True (default): parallel execution (fast, may cause import conflicts). "
                        "False: sequential execution (slower, more stable code generation)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA evaluation. Default: false",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project root directory for validation (pytest collection check). "
                        "If omitted, auto-detected from execution output or CWD."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evolve_step request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_step",
                )
            )

        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_step",
                )
            )

        # Parse seed if provided (Gen 1)
        initial_seed = None
        seed_content = arguments.get("seed_content")
        if seed_content:
            try:
                seed_dict = yaml.safe_load(seed_content)
                initial_seed = Seed.from_dict(seed_dict)
            except Exception as e:
                return Result.err(
                    MCPToolError(
                        f"Failed to parse seed_content: {e}",
                        tool_name="ouroboros_evolve_step",
                    )
                )

        execute = arguments.get("execute", True)
        parallel = arguments.get("parallel", True)
        project_dir = arguments.get("project_dir")
        normalized_project_dir = (
            project_dir if isinstance(project_dir, str) and project_dir else None
        )

        project_dir_token = self.evolutionary_loop.set_project_dir(normalized_project_dir)

        try:
            # Ensure event store is initialized before evolve_step accesses it
            # (evolve_step calls replay_lineage/append before executor/evaluator)
            await self.evolutionary_loop.event_store.initialize()
            result = await self.evolutionary_loop.evolve_step(
                lineage_id, initial_seed, execute=execute, parallel=parallel
            )
        except Exception as e:
            log.error("mcp.tool.evolve_step.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"evolve_step failed: {e}",
                    tool_name="ouroboros_evolve_step",
                )
            )
        finally:
            self.evolutionary_loop.reset_project_dir(project_dir_token)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_step",
                )
            )

        step = result.value
        gen = step.generation_result
        sig = step.convergence_signal

        # Format output
        text_lines = [
            f"## Generation {gen.generation_number}",
            "",
            f"**Action**: {step.action.value}",
            f"**Phase**: {gen.phase.value}",
            f"**Convergence similarity**: {sig.ontology_similarity:.2%}",
            f"**Reason**: {sig.reason}",
            *(
                [f"**Failed ACs**: {', '.join(str(i + 1) for i in sig.failed_acs)}"]
                if sig.failed_acs
                else []
            ),
            f"**Lineage**: {step.lineage.lineage_id} ({step.lineage.current_generation} generations)",
            f"**Next generation**: {step.next_generation}",
        ]

        if gen.execution_output:
            text_lines.append("")
            text_lines.append("### Execution output")
            output_preview = truncate_head_tail(gen.execution_output)
            text_lines.append(output_preview)

        if gen.evaluation_summary:
            text_lines.append("")
            text_lines.append("### Evaluation")
            es = gen.evaluation_summary
            text_lines.append(f"- **Approved**: {es.final_approved}")
            text_lines.append(f"- **Score**: {es.score}")
            text_lines.append(f"- **Drift**: {es.drift_score}")
            if es.failure_reason:
                text_lines.append(f"- **Failure**: {es.failure_reason}")
            if es.ac_results:
                text_lines.append("")
                text_lines.append("#### Per-AC Results")
                for ac in es.ac_results:
                    status = "PASS" if ac.passed else "FAIL"
                    text_lines.append(f"- AC {ac.ac_index + 1}: [{status}] {ac.ac_content[:80]}")

        if gen.wonder_output:
            text_lines.append("")
            text_lines.append("### Wonder questions")
            for q in gen.wonder_output.questions:
                text_lines.append(f"- {q}")

        if gen.validation_output:
            text_lines.append("")
            text_lines.append("### Validation")
            text_lines.append(gen.validation_output)

        if gen.ontology_delta:
            text_lines.append("")
            text_lines.append(
                f"### Ontology delta (similarity: {gen.ontology_delta.similarity:.2%})"
            )
            for af in gen.ontology_delta.added_fields:
                text_lines.append(f"- **Added**: {af.name} ({af.field_type})")
            for rf in gen.ontology_delta.removed_fields:
                text_lines.append(f"- **Removed**: {rf}")
            for mf in gen.ontology_delta.modified_fields:
                text_lines.append(f"- **Modified**: {mf.field_name}: {mf.old_type} → {mf.new_type}")

        # Post-execution QA
        qa_meta = None
        skip_qa = arguments.get("skip_qa", False)
        if step.action.value in ("continue", "converged") and execute and not skip_qa:
            from ouroboros.mcp.tools.qa import QAHandler

            qa_handler = QAHandler()
            quality_bar = "Generation must improve upon previous generation."
            if initial_seed:
                ac_lines = [f"- {ac}" for ac in initial_seed.acceptance_criteria]
                quality_bar = "The execution must satisfy all acceptance criteria:\n" + "\n".join(
                    ac_lines
                )

            artifact = gen.execution_output or "\n".join(text_lines)
            qa_result = await qa_handler.handle(
                {
                    "artifact": artifact,
                    "artifact_type": "test_output",
                    "quality_bar": quality_bar,
                    "seed_content": seed_content or "",
                    "pass_threshold": 0.80,
                }
            )
            if qa_result.is_ok:
                text_lines.append("")
                text_lines.append("### QA Verdict")
                text_lines.append(qa_result.value.content[0].text)
                qa_meta = qa_result.value.meta

        meta = {
            "lineage_id": step.lineage.lineage_id,
            "generation": gen.generation_number,
            "action": step.action.value,
            "similarity": sig.ontology_similarity,
            "converged": sig.converged,
            "next_generation": step.next_generation,
            "executed": execute,
            "has_execution_output": gen.execution_output is not None,
        }
        if qa_meta:
            meta["qa"] = qa_meta

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=False,
                meta=meta,
            )
        )


@dataclass
class EvolveRewindHandler:
    """Handler for the ouroboros_evolve_rewind tool.

    Rewinds an evolutionary lineage to a specific generation.
    Delegates to EvolutionaryLoop.rewind_to().
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)

    TIMEOUT_SECONDS: int = 0  # No timeout

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_rewind",
            description=(
                "Rewind an evolutionary lineage to a specific generation. "
                "Truncates all generations after the target and emits a "
                "lineage.rewound event. The lineage can then continue evolving "
                "from the rewind point."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to rewind",
                    required=True,
                ),
                MCPToolParameter(
                    name="to_generation",
                    type=ToolInputType.INTEGER,
                    description="Generation number to rewind to (inclusive)",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a rewind request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        to_generation = arguments.get("to_generation")
        if to_generation is None:
            return Result.err(
                MCPToolError(
                    "to_generation is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        try:
            await self.evolutionary_loop.event_store.initialize()
            events = await self.evolutionary_loop.event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to replay lineage: {e}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        # Validate generation is in range
        if to_generation < 1 or to_generation > lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Generation {to_generation} out of range [1, {lineage.current_generation}]",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if to_generation == lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Already at generation {to_generation}, nothing to rewind",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from_gen = lineage.current_generation
        result = await self.evolutionary_loop.rewind_to(lineage, to_generation)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        rewound_lineage = result.value

        # Get seed_json from the target generation if available
        target_gen = None
        for g in rewound_lineage.generations:
            if g.generation_number == to_generation:
                target_gen = g
                break

        seed_info = ""
        if target_gen and target_gen.seed_json:
            seed_info = f"\n\n### Target generation seed\n```yaml\n{target_gen.seed_json}\n```"

        text = (
            f"## Rewind Complete\n\n"
            f"**Lineage**: {lineage_id}\n"
            f"**From generation**: {from_gen}\n"
            f"**To generation**: {to_generation}\n"
            f"**Status**: {rewound_lineage.status.value}\n"
            f"**Git tag**: `ooo/{lineage_id}/gen_{to_generation}`\n\n"
            f"Generations {to_generation + 1}–{from_gen} have been truncated.\n"
            f"Run `ralph.sh --lineage-id {lineage_id}` to resume evolution."
            f"{seed_info}"
        )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "lineage_id": lineage_id,
                    "from_generation": from_gen,
                    "to_generation": to_generation,
                },
            )
        )


@dataclass
class LineageStatusHandler:
    """Handler for the ouroboros_lineage_status tool.

    Queries the current state of an evolutionary lineage
    without running a generation.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lineage_status",
            description=(
                "Query the current state of an evolutionary lineage. "
                "Returns generation count, status, ontology evolution, "
                "and convergence progress."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to query",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lineage status request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_lineage_status",
                )
            )

        await self._ensure_initialized()

        try:
            events = await self._event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage from events: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        text_lines = [
            f"## Lineage: {lineage.lineage_id}",
            "",
            f"**Status**: {lineage.status.value}",
            f"**Goal**: {lineage.goal}",
            f"**Generations**: {lineage.current_generation}",
            f"**Created**: {lineage.created_at.isoformat()}",
        ]

        # Ontology summary
        if lineage.current_ontology:
            text_lines.append("")
            text_lines.append(f"### Current Ontology: {lineage.current_ontology.name}")
            for f in lineage.current_ontology.fields:
                required = " (required)" if f.required else ""
                text_lines.append(f"- **{f.name}**: {f.field_type}{required}")

        # Generation history
        if lineage.generations:
            text_lines.append("")
            text_lines.append("### Generation History")
            for gen in lineage.generations:
                status = (
                    "passed"
                    if gen.evaluation_summary and gen.evaluation_summary.final_approved
                    else "pending"
                )
                error_part = ""
                if gen.failure_error:
                    error_part = f" | {gen.failure_error[:60]}"
                text_lines.append(
                    f"- Gen {gen.generation_number}: {gen.phase.value} | {status}{error_part}"
                )

        # Rewind history
        if lineage.rewind_history:
            text_lines.append("")
            text_lines.append("### Rewind History")
            for rr in lineage.rewind_history:
                ts = rr.rewound_at
                time_str = (
                    ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                )
                text_lines.append(
                    f"- \u21a9 Rewound Gen {rr.from_generation} \u2192 "
                    f"Gen {rr.to_generation} ({time_str})"
                )
                for dg in rr.discarded_generations:
                    score_part = ""
                    if dg.evaluation_summary and dg.evaluation_summary.score is not None:
                        score_part = f" | score={dg.evaluation_summary.score:.2f}"
                    error_part = ""
                    if dg.failure_error:
                        error_part = f" | {dg.failure_error[:60]}"
                    text_lines.append(
                        f"  - Gen {dg.generation_number}: {dg.phase.value}{score_part}{error_part}"
                    )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=False,
                meta={
                    "lineage_id": lineage.lineage_id,
                    "status": lineage.status.value,
                    "generations": lineage.current_generation,
                    "goal": lineage.goal,
                },
            )
        )


@dataclass
class StartEvolveStepHandler:
    """Start one evolve_step generation asynchronously."""

    evolve_handler: EvolveStepHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evolve_handler = self.evolve_handler or EvolveStepHandler()

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_evolve_step",
            description=(
                "Start one evolve_step generation in the background and return a job ID "
                "immediately for later status checks."
            ),
            parameters=EvolveStepHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_start_evolve_step",
                )
            )

        async def _runner() -> MCPToolResult:
            result = await self._evolve_handler.handle(arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        snapshot = await self._job_manager.start_job(
            job_type="evolve_step",
            initial_message=f"Queued evolve_step for {lineage_id}",
            runner=_runner(),
            links=JobLinks(lineage_id=lineage_id),
        )

        text = (
            f"Started background evolve_step.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Lineage ID: {lineage_id}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, or ouroboros_job_result to monitor it."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "lineage_id": lineage_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                },
            )
        )
