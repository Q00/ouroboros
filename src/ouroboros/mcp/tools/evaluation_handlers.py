"""Evaluation-phase tool handlers for Ouroboros MCP server.

Contains handlers for drift measurement, evaluation, and lateral thinking tools:
- MeasureDriftHandler: Measures goal deviation from seed specification.
- EvaluateHandler: Three-stage evaluation pipeline (mechanical, semantic, consensus).
- LateralThinkHandler: Generates alternative thinking approaches via personas.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.config import (
    get_auto_evolve_enabled,
    get_llm_backend_for_role,
    get_llm_model_for_role,
)
from ouroboros.core.errors import ConfigError, ProviderError, ValidationError
from ouroboros.core.project_paths import resolve_path_against_base, resolve_seed_project_path
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed, ac_text
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPAuthError, MCPServerError, MCPTimeoutError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.telemetry_boundary import (
    record_direct_evaluation_outcome,
)
from ouroboros.mcp.tools import background as background_jobs
from ouroboros.mcp.tools.fanout_handler import (  # noqa: F401
    FetchArtifactHandler,
    SubmitFanoutResultsHandler,
)
from ouroboros.mcp.tools.job_observer import (
    append_job_observer_inline_handoff,
    build_job_observer_contract,
)

# Re-export: LateralThinkHandler moved to its own module (#1797 ratchet);
# definitions.py and tests import it from here.
from ouroboros.mcp.tools.lateral_think_handler import (  # noqa: E402,F401
    LateralThinkHandler,
)
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_SUBAGENT,
    build_evaluate_subagent,
    dispatch_plugin_terminal,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasurement,
)
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_runtime_builtin_tool_names,
)
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter

log = structlog.get_logger(__name__)


def _direct_evaluation_failure_reason(error: object) -> str | None:
    """Map only trusted exception types to the telemetry reason vocabulary."""
    if isinstance(error, (ConfigError,)):
        return "config"
    if isinstance(error, (ValidationError, PydanticValidationError)):
        return "validation"
    if isinstance(error, (MCPAuthError,)):
        return "auth"
    if isinstance(error, (MCPTimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, ProviderError):
        return "model"
    return None


if TYPE_CHECKING:
    from ouroboros.mcp.tools.ralph_handlers import StartRalphHandler
    from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry


def _seed_acceptance_criteria(seed: Seed) -> tuple[str, ...]:
    return tuple(
        stripped
        for criterion in seed.acceptance_criteria
        if (stripped := ac_text(criterion).strip())
    )


def _seed_ac_spec_map(seed: Seed | None) -> dict[str, AcceptanceCriterionSpec]:
    """Map each AC description to its structured spec (PR-H).

    Since PR-F, ``Seed.acceptance_criteria`` holds ``AcceptanceCriterionSpec``
    objects.  Evaluation flows AC text (descriptions) around as bare strings,
    so we key specs by their trimmed ``description`` to reattach the declared
    success contract when building each ``EvaluationContext``.  Only specs
    that actually carry a contract are included — bare-description ACs map to
    nothing and the evaluator judges the AC text alone (today's behavior).
    """
    if seed is None:
        return {}
    spec_map: dict[str, AcceptanceCriterionSpec] = {}
    for criterion in seed.acceptance_criteria:
        if not isinstance(criterion, AcceptanceCriterionSpec):
            continue
        if not criterion.has_success_contract:
            continue
        key = criterion.description.strip()
        if key:
            spec_map.setdefault(key, criterion)
    return spec_map


async def _default_brownfield_project_dir() -> Path | None:
    """Return the registered default brownfield project directory, if any."""
    from ouroboros.persistence.brownfield import BrownfieldStore

    store = BrownfieldStore()
    try:
        await store.initialize()
        default_repo = await store.get_default()
    except Exception as exc:  # noqa: BLE001 - fallback discovery must be best-effort
        log.warning("mcp.tool.evaluate.brownfield_default_lookup_failed", error=str(exc))
        return None
    finally:
        await store.close()

    if default_repo is None or not default_repo.path:
        return None

    resolved = Path(default_repo.path).expanduser().resolve()
    if not resolved.is_dir():
        log.warning(
            "mcp.tool.evaluate.brownfield_default_unusable",
            path=str(resolved),
        )
        return None
    return resolved


def _seed_project_dir(seed: Seed | None, *, stable_base: Path) -> Path | None:
    """Resolve a contained project directory encoded in seed metadata/context."""
    resolution = resolve_seed_project_path(seed, stable_base=stable_base)
    if resolution.path is None:
        return None

    resolved = resolution.path
    if resolved.is_file():
        return resolved.parent
    if resolved.exists() and not resolved.is_dir():
        return None
    return resolved


async def _resolve_evaluate_working_dir(
    explicit_working_dir: str | None,
    seed: Seed | None,
) -> Path:
    """Resolve the project root that gates Stage 1 and Stage 2 evaluation.

    Precedence is explicit tool argument, registered brownfield default,
    seed-declared project directory, then the MCP server cwd. The last
    fallback preserves the historical behavior, but only after project-aware
    sources have been exhausted.
    """
    stable_base = Path.cwd().resolve()
    if explicit_working_dir:
        resolved = resolve_path_against_base(explicit_working_dir, stable_base=stable_base)
        if resolved is not None:
            return resolved

    brownfield_default = await _default_brownfield_project_dir()
    if brownfield_default is not None:
        if brownfield_default.is_dir():
            return brownfield_default.resolve()
        log.warning(
            "mcp.tool.evaluate.brownfield_default_unusable",
            path=str(brownfield_default),
        )

    seed_dir = _seed_project_dir(seed, stable_base=stable_base)
    if seed_dir is not None:
        return seed_dir

    return stable_base


async def _resolve_executor_backend(store: EventStore | None, session_id: str) -> str | None:
    """Best-effort: which runtime backend executed this session (PR-X X2).

    Read from the ``execution.session.completed`` / ``.started`` lifecycle events,
    whose payload carries ``runtime_backend`` per node. Lets formal evaluation
    keep the executor's own vendor out of the reviewer jury. Any failure returns
    ``None`` — evaluation then behaves exactly as before.
    """
    if store is None or not session_id:
        return None
    try:
        for event_type in (
            "execution.session.completed",
            "execution.session.started",
        ):
            events = await store.query_session_related_events(
                session_id, event_type=event_type, limit=50
            )
            for event in events:
                data = event.data if isinstance(event.data, dict) else {}
                backend = data.get("runtime_backend")
                if isinstance(backend, str) and backend.strip():
                    return backend.strip()
    except Exception:
        return None
    return None


def _evaluation_allowed_tools(runtime_backend: str | None) -> list[str]:
    """Return the policy-derived read-only tool envelope for evaluation."""
    return allowed_runtime_builtin_tool_names(
        PolicyContext(
            runtime_backend=runtime_backend,
            session_role=PolicySessionRole.EVALUATION,
            execution_phase=PolicyExecutionPhase.EVALUATION,
        )
    )


@dataclass
class MeasureDriftHandler:
    """Handler for the measure_drift tool.

    Measures goal deviation from the original seed specification
    using DriftMeasurement with weighted components:
    goal (50%), constraint (30%), ontology (20%).
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_measure_drift",
            description=(
                "Measure drift from the original seed goal. "
                "Calculates goal deviation score using weighted components: "
                "goal drift (50%), constraint drift (30%), ontology drift (20%). "
                "Returns drift metrics, analysis, and suggestions if drift exceeds threshold."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to measure drift for",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_output",
                    type=ToolInputType.STRING,
                    description="Current execution output to measure drift against the seed goal",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML content for drift calculation",
                    required=True,
                ),
                MCPToolParameter(
                    name="constraint_violations",
                    type=ToolInputType.ARRAY,
                    description="Known constraint violations (e.g., ['Missing tests', 'Wrong language'])",
                    required=False,
                ),
                MCPToolParameter(
                    name="current_concepts",
                    type=ToolInputType.ARRAY,
                    description="Concepts present in the current output (for ontology drift)",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a drift measurement request.

        Args:
            arguments: Tool arguments including session_id, current_output, and seed_content.

        Returns:
            Result containing drift metrics or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        current_output = arguments.get("current_output")
        if not current_output:
            return Result.err(
                MCPToolError(
                    "current_output is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        constraint_violations_raw = arguments.get("constraint_violations") or []
        current_concepts_raw = arguments.get("current_concepts") or []

        log.info(
            "mcp.tool.measure_drift",
            session_id=session_id,
            output_length=len(current_output),
            violations_count=len(constraint_violations_raw),
        )

        try:
            # Parse seed YAML
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as e:
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )
        except (ValidationError, PydanticValidationError) as e:
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )

        try:
            # Calculate drift using real DriftMeasurement
            measurement = DriftMeasurement()
            metrics = measurement.measure(
                current_output=current_output,
                constraint_violations=[str(v) for v in constraint_violations_raw],
                current_concepts=[str(c) for c in current_concepts_raw],
                seed=seed,
            )

            drift_text = (
                f"Drift Measurement Report\n"
                f"=======================\n"
                f"Session: {session_id}\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Goal: {seed.goal}\n\n"
                f"Combined Drift: {metrics.combined_drift:.2f}\n"
                f"Acceptable Threshold: {DRIFT_THRESHOLD}\n"
                f"Status: {'ACCEPTABLE' if metrics.is_acceptable else 'EXCEEDED'}\n\n"
                f"Component Breakdown:\n"
                f"  Goal Drift: {metrics.goal_drift:.2f} (50% weight)\n"
                f"  Constraint Drift: {metrics.constraint_drift:.2f} (30% weight)\n"
                f"  Ontology Drift: {metrics.ontology_drift:.2f} (20% weight)\n"
            )

            suggestions: list[str] = []
            if not metrics.is_acceptable:
                suggestions.append("Drift exceeds threshold - consider consensus review")
                suggestions.append("Review execution path against original goal")
                if metrics.constraint_drift > 0:
                    suggestions.append(
                        f"Constraint violations detected: {constraint_violations_raw}"
                    )

            if suggestions:
                drift_text += "\nSuggestions:\n"
                for s in suggestions:
                    drift_text += f"  - {s}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=drift_text),),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "seed_id": seed.metadata.seed_id,
                        "goal_drift": metrics.goal_drift,
                        "constraint_drift": metrics.constraint_drift,
                        "ontology_drift": metrics.ontology_drift,
                        "combined_drift": metrics.combined_drift,
                        "is_acceptable": metrics.is_acceptable,
                        "threshold": DRIFT_THRESHOLD,
                        "suggestions": suggestions,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.measure_drift.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to measure drift: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )


@dataclass
class EvaluateHandler:
    """Handler for the ouroboros_evaluate tool.

    Evaluates an execution session using the three-stage evaluation pipeline:
    Stage 1: Mechanical Verification ($0)
    Stage 2: Semantic Evaluation (Standard tier)
    Stage 3: Multi-Model Consensus (Frontier tier, if triggered)
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    TIMEOUT_SECONDS: int = 0  # No server-side timeout; client/runtime decides.

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evaluate",
            description=(
                "Evaluate an Ouroboros execution session using the three-stage evaluation pipeline. "
                "Stage 1 performs mechanical verification (lint, build, test). "
                "Stage 2 performs semantic evaluation of AC compliance and goal alignment. "
                "Stage 3 runs multi-model consensus if triggered by uncertainty or manual request."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The execution output/artifact to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML for goal/constraints extraction",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criterion",
                    type=ToolInputType.STRING,
                    description="Specific acceptance criterion to evaluate against",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criteria",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Multiple acceptance criteria for checklist evaluation. "
                        "When two or more items are provided, each AC is evaluated "
                        "independently and the results are aggregated into a "
                        "pass/fail checklist (#366). Overrides acceptance_criterion."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="trigger_consensus",
                    type=ToolInputType.BOOLEAN,
                    description="Force Stage 3 consensus evaluation. Default: False",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project root used to resolve Stage 1 mechanical verification "
                        "commands and Stage 2 source-file visibility. Commands are "
                        "read from .ouroboros/mechanical.toml; "
                        "when the file is missing, the evaluator makes one AI detect "
                        "call that inspects manifests (package.json, pyproject.toml, "
                        "Cargo.toml, Makefile, ...) and authors the toml. Stage 1 "
                        "skips every check when no toml is produced — it never guesses."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        emit_terminal_telemetry: bool = True,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evaluation request.

        Args:
            arguments: Tool arguments including session_id, artifact, and optional seed_content.
            emit_terminal_telemetry: Whether this call owns the direct-evaluation
                telemetry boundary. True (default) for genuine top-level direct
                calls — the MCP adapter dispatching ``ouroboros_evaluate``, or
                ``ChecklistVerifyHandler`` delegating in multi-AC mode (neither
                is observed by JobManager). False when the caller is itself
                job-backed (``run_evaluation_job`` behind ``StartEvaluateHandler``),
                since ``JobTelemetryBoundary`` already emits a deduplicated
                (``$insert_id``-keyed) terminal outcome for that job — this
                handler is a shared instance reused across both call shapes
                (see ``definitions.py``), so the decision must be made by the
                caller at each call, not baked into the instance.

        Returns:
            Result containing evaluation results or error.
        """
        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            SemanticConfig,
            build_mechanical_config,
            ensure_mechanical_toml,
            has_mechanical_toml,
        )

        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        seed_content = arguments.get("seed_content")
        acceptance_criterion = arguments.get("acceptance_criterion")
        acceptance_criteria_raw = arguments.get("acceptance_criteria")
        artifact_type = arguments.get("artifact_type", "code")
        trigger_consensus = arguments.get("trigger_consensus", False)

        # Normalize all AC inputs into a single tuple (#366 fix):
        # 1. If acceptance_criteria (plural, ARRAY) has valid entries, use them.
        # 2. Else if acceptance_criterion (singular, STRING) is set, wrap it.
        # 3. Else empty — single-AC path will use a default.
        # This ensures a 1-item list is honoured as the effective AC,
        # fixing the contract violation where the input shape was accepted
        # but its meaning was silently ignored.
        acceptance_criteria: tuple[str, ...] = ()
        if isinstance(acceptance_criteria_raw, list):
            acceptance_criteria = tuple(
                str(item).strip()
                for item in acceptance_criteria_raw
                if isinstance(item, (str, int, float)) and str(item).strip()
            )
        if not acceptance_criteria and acceptance_criterion and str(acceptance_criterion).strip():
            acceptance_criteria = (str(acceptance_criterion).strip(),)

        # Parse seed before dispatch so working_dir fallback is available for
        # both plugin/subagent and in-process evaluation paths.
        goal = ""
        constraints: tuple[str, ...] = ()
        seed_id = session_id  # fallback
        seed: Seed | None = None

        if seed_content:
            try:
                seed_dict = yaml.safe_load(seed_content)
                seed = Seed.from_dict(seed_dict)
                goal = seed.goal
                constraints = tuple(seed.constraints)
                seed_id = seed.metadata.seed_id
                if not acceptance_criteria:
                    acceptance_criteria = _seed_acceptance_criteria(seed)
            except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                # Seed failed to parse — leave seed as None; spec map stays empty.
                log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(e))
                # Continue without seed data - not fatal

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            has_seed=seed_content is not None,
            multi_ac_count=len(acceptance_criteria),
            trigger_consensus=trigger_consensus,
        )

        working_dir = await _resolve_evaluate_working_dir(arguments.get("working_dir"), seed)

        if len(acceptance_criteria) > 1:
            ac_for_payload: str | None = "\n".join(
                f"{i + 1}. {ac}" for i, ac in enumerate(acceptance_criteria)
            )
        elif acceptance_criteria:
            ac_for_payload = acceptance_criteria[0]
        else:
            ac_for_payload = None
        from ouroboros.mcp.tools.evaluation_job import worker_safe_evaluation_inputs

        worker_inputs = worker_safe_evaluation_inputs(seed_content, ac_for_payload, artifact)
        payload = build_evaluate_subagent(
            session_id=session_id,
            artifact=worker_inputs.artifact,
            artifact_type=artifact_type,
            seed_content=worker_inputs.seed_content,
            acceptance_criterion=worker_inputs.acceptance_criterion,
            working_dir=str(working_dir),
            trigger_consensus=trigger_consensus,
        )
        if (
            should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode)
            and arguments.get("_force_in_process") is not True
        ):
            # Preserve public response shape (#442): session_id + status are
            # part of the documented contract for ouroboros_evaluate.
            return await dispatch_plugin_terminal(
                self.event_store,
                session_id=session_id,
                payload=payload,
                response_shape={
                    "session_id": session_id,
                    "status": DELEGATED_TO_SUBAGENT,
                    "dispatch_mode": "plugin",
                    "artifact_type": artifact_type,
                    "trigger_consensus": trigger_consensus,
                },
            )

        # Fall-through: real in-process evaluation pipeline (subprocess / non-opencode runtimes).

        store = self.event_store
        owns_event_store = False

        try:
            # Try to enrich from session repository if event_store available
            if not goal:
                if store is None:
                    store = EventStore()
                    owns_event_store = True
                try:
                    await store.initialize()
                    repo = SessionRepository(store)
                    session_result = await repo.reconstruct_session(session_id)
                    if session_result.is_ok:
                        tracker = session_result.value
                        seed_id = tracker.seed_id
                except Exception:
                    pass  # Best-effort enrichment

            # PR-X X2: resolve which runtime backend executed this session so
            # consensus can keep that vendor out of the reviewer jury. Best-effort
            # — None leaves today's behavior untouched.
            executor_backend = await _resolve_executor_backend(store, session_id)

            # Derive current_ac from the unified acceptance_criteria tuple.
            # The tuple already incorporates both the plural and singular params,
            # so we only need to index or fall back to a default.
            current_ac = (
                acceptance_criteria[0]
                if acceptance_criteria
                else "Verify execution output meets requirements"
            )

            # Reattach declared success contracts (PR-H): map AC text back to
            # its structured spec so Stage 2 grades against verify_command /
            # expected_artifacts / output_assertion, not the bare wording.
            ac_spec_map = _seed_ac_spec_map(seed)

            # Evaluation reads multiple spec files (one Read call per AC).
            # Use a dedicated adapter with a higher turn budget — the shared
            # MCP adapter is max_turns=1 (tuned for interview/seed single-shot).
            backend = get_llm_backend_for_role(
                "semantic_evaluation",
                explicit_backend=self.llm_backend,
            )
            llm_adapter = create_llm_adapter(
                backend=backend,
                allowed_tools=_evaluation_allowed_tools(backend),
                max_turns=20,
            )
            log.info(
                "mcp.tool.evaluate.started",
                session_id=session_id,
                artifact_type=artifact_type,
                working_dir=str(working_dir),
                llm_backend=backend,
                adapter_type=type(llm_adapter).__name__,
            )

            # Collect file-based artifacts for richer semantic evaluation.
            # working_dir is used as the project root for artifact resolution.
            #
            # Write the artifact text to a file in working_dir so the
            # ArtifactCollector can pick it up naturally during its scan
            # instead of inlining the full text (potentially 50KB+) into
            # the evaluation prompt.
            from ouroboros.evaluation.artifact_collector import ArtifactCollector

            artifact_file = working_dir / ".ouroboros_eval_artifact.md"
            try:
                artifact_file.write_text(artifact, encoding="utf-8")
            except OSError:
                pass  # Non-critical — evaluator falls back to text_summary

            try:
                artifact_bundle = ArtifactCollector().collect(artifact, str(working_dir))
            except Exception as exc:
                log.warning(
                    "mcp.tool.evaluate.artifact_collection_failed",
                    error=str(exc),
                    working_dir=str(working_dir),
                )
                artifact_bundle = None

            # Stage 1 trusts .ouroboros/mechanical.toml only. When the file is
            # absent we run the AI detector once to author it — silent
            # best-effort, so a failed detect simply leaves Stage 1 empty and
            # the pipeline falls through to Stage 2 instead of phantom-failing
            # on hardcoded preset guesses.
            if not has_mechanical_toml(working_dir):
                try:
                    await ensure_mechanical_toml(
                        working_dir,
                        llm_adapter,
                        backend=backend,
                    )
                except Exception as exc:  # noqa: BLE001 — detector must never break eval
                    log.warning(
                        "mcp.tool.evaluate.detect_failed",
                        working_dir=str(working_dir),
                        error=str(exc),
                    )
            mechanical_config = build_mechanical_config(working_dir)
            config = PipelineConfig(
                mechanical=mechanical_config,
                semantic=SemanticConfig(
                    model=get_llm_model_for_role("semantic_evaluation", backend=backend)
                ),
            )
            pipeline = EvaluationPipeline(llm_adapter, config)

            # Multi-AC checklist path (#366):
            # When the caller provides >= 2 acceptance criteria we run the
            # pipeline once per AC and aggregate the results into a
            # checklist.  Single-AC callers keep the original single-pass
            # behaviour — no extra cost or behaviour change for them.
            if len(acceptance_criteria) >= 2:
                return await self._handle_multi_ac(
                    session_id=session_id,
                    seed_id=seed_id,
                    acceptance_criteria=acceptance_criteria,
                    artifact=artifact,
                    artifact_type=artifact_type,
                    goal=goal,
                    constraints=constraints,
                    trigger_consensus=trigger_consensus,
                    artifact_bundle=artifact_bundle,
                    pipeline=pipeline,
                    working_dir=working_dir,
                    executor_backend=executor_backend,
                    ac_spec_map=ac_spec_map,
                    emit_terminal_telemetry=emit_terminal_telemetry,
                )

            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=current_ac,
                current_ac_spec=ac_spec_map.get(current_ac),
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
                executor_backend=executor_backend,
            )
            result = await pipeline.evaluate(context)

            if result.is_err:
                rendered_error = (
                    result.error.format_details()
                    if hasattr(result.error, "format_details")
                    else str(result.error)
                )
                log.warning(
                    "mcp.tool.evaluate.pipeline_failed",
                    session_id=session_id,
                    working_dir=str(working_dir),
                    llm_backend=backend,
                    error=rendered_error,
                )
                if emit_terminal_telemetry:
                    record_direct_evaluation_outcome(
                        final_approved=None,
                        failed=True,
                        failure_reason_code=_direct_evaluation_failure_reason(result.error),
                    )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed: {rendered_error}",
                        tool_name="ouroboros_evaluate",
                    )
                )

            eval_result = result.value

            # Detect code changes when Stage 1 fails (presentation concern)
            code_changes: bool | None = None
            if eval_result.stage1_result and not eval_result.stage1_result.passed:
                code_changes = await self._has_code_changes(working_dir)

            # Build result text
            result_text = self._format_evaluation_result(eval_result, code_changes=code_changes)

            # Build metadata
            meta = {
                "session_id": session_id,
                "final_approved": eval_result.final_approved,
                "highest_stage": eval_result.highest_stage_completed,
                "stage1_passed": eval_result.stage1_result.passed
                if eval_result.stage1_result
                else None,
                "stage2_ac_compliance": eval_result.stage2_result.ac_compliance
                if eval_result.stage2_result
                else None,
                "stage2_score": eval_result.stage2_result.score
                if eval_result.stage2_result
                else None,
                "stage3_approved": eval_result.stage3_result.approved
                if eval_result.stage3_result
                else None,
                "stage3_reviewer_independence": eval_result.stage3_result.reviewer_independence
                if eval_result.stage3_result
                else None,
                "code_changes_detected": code_changes,
            }

            if emit_terminal_telemetry:
                record_direct_evaluation_outcome(final_approved=eval_result.final_approved)
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta=meta,
                )
            )
        except ConfigError as e:
            # Configuration/bootstrap errors (unsupported backend, missing
            # provider install) — actionable by the user, safe to surface.
            log.warning("mcp.tool.evaluate.config_error", error=str(e))
            if emit_terminal_telemetry:
                record_direct_evaluation_outcome(
                    final_approved=None,
                    failed=True,
                    failure_reason_code="config",
                )
            return Result.err(
                MCPToolError(
                    f"Evaluation setup failed: {e}",
                    tool_name="ouroboros_evaluate",
                )
            )
        except (ValueError, RuntimeError) as e:
            # Preserve the historical setup-facing response for these factory
            # failures, but do not infer ``config`` from a generic built-in
            # exception. Only a typed ConfigError earns that telemetry reason.
            log.warning("mcp.tool.evaluate.setup_error", error=str(e))
            if emit_terminal_telemetry:
                record_direct_evaluation_outcome(
                    final_approved=None,
                    failed=True,
                    failure_reason_code=_direct_evaluation_failure_reason(e),
                )
            return Result.err(
                MCPToolError(
                    f"Evaluation setup failed: {e}",
                    tool_name="ouroboros_evaluate",
                )
            )
        except Exception as exc:
            log.exception("mcp.tool.evaluate.error")
            if emit_terminal_telemetry:
                record_direct_evaluation_outcome(
                    final_approved=None,
                    failed=True,
                    failure_reason_code=_direct_evaluation_failure_reason(exc),
                )
            return Result.err(
                MCPToolError(
                    "Evaluation failed due to an internal error. Check server logs for details.",
                    tool_name="ouroboros_evaluate",
                )
            )
        finally:
            if owns_event_store and store is not None:
                await store.close()

    async def _handle_multi_ac(
        self,
        *,
        session_id: str,
        seed_id: str,
        acceptance_criteria: tuple[str, ...],
        artifact: str,
        artifact_type: str,
        goal: str,
        constraints: tuple[str, ...],
        trigger_consensus: bool,
        artifact_bundle: object | None,
        pipeline: object,  # EvaluationPipeline — typed as object to avoid import cycle
        working_dir: Path,
        executor_backend: str | None = None,
        ac_spec_map: dict[str, AcceptanceCriterionSpec] | None = None,
        emit_terminal_telemetry: bool = True,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Evaluate each AC individually and return an aggregated checklist (#366).

        Stage 1 (mechanical verification — lint/build/test) is AC-agnostic,
        so we run it exactly once via the first AC's full pipeline call and
        inject the result into the remaining per-AC evaluations.  Only
        Stage 2+ (semantic evaluation) is parallelized per AC via
        ``asyncio.gather``.

        Per-AC results are then folded into a single ``ACChecklistResult``
        so the caller sees one pass/fail checklist with per-item evidence
        and failure reasons.

        Single-AC callers never reach this path — see ``handle()``.
        """

        from ouroboros.evaluation import EvaluationContext
        from ouroboros.evaluation.checklist import (
            aggregate_results,
            build_run_feedback,
            format_checklist,
        )

        spec_map = ac_spec_map or {}

        log.info(
            "mcp.tool.evaluate.multi_ac_started",
            session_id=session_id,
            ac_count=len(acceptance_criteria),
        )

        # --- Stage 1: run once via the first AC's full pipeline call ---
        first_context = EvaluationContext(
            execution_id=session_id,
            seed_id=seed_id,
            current_ac=acceptance_criteria[0],
            current_ac_spec=spec_map.get(acceptance_criteria[0]),
            artifact=artifact,
            artifact_type=artifact_type,
            goal=goal,
            constraints=constraints,
            trigger_consensus=trigger_consensus,
            artifact_bundle=artifact_bundle,
            executor_backend=executor_backend,
        )
        first_result = await pipeline.evaluate(first_context)  # type: ignore[attr-defined]
        if first_result.is_err:
            err = first_result.error
            rendered = err.format_details() if hasattr(err, "format_details") else str(err)
            if emit_terminal_telemetry:
                record_direct_evaluation_outcome(
                    final_approved=None,
                    failed=True,
                    failure_reason_code=_direct_evaluation_failure_reason(err),
                )
            return Result.err(
                MCPToolError(
                    f"Evaluation failed: {rendered}",
                    tool_name="ouroboros_evaluate",
                )
            )

        # Extract Stage 1 result to share with remaining ACs.
        shared_stage1 = first_result.value.stage1_result

        # --- Stage 2+: parallelize remaining ACs (Stage 1 injected) ---
        async def _run_one(ac_text: str) -> Result[object, object]:
            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=ac_text,
                current_ac_spec=spec_map.get(ac_text),
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
                executor_backend=executor_backend,
            )
            return await pipeline.evaluate(  # type: ignore[attr-defined]
                context,
                stage1_result=shared_stage1,
            )

        remaining_gathered = await asyncio.gather(
            *(_run_one(ac) for ac in acceptance_criteria[1:]),
            return_exceptions=True,
        )
        gathered = (first_result, *remaining_gathered)

        # Any exception or err-Result aborts the whole checklist —
        # otherwise we'd aggregate over a half-evaluated set.
        for entry in gathered:
            if isinstance(entry, BaseException):
                log.exception(
                    "mcp.tool.evaluate.multi_ac_exception",
                    session_id=session_id,
                )
                if emit_terminal_telemetry:
                    record_direct_evaluation_outcome(
                        final_approved=None,
                        failed=True,
                        failure_reason_code=_direct_evaluation_failure_reason(entry),
                    )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed during multi-AC run: {entry}",
                        tool_name="ouroboros_evaluate",
                    )
                )
            if entry.is_err:  # type: ignore[union-attr]
                err = entry.error  # type: ignore[union-attr]
                rendered = err.format_details() if hasattr(err, "format_details") else str(err)
                log.warning(
                    "mcp.tool.evaluate.multi_ac_pipeline_failed",
                    session_id=session_id,
                    error=rendered,
                )
                if emit_terminal_telemetry:
                    record_direct_evaluation_outcome(
                        final_approved=None,
                        failed=True,
                        failure_reason_code=_direct_evaluation_failure_reason(err),
                    )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed: {rendered}",
                        tool_name="ouroboros_evaluate",
                    )
                )

        eval_results = tuple(entry.value for entry in gathered)  # type: ignore[union-attr]
        checklist = aggregate_results(acceptance_criteria, eval_results)
        feedback = build_run_feedback(checklist)
        # The checklist is one aggregate evaluation, so its trustworthy stage
        # is the highest stage completed by every AC pipeline, not an invented
        # constant or the best stage reached by only one AC. EvaluationSummary
        # uses this value as a quality axis in frugality comparisons.
        highest_stage = min(max(1, result.highest_stage_completed) for result in eval_results)

        code_changes: bool | None = None
        if any(r.stage1_result and not r.stage1_result.passed for r in eval_results):
            code_changes = await self._has_code_changes(working_dir)

        text_parts = [format_checklist(checklist)]
        if code_changes is False:
            text_parts.append("\nNote: no code changes detected in the working tree.")
        result_text = "\n".join(text_parts)

        meta = {
            "session_id": session_id,
            "final_approved": checklist.all_passed,
            "highest_stage": highest_stage,
            "multi_ac": True,
            "ac_count": checklist.total,
            "passed_count": checklist.passed_count,
            "pass_rate": checklist.pass_rate,
            "checklist": [
                {
                    "ac_text": item.ac_text,
                    "passed": item.passed,
                    "reasoning": item.reasoning,
                    "evidence": list(item.evidence),
                    "questions_used": list(item.questions_used),
                    "failure_reason": item.failure_reason,
                }
                for item in checklist.items
            ],
            "run_feedback": list(feedback),
            "code_changes_detected": code_changes,
        }

        log.info(
            "mcp.tool.evaluate.multi_ac_completed",
            session_id=session_id,
            passed=checklist.passed_count,
            total=checklist.total,
            all_passed=checklist.all_passed,
        )

        if emit_terminal_telemetry:
            record_direct_evaluation_outcome(final_approved=checklist.all_passed)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=False,
                meta=meta,
            )
        )

    async def _has_code_changes(self, working_dir: Path) -> bool | None:
        """Detect whether the working tree has code changes.

        Runs ``git status --porcelain`` to check for modifications.

        Returns:
            True if changes detected, False if clean, None if not a git repo
            or git is unavailable.
        """
        from ouroboros.evaluation.mechanical import run_command

        try:
            cmd_result = await run_command(
                ("git", "status", "--porcelain"),
                timeout=10,
                working_dir=working_dir,
            )
            if cmd_result.return_code != 0:
                return None
            return bool(cmd_result.stdout.strip())
        except Exception:
            return None

    def _format_evaluation_result(self, result, *, code_changes: bool | None = None) -> str:
        """Format evaluation result as human-readable text.

        Args:
            result: EvaluationResult from pipeline.
            code_changes: Whether working tree has code changes (Stage 1 context).

        Returns:
            Formatted text representation.
        """
        lines = [
            "Evaluation Results",
            "=" * 60,
            f"Execution ID: {result.execution_id}",
            f"Final Approval: {'APPROVED' if result.final_approved else 'REJECTED'}",
            f"Highest Stage Completed: {result.highest_stage_completed}",
            "",
        ]

        # Stage 1 results
        if result.stage1_result:
            s1 = result.stage1_result
            lines.extend(
                [
                    "Stage 1: Mechanical Verification",
                    "-" * 40,
                    f"Status: {'PASSED' if s1.passed else 'FAILED'}",
                    f"Coverage: {s1.coverage_score:.1%}" if s1.coverage_score else "Coverage: N/A",
                ]
            )
            for check in s1.checks:
                status = "PASS" if check.passed else "FAIL"
                lines.append(f"  [{status}] {check.check_type}: {check.message}")
                if not check.passed:
                    details = check.details
                    command = details.get("command")
                    if isinstance(command, list) and command:
                        lines.append(f"    command: {' '.join(str(part) for part in command)}")
                    working_dir = details.get("working_dir")
                    if working_dir:
                        lines.append(f"    cwd: {working_dir}")
                    stdout_tail = str(details.get("stdout_tail") or "").strip()
                    stderr_tail = str(details.get("stderr_tail") or "").strip()
                    if stdout_tail:
                        lines.append("    stdout tail:")
                        lines.extend(f"      {line}" for line in stdout_tail.splitlines())
                    if stderr_tail:
                        lines.append("    stderr tail:")
                        lines.extend(f"      {line}" for line in stderr_tail.splitlines())
            lines.append("")

        # Stage 2 results
        if result.stage2_result:
            s2 = result.stage2_result
            lines.extend(
                [
                    "Stage 2: Semantic Evaluation",
                    "-" * 40,
                    f"Score: {s2.score:.2f}",
                    f"AC Compliance: {'YES' if s2.ac_compliance else 'NO'}",
                    f"Goal Alignment: {s2.goal_alignment:.2f}",
                    f"Drift Score: {s2.drift_score:.2f}",
                    f"Uncertainty: {s2.uncertainty:.2f}",
                    f"Reasoning: {s2.reasoning[:200]}..."
                    if len(s2.reasoning) > 200
                    else f"Reasoning: {s2.reasoning}",
                ]
            )
            # Anti-reward-hacking transparency (#367): surface the concrete
            # Socratic questions and evidence the evaluator relied on so
            # the user can audit whether the verdict was earned.
            if s2.questions_used:
                lines.append("Questions Used:")
                for question in s2.questions_used:
                    lines.append(f"  - {question}")
            if s2.evidence:
                lines.append("Evidence:")
                for item in s2.evidence:
                    lines.append(f"  - {item}")
            lines.append("")

        # Stage 3 results
        if result.stage3_result:
            s3 = result.stage3_result
            lines.extend(
                [
                    "Stage 3: Multi-Model Consensus",
                    "-" * 40,
                    f"Status: {'APPROVED' if s3.approved else 'REJECTED'}",
                    f"Majority Ratio: {s3.majority_ratio:.1%}",
                    f"Total Votes: {s3.total_votes}",
                    f"Approving: {s3.approving_votes}",
                ]
            )
            reviewer_independence = getattr(s3, "reviewer_independence", None)
            if reviewer_independence:
                lines.append(f"Reviewer Independence: {reviewer_independence}")
            for vote in s3.votes:
                decision = "APPROVE" if vote.approved else "REJECT"
                lines.append(f"  [{decision}] {vote.model} (confidence: {vote.confidence:.2f})")
            if s3.disagreements:
                lines.append("Disagreements:")
                for d in s3.disagreements:
                    lines.append(f"  - {d[:100]}...")
            lines.append("")

        # Failure reason
        if not result.final_approved:
            lines.extend(
                [
                    "Failure Reason",
                    "-" * 40,
                    result.failure_reason or "Unknown",
                ]
            )
            # Contextual annotation for Stage 1 failures
            stage1_failed = result.stage1_result and not result.stage1_result.passed
            if stage1_failed and code_changes is True:
                lines.extend(
                    [
                        "",
                        "⚠ Code changes detected — these are real build/test failures "
                        "that need to be fixed before re-evaluating.",
                    ]
                )
            elif stage1_failed and code_changes is False:
                lines.extend(
                    [
                        "",
                        "ℹ No code changes detected in the working tree. These failures "
                        "are expected if you haven't run `ooo run` yet to produce code.",
                    ]
                )

        return "\n".join(lines)


@dataclass
class ChecklistVerifyHandler:
    """Handler for the ``ouroboros_checklist_verify`` tool (#366).

    Given a seed (containing ``acceptance_criteria``) and an execution
    artifact, this handler routes each AC through the Stage 2 evaluation
    pipeline and returns an aggregated checklist.  It is intentionally
    thin — it composes ``EvaluateHandler`` rather than reimplementing
    pipeline orchestration, so it stays in sync with any future changes
    to the main evaluator.

    Why this is a separate tool instead of a flag on ``ouroboros_execute_seed``:

    - ``ExecuteSeed`` is already complex (background execution, resume,
      delegation) and has a stable public contract.  Adding a retry
      loop inside it would entangle with Ralph mode and the Job system.
    - This tool lets the *caller* (a human, a ``/ralph`` loop, or a
      runtime workflow) decide when and how to retry.  No decisions
      are hidden inside background tasks.
    - It is opt-in: existing callers are unaffected.
    """

    evaluate_handler: EvaluateHandler | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_checklist_verify",
            description=(
                "Verify that a Run artifact satisfies every acceptance criterion "
                "in a Seed.  Returns a per-AC checklist (pass/fail with evidence "
                "and failure reasons) plus ready-to-use run_feedback strings the "
                "caller can inject into a re-run prompt.  Does NOT automatically "
                "re-execute — the caller (Ralph, workflow, or human) decides."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID being verified",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description=(
                        "Seed YAML containing acceptance_criteria, goal, constraints. "
                        "The seed's acceptance_criteria list is evaluated in full."
                    ),
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The Run output/artifact to verify against the seed's ACs",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description="Project working directory (for language auto-detection).",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Verify the seed's full AC list against the artifact."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        # Extract acceptance criteria from seed.
        try:
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as exc:
            log.warning("mcp.tool.checklist_verify.yaml_error", error=str(exc))
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {exc}",
                    tool_name="ouroboros_checklist_verify",
                )
            )
        except (ValidationError, PydanticValidationError) as exc:
            log.warning("mcp.tool.checklist_verify.seed_validation_error", error=str(exc))
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {exc}",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        acceptance_criteria = tuple(
            text for criterion in seed.acceptance_criteria if (text := ac_text(criterion).strip())
        )
        if not acceptance_criteria:
            return Result.err(
                MCPToolError(
                    "Seed has no acceptance_criteria — cannot build checklist.",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        # Delegate to EvaluateHandler in multi-AC mode.  Re-using the
        # evaluator means language detection, artifact bundling, event
        # logging, and LLM backend handling stay consistent.
        evaluator = self.evaluate_handler or EvaluateHandler(llm_backend=self.llm_backend)

        evaluate_args = {
            "session_id": session_id,
            "artifact": artifact,
            "seed_content": seed_content,
            "acceptance_criteria": list(acceptance_criteria),
            "artifact_type": arguments.get("artifact_type", "code"),
        }
        if "working_dir" in arguments:
            evaluate_args["working_dir"] = arguments["working_dir"]

        log.info(
            "mcp.tool.checklist_verify.started",
            session_id=session_id,
            ac_count=len(acceptance_criteria),
        )

        # ouroboros_checklist_verify is itself a genuine top-level direct MCP
        # tool (no "Start" job-backed variant, no JobManager wraps it), so the
        # nested EvaluateHandler call keeps emit_terminal_telemetry at its
        # default True — nothing else will emit a workflow_outcome for this
        # invocation.
        result = await evaluator.handle(evaluate_args)

        if result.is_err:
            log.warning(
                "mcp.tool.checklist_verify.evaluate_failed",
                session_id=session_id,
                error=str(result.error),
            )
            return result

        # Augment the MCP result meta so callers can distinguish the
        # verify path from a plain multi-AC evaluate call.
        meta = dict(result.value.meta or {})
        meta["checklist_verify"] = True
        meta["seed_goal"] = seed.goal
        augmented = MCPToolResult(
            content=result.value.content,
            is_error=result.value.is_error,
            meta=meta,
        )

        log.info(
            "mcp.tool.checklist_verify.completed",
            session_id=session_id,
            all_passed=meta.get("final_approved"),
            passed_count=meta.get("passed_count"),
            ac_count=meta.get("ac_count"),
        )

        return Result.ok(augmented)


@dataclass
class StartEvaluateHandler:
    """Start an evaluation asynchronously and return a job ID immediately.

    The three-stage evaluation pipeline (mechanical + semantic + optional
    consensus) routinely runs longer than an MCP client's default tool-call
    timeout (Claude Code's MCP layer caps tool calls at ~120s). This handler
    wraps :class:`EvaluateHandler` in a :class:`JobManager`-backed background
    job so the caller gets a ``job_id`` immediately and polls for the verdict
    via ``ouroboros_job_status`` / ``ouroboros_job_wait`` /
    ``ouroboros_job_result``.

    Plugin mode (OpenCode subagent dispatch) is terminal when automatic
    evolution is disabled, mirroring :class:`StartExecuteSeedHandler` and
    :class:`StartEvolveStepHandler`: the envelope is emitted directly and no
    background job is enqueued. With automatic evolution enabled the parent
    must observe the verdict, so evaluation stays parent-owned and pollable.
    """

    evaluate_handler: EvaluateHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    start_ralph_handler: "StartRalphHandler | None" = field(default=None, repr=False)
    seed_handoff_registry: "SeedHandoffRegistry | None" = field(default=None, repr=False)
    deadline_seconds: float = 1800.0

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evaluate_handler = self.evaluate_handler or EvaluateHandler(
            event_store=self._event_store,
            llm_backend=self.llm_backend,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )

    async def _enqueue_chained_ralph(
        self,
        evaluation_result: MCPToolResult,
        *,
        session_id: str,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        """Compatibility seam for direct chain tests and embedders."""
        from ouroboros.mcp.tools.evaluate_ralph_chain import enqueue_chained_ralph

        return await enqueue_chained_ralph(
            evaluation_result,
            session_id=session_id,
            arguments=arguments,
            event_store=self._event_store,
            job_manager=self._job_manager,
            start_ralph_handler=self.start_ralph_handler,
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_evaluate",
            description=(
                "Start an evaluation in the background and return a job ID immediately. "
                "Use this instead of ouroboros_evaluate when the three-stage pipeline "
                "(mechanical + semantic + optional consensus) is expected to exceed the "
                "MCP client tool-call timeout. Poll with ouroboros_job_status / "
                "ouroboros_job_wait and read the verdict via ouroboros_job_result. "
                "In plugin mode with auto_evolve disabled, evaluation is delegated "
                "to an OpenCode Task pane and job_id is None. With auto_evolve enabled, "
                "the parent keeps evaluation pollable so a rejection can start Ralph."
            ),
            parameters=(
                *EvaluateHandler().definition.parameters,
                MCPToolParameter(
                    name="auto_evolve",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Override execution.auto_evolve for this background call. "
                        "When true, an explicitly rejected formal evaluation starts "
                        "a bounded Ralph continuation loop."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_handoff_id",
                    type=ToolInputType.STRING,
                    description="Opaque parent-owned Seed handle for plugin evaluation",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_start_evaluate",
                )
            )
        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_start_evaluate",
                )
            )

        from ouroboros.mcp.tools.evaluation_job import (
            dispatch_plugin_evaluation,
            resolve_auto_evolve_policy,
            restore_seed_handoff,
        )

        try:
            arguments, auto_evolve_enabled = resolve_auto_evolve_policy(
                arguments, configured_enabled=get_auto_evolve_enabled()
            )
            plugin_dispatch = should_dispatch_via_plugin(
                self.agent_runtime_backend, self.opencode_mode
            )
            background_acceptance = background_jobs.BackgroundJobAcceptanceState()
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_evaluate"))

        try:
            arguments, seed_handoff = restore_seed_handoff(
                arguments, session_id=session_id, registry=self.seed_handoff_registry
            )
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_evaluate"))

        # Plugin mode is terminal only when no convergence successor is needed:
        # return the delegation envelope without enqueuing a background job,
        # matching StartExecuteSeedHandler / StartEvolveStepHandler. Polling a
        # fake job_id would break the ouroboros_job_status contract.
        if plugin_dispatch and not auto_evolve_enabled:
            return await seed_handoff.accept(
                dispatch_plugin_evaluation(
                    arguments=arguments,
                    session_id=session_id,
                    artifact=artifact,
                    event_store=self._event_store,
                    resolve_working_dir=_resolve_evaluate_working_dir,
                ),
                require_ok=True,
                acceptance_may_have_occurred=False,
            )

        # The shared helper preserves job-scoped cancellation across restart.
        async def _runner(_handle) -> MCPToolResult:
            from ouroboros.mcp.tools.evaluation_job import run_evaluation_job

            return await run_evaluation_job(
                evaluate_handler=self._evaluate_handler,
                arguments=arguments,
                session_id=session_id,
                deadline_seconds=self.deadline_seconds,
                force_in_process=auto_evolve_enabled and plugin_dispatch,
                auto_evolve=auto_evolve_enabled,
                event_store=self._event_store,
                job_manager=self._job_manager,
                start_ralph_handler=self.start_ralph_handler,
            )

        snapshot = await seed_handoff.accept(
            background_jobs.start_background_tool_job(
                job_manager=self._job_manager,
                event_store=self._event_store,
                job_type="evaluate",
                intent="evaluate",
                process_scope=f"evaluate:{session_id}",
                initial_message=f"Queued evaluation for {session_id}",
                links=JobLinks(session_id=session_id),
                work_fn=_runner,
                cancelled_text="Evaluation cancelled before work began.",
                detached_tool_name="ouroboros_start_evaluate",
                detached_arguments=arguments,
                runtime_backend=self.agent_runtime_backend,
                llm_backend=self.llm_backend,
                opencode_mode=self.opencode_mode,
                acceptance_state=background_acceptance,
            ),
            acceptance_may_have_occurred=background_acceptance.may_have_accepted,
        )

        observer = build_job_observer_contract(
            job_id=snapshot.job_id,
            cursor=snapshot.cursor,
            session_id=session_id,
            follow_result_job_keys=("chained_ralph_job_id",),
        )
        text = (
            f"Started background evaluation.\n\nJob ID: {snapshot.job_id}\n"
            f"Session ID: {session_id}\n\nUse ouroboros_job_status, ouroboros_job_wait, "
            "or ouroboros_job_result to monitor it."
        )
        text = append_job_observer_inline_handoff(text, observer)
        meta = {
            "job_id": snapshot.job_id,
            "session_id": session_id,
            "status": snapshot.status.value,
            "cursor": snapshot.cursor,
            "job_observer": observer,
        }
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta=meta,
                structured_content=dict(meta),
            )
        )
