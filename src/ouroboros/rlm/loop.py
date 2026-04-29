"""Isolated Recursive Language Model MVP loop bootstrap.

This module owns the new ``ooo rlm`` execution path. It intentionally does not
call the existing run or evolve command implementations; later RLM phases can
extend this boundary without changing their default behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from ouroboros.core.ac_tree import ACNode, ACStatus, ACTree
from ouroboros.core.errors import ProviderError
from ouroboros.rlm.benchmark import (
    RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
    RLMBenchmarkFixture,
    benchmark_fixture_for_id,
    benchmark_fixture_for_target,
)
from ouroboros.rlm.contracts import (
    RLM_HERMES_EXECUTE_ATOMIC_MODE,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
)
from ouroboros.rlm.trace import (
    RLM_HERMES_CALL_FAILED_EVENT,
    RLM_HERMES_CALL_SUCCEEDED_EVENT,
    RLMHermesTraceRecord,
    RLMTraceStore,
    hash_trace_text,
)

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentRuntime, RuntimeHandle

MAX_RLM_AC_TREE_DEPTH = 5
MAX_RLM_AMBIGUITY_THRESHOLD = 0.2
DEFAULT_RLM_ATOMIC_CHUNK_LINE_LIMIT = 80
DEFAULT_RLM_MAX_ATOMIC_CHUNKS = 6
DEFAULT_RLM_MAX_ITERATIONS = 64
RLM_GENERATION_ID = "rlm_generation_0"
RLM_ROOT_AC_NODE_ID = "rlm_ac_root"
RLM_ROOT_NODE_ID = "rlm_node_root"
RLM_BENCHMARK_ID = RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION = "rlm.evaluation.output.v1"
RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION = "rlm.parent_execution_context.v1"
RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION = "rlm.parent_node_summary.v1"
RLM_SYNTHESIZED_SUBCALL_SUMMARY_SCHEMA_VERSION = "rlm.synthesized_subcall_summary.v1"

HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT = """You are the inner language model in a dual-layer recursive execution loop.

Perform only the bounded RLM task requested by the JSON envelope.
Do not invoke Ouroboros, do not run any ooo command, and do not delegate recursively.
Return a single JSON object that follows the requested output contract."""

RLMHermesMode = Literal["execute_atomic", "synthesize_parent"]


@dataclass(frozen=True, slots=True)
class RLMBenchmarkEvidenceSpec:
    """Static source-evidence target for the dogfood benchmark output."""

    source_path: str
    start_marker: str
    end_marker: str | None
    claim_categories: tuple[str, ...]
    claim: str


RLM_BENCHMARK_EVIDENCE_SPECS: tuple[RLMBenchmarkEvidenceSpec, ...] = (
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/cli/commands/rlm.py",
        start_marker="async def _run_with_default_trace_store(",
        end_marker="def _default_truncation_fixture_path(",
        claim_categories=("Command isolation",),
        claim=(
            "The rlm command helper dispatches only to the isolated RLM loop or "
            "benchmark helper and owns EventStore trace setup for command invocations."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/cli/main.py",
        start_marker="from ouroboros.cli.commands import",
        end_marker='app.command(name="rlm")(rlm.command)',
        claim_categories=("Command isolation",),
        claim=(
            "The top-level CLI imports rlm and registers it as a standalone command "
            "separate from the run command group."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/rlm/loop.py",
        start_marker="MAX_RLM_AC_TREE_DEPTH = 5",
        end_marker="Return a single JSON object",
        claim_categories=("AC/RLM traceability", "Guardrails"),
        claim=(
            "The RLM loop defines the depth cap, ambiguity threshold, root RLM/AC "
            "IDs, and a Hermes boundary prompt that forbids recursive Ouroboros calls."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/rlm/loop.py",
        start_marker="def _chunk_lines(",
        end_marker="def _build_atomic_execution_prompt(",
        claim_categories=("Context scaling",),
        claim=(
            "Source targets are split into bounded chunks with stable chunk IDs, "
            "line spans, token estimates, and a configured chunk limit."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/evolution/wonder.py",
        start_marker="def _build_prompt(",
        end_marker='return "\\n".join(parts)',
        claim_categories=("Wonder input construction", "Benchmark migration question support"),
        claim=(
            "Wonder prompt construction supplies seed scope, current ontology, evaluation "
            "results, execution output, and recent lineage for benchmark inspection."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/evolution/reflect.py",
        start_marker="class ReflectOutput",
        end_marker="class ReflectEngine",
        claim_categories=("Reflect output", "Generation-level ontology migration"),
        claim=(
            "Reflect output carries next-generation acceptance criteria, ontology "
            "mutations, and reasoning for benchmark inspection."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/rlm/loop.py",
        start_marker="async def _execute_hermes_atomic_subcall(",
        end_marker="system_prompt=HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT",
        claim_categories=("Hermes invocation",),
        claim=(
            "RLM sub-calls enter the configured AgentRuntime through "
            "execute_task_to_result with no tools and the RLM system prompt."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/orchestrator/hermes_runtime.py",
        start_marker="async def execute_task(",
        end_marker='args.extend(["-q", full_prompt])',
        claim_categories=("Hermes invocation",),
        claim=(
            "HermesCliRuntime uses the existing Hermes chat tool path, including "
            "quiet mode and --source tool, rather than a new RLM REPL."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/core/ac_tree.py",
        start_marker="class ACTree:",
        end_marker="def get_node",
        claim_categories=("Guardrails",),
        claim=(
            "ACTree stores the configured maximum depth and rejects added nodes that "
            "exceed that limit."
        ),
    ),
    RLMBenchmarkEvidenceSpec(
        source_path="src/ouroboros/persistence/event_store.py",
        start_marker="async def append",
        end_marker="async def replay",
        claim_categories=("Trace replay",),
        claim=(
            "EventStore appends and replays aggregate events, providing the durable "
            "basis for RLM trace reconstruction."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RLMRunConfig:
    """Configuration for one isolated RLM command invocation."""

    target: str
    cwd: Path
    fixture_id: str | None = None
    initial_prompt: str | None = None
    max_depth: int = MAX_RLM_AC_TREE_DEPTH
    ambiguity_threshold: float = MAX_RLM_AMBIGUITY_THRESHOLD
    chunk_line_limit: int = DEFAULT_RLM_ATOMIC_CHUNK_LINE_LIMIT
    max_atomic_chunks: int = DEFAULT_RLM_MAX_ATOMIC_CHUNKS
    max_iterations: int = DEFAULT_RLM_MAX_ITERATIONS
    benchmark_id: str | None = None
    dry_run: bool = False
    debug: bool = False
    hermes_runtime: AgentRuntime | None = field(default=None, compare=False, repr=False)
    trace_store: RLMTraceStore | None = field(default=None, compare=False, repr=False)


class RLMRunLifecycleState(StrEnum):
    """Outer Ouroboros run states for the isolated RLM path."""

    INITIALIZED = "initialized"
    GUARDING = "guarding"
    SCHEDULING = "scheduling"
    RUNNING_NODE = "running_node"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RLMNodeLifecycleState(StrEnum):
    """Outer-owned RLM node states."""

    QUEUED = "queued"
    PREPARING = "preparing"
    CONTEXT_BOUND = "context_bound"
    AWAITING_HERMES = "awaiting_hermes"
    VALIDATING_RESPONSE = "validating_response"
    COMMITTING = "committing"
    BLOCKED_RETRY = "blocked_retry"
    DECOMPOSED = "decomposed"
    ATOMIC_COMPLETE = "atomic_complete"
    SUMMARY_COMPLETE = "summary_complete"
    SYNTHESIS_COMPLETE = "synthesis_complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether this node state stops recursive work for that node."""
        return self in {
            RLMNodeLifecycleState.DECOMPOSED,
            RLMNodeLifecycleState.ATOMIC_COMPLETE,
            RLMNodeLifecycleState.SUMMARY_COMPLETE,
            RLMNodeLifecycleState.SYNTHESIS_COMPLETE,
            RLMNodeLifecycleState.FAILED,
            RLMNodeLifecycleState.CANCELLED,
        }


class RLMTerminationReason(StrEnum):
    """Outer-layer stop reasons for RLM scheduling."""

    DRY_RUN_READY = "dry_run_ready"
    ROOT_ATOMIC_COMPLETED = "root_atomic_completed"
    PARENT_SYNTHESIS_COMPLETED = "parent_synthesis_completed"
    WORK_QUEUE_EXHAUSTED = "work_queue_exhausted"
    MAX_DEPTH_REACHED = "max_depth_reached"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    NODE_FAILED = "node_failed"
    GUARDRAIL_FAILED = "guardrail_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RLMScaffoldTransition:
    """One outer-scaffold state transition for replay and debugging."""

    iteration: int
    subject: Literal["run", "node"]
    subject_id: str
    from_state: str | None
    to_state: str
    decision: str
    reason: str | None = None
    causal_parent_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the transition in a trace-friendly shape."""
        return {
            "iteration": self.iteration,
            "subject": self.subject,
            "subject_id": self.subject_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "decision": self.decision,
            "reason": self.reason,
            "causal_parent_event_id": self.causal_parent_event_id,
        }


@dataclass(frozen=True, slots=True)
class RLMScaffoldNode:
    """Outer-owned RLM tree node linked to an AC tree node."""

    rlm_node_id: str
    ac_node_id: str
    mode: str
    depth: int
    state: RLMNodeLifecycleState = RLMNodeLifecycleState.QUEUED
    parent_node_id: str | None = None
    parent_ac_node_id: str | None = None
    parent_call_id: str | None = None
    child_node_ids: tuple[str, ...] = ()
    selected_chunk_ids: tuple[str, ...] = ()
    retry_count: int = 0
    terminal_reason: RLMTerminationReason | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the RLM node without exposing mutable internals."""
        return {
            "rlm_node_id": self.rlm_node_id,
            "ac_node_id": self.ac_node_id,
            "mode": self.mode,
            "depth": self.depth,
            "state": self.state.value,
            "parent_node_id": self.parent_node_id,
            "parent_ac_node_id": self.parent_ac_node_id,
            "parent_call_id": self.parent_call_id,
            "child_node_ids": list(self.child_node_ids),
            "selected_chunk_ids": list(self.selected_chunk_ids),
            "retry_count": self.retry_count,
            "terminal_reason": self.terminal_reason.value
            if self.terminal_reason is not None
            else None,
        }


@dataclass(slots=True)
class RLMOuterScaffoldState:
    """Executable outer Ouroboros state for one isolated RLM run.

    Hermes only receives prompt envelopes. This object owns scheduling, linked
    AC/RLM node state, recursion limits, iteration limits, and stop decisions.
    """

    run_id: str
    target: str
    max_depth: int
    ambiguity_threshold: float
    max_iterations: int
    run_state: RLMRunLifecycleState = RLMRunLifecycleState.INITIALIZED
    ac_tree: ACTree = field(default_factory=lambda: ACTree(max_depth=MAX_RLM_AC_TREE_DEPTH))
    nodes: dict[str, RLMScaffoldNode] = field(default_factory=dict)
    work_queue: list[str] = field(default_factory=list)
    iteration: int = 0
    termination_reason: RLMTerminationReason | None = None
    transitions: list[RLMScaffoldTransition] = field(default_factory=list)

    @classmethod
    def initialize(cls, config: RLMRunConfig) -> RLMOuterScaffoldState:
        """Create the root AC/RLM nodes before Hermes can affect state."""
        ac_tree = ACTree(max_depth=config.max_depth)
        root_content = config.initial_prompt or f"Execute RLM target {config.target!r}"
        root_ac = ACNode(
            id=RLM_ROOT_AC_NODE_ID,
            content=root_content,
            depth=0,
            status=ACStatus.PENDING,
            metadata={
                "rlm_node_id": RLM_ROOT_NODE_ID,
                "mode": RLM_HERMES_EXECUTE_ATOMIC_MODE,
                "stable_identity": f"rlm-root:{config.target}",
                "fixture_id": config.fixture_id,
                "initial_prompt": config.initial_prompt,
            },
        )
        ac_tree.add_node(root_ac)
        state = cls(
            run_id=RLM_GENERATION_ID,
            target=config.target,
            max_depth=config.max_depth,
            ambiguity_threshold=config.ambiguity_threshold,
            max_iterations=config.max_iterations,
            ac_tree=ac_tree,
            nodes={
                RLM_ROOT_NODE_ID: RLMScaffoldNode(
                    rlm_node_id=RLM_ROOT_NODE_ID,
                    ac_node_id=RLM_ROOT_AC_NODE_ID,
                    mode=RLM_HERMES_EXECUTE_ATOMIC_MODE,
                    depth=0,
                )
            },
            work_queue=[RLM_ROOT_NODE_ID],
        )
        state._record_run_transition(
            None,
            RLMRunLifecycleState.INITIALIZED,
            decision="initialize_rlm_root",
        )
        return state

    def enter_guarding(self) -> None:
        """Record the outer guardrail phase."""
        self._transition_run(RLMRunLifecycleState.GUARDING, decision="validate_guardrails")

    def complete_guarding(self) -> None:
        """Move to scheduling after config guardrails have passed."""
        self._transition_run(RLMRunLifecycleState.SCHEDULING, decision="guardrails_passed")

    def mark_dry_run_ready(self) -> None:
        """Terminate before Hermes calls for validation-only runs."""
        self.termination_reason = RLMTerminationReason.DRY_RUN_READY
        self._transition_run(
            RLMRunLifecycleState.COMPLETED,
            decision="dry_run_no_hermes",
            reason=RLMTerminationReason.DRY_RUN_READY,
        )

    def select_node(self, rlm_node_id: str | None = None) -> RLMScaffoldNode:
        """Select the next outer-owned node and advance the iteration counter."""
        if self.iteration >= self.max_iterations:
            self.fail_run(RLMTerminationReason.MAX_ITERATIONS_REACHED)
            msg = f"RLM max iterations reached ({self.max_iterations})"
            raise ValueError(msg)

        if rlm_node_id is None:
            if not self.work_queue:
                self.complete_run(RLMTerminationReason.WORK_QUEUE_EXHAUSTED)
                msg = "RLM work queue exhausted"
                raise ValueError(msg)
            rlm_node_id = self.work_queue.pop(0)
        elif rlm_node_id in self.work_queue:
            self.work_queue.remove(rlm_node_id)

        node = self.nodes[rlm_node_id]
        self.iteration += 1
        self._transition_run(
            RLMRunLifecycleState.RUNNING_NODE,
            decision="select_node",
            causal_parent_event_id=node.parent_call_id,
        )
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.PREPARING,
            decision="selected_for_execution",
            causal_parent_event_id=node.parent_call_id,
        )
        return self.nodes[rlm_node_id]

    def bind_context(
        self,
        rlm_node_id: str,
        *,
        selected_chunk_ids: Sequence[str] = (),
    ) -> None:
        """Record that Ouroboros selected bounded context for a node."""
        node = self.nodes[rlm_node_id]
        self.nodes[rlm_node_id] = replace(
            node,
            selected_chunk_ids=tuple(selected_chunk_ids),
        )
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.CONTEXT_BOUND,
            decision="bind_bounded_context",
            causal_parent_event_id=node.parent_call_id,
        )

    def mark_awaiting_hermes(self, rlm_node_id: str) -> None:
        """Record the adapter boundary before invoking Hermes."""
        node = self.nodes[rlm_node_id]
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.AWAITING_HERMES,
            decision="invoke_hermes_runtime",
            causal_parent_event_id=node.parent_call_id,
        )

    def begin_response_validation(self, rlm_node_id: str) -> None:
        """Record that control returned to Ouroboros after a Hermes call."""
        node = self.nodes[rlm_node_id]
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.VALIDATING_RESPONSE,
            decision="validate_hermes_response",
            causal_parent_event_id=node.parent_call_id,
        )

    def complete_node(
        self,
        rlm_node_id: str,
        terminal_state: RLMNodeLifecycleState,
        *,
        reason: RLMTerminationReason,
        finish_run: bool = False,
    ) -> None:
        """Commit a terminal node decision owned by Ouroboros."""
        if not terminal_state.is_terminal:
            msg = f"{terminal_state.value} is not a terminal RLM node state"
            raise ValueError(msg)

        node = self.nodes[rlm_node_id]
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.COMMITTING,
            decision="commit_outer_state",
            causal_parent_event_id=node.parent_call_id,
        )
        node = self.nodes[rlm_node_id]
        self.nodes[rlm_node_id] = replace(node, terminal_reason=reason)
        self._transition_node(
            rlm_node_id,
            terminal_state,
            decision="node_terminal",
            reason=reason,
            causal_parent_event_id=node.parent_call_id,
        )
        self._mark_ac_node_terminal(rlm_node_id, terminal_state)

        if finish_run:
            if terminal_state == RLMNodeLifecycleState.FAILED:
                self.fail_run(reason)
            else:
                self.complete_run(reason)
        elif self.work_queue:
            self._transition_run(RLMRunLifecycleState.SCHEDULING, decision="schedule_next_node")

    def schedule_atomic_chunk_children(
        self,
        *,
        parent_node_id: str,
        chunks: Sequence[Mapping[str, Any]],
        parent_call_id: str,
    ) -> tuple[str, ...]:
        """Create queued child RLM/AC nodes for chunk-level atomic recursion."""
        parent = self.nodes[parent_node_id]
        child_depth = parent.depth + 1
        if child_depth > self.max_depth:
            self.complete_node(
                parent_node_id,
                RLMNodeLifecycleState.FAILED,
                reason=RLMTerminationReason.MAX_DEPTH_REACHED,
                finish_run=True,
            )
            return ()

        child_node_ids: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_id = str(chunk["chunk_id"])
            child_node_id = f"rlm_node_atomic_chunk_{index:03d}"
            child_ac_node_id = f"rlm_ac_atomic_chunk_{index:03d}"
            parent_trace_id = _trace_id_for_call(parent_call_id)
            child_node_ids.append(child_node_id)
            child_ac = ACNode(
                id=child_ac_node_id,
                content=f"Execute bounded RLM chunk {chunk_id}",
                depth=child_depth,
                parent_id=parent.ac_node_id,
                status=ACStatus.EXECUTING,
                is_atomic=True,
                originating_subcall_trace_id=parent_trace_id,
                metadata={
                    "rlm_node_id": child_node_id,
                    "mode": RLM_HERMES_EXECUTE_ATOMIC_MODE,
                    "chunk_id": chunk_id,
                    "stable_identity": f"rlm-chunk:{parent.ac_node_id}:{chunk_id}",
                    "originating_subcall_trace_id": parent_trace_id,
                },
            )
            self.ac_tree.add_node(child_ac)
            self.nodes[child_node_id] = RLMScaffoldNode(
                rlm_node_id=child_node_id,
                ac_node_id=child_ac_node_id,
                mode=RLM_HERMES_EXECUTE_ATOMIC_MODE,
                depth=child_depth,
                parent_node_id=parent_node_id,
                parent_ac_node_id=parent.ac_node_id,
                parent_call_id=parent_call_id,
                selected_chunk_ids=(chunk_id,),
            )
            self.work_queue.append(child_node_id)
            self._record_node_transition(
                child_node_id,
                None,
                RLMNodeLifecycleState.QUEUED,
                decision="schedule_atomic_chunk_child",
                causal_parent_event_id=parent_call_id,
            )

        self.nodes[parent_node_id] = replace(
            self.nodes[parent_node_id],
            child_node_ids=tuple(child_node_ids),
        )
        self._transition_node(
            parent_node_id,
            RLMNodeLifecycleState.DECOMPOSED,
            decision="schedule_chunk_recursion",
            causal_parent_event_id=parent_call_id,
        )
        self._transition_run(RLMRunLifecycleState.SCHEDULING, decision="chunk_children_queued")
        return tuple(child_node_ids)

    def schedule_benchmark_validation_child(
        self,
        *,
        parent_node_id: str,
        parent_call_id: str,
        chunk_id: str,
        benchmark_id: str,
        order: int,
    ) -> tuple[str, str] | None:
        """Create a nested child node used to validate benchmark recursion depth."""
        parent = self.nodes[parent_node_id]
        child_depth = parent.depth + 1
        if child_depth > self.max_depth:
            return None

        child_node_id = f"rlm_node_benchmark_validation_{order:03d}"
        child_ac_node_id = f"rlm_ac_benchmark_validation_{order:03d}"
        parent_trace_id = _trace_id_for_call(parent_call_id)
        child_ac = ACNode(
            id=child_ac_node_id,
            content=f"Validate RLM benchmark recursion for chunk {chunk_id}",
            depth=child_depth,
            parent_id=parent.ac_node_id,
            status=ACStatus.EXECUTING,
            is_atomic=True,
            originating_subcall_trace_id=parent_trace_id,
            metadata={
                "rlm_node_id": child_node_id,
                "mode": RLM_HERMES_EXECUTE_ATOMIC_MODE,
                "benchmark_id": benchmark_id,
                "chunk_id": chunk_id,
                "stable_identity": (
                    f"rlm-benchmark-validation:{benchmark_id}:{parent.ac_node_id}:{chunk_id}"
                ),
                "originating_subcall_trace_id": parent_trace_id,
            },
        )
        self.ac_tree.add_node(child_ac)
        self.nodes[child_node_id] = RLMScaffoldNode(
            rlm_node_id=child_node_id,
            ac_node_id=child_ac_node_id,
            mode=RLM_HERMES_EXECUTE_ATOMIC_MODE,
            depth=child_depth,
            parent_node_id=parent_node_id,
            parent_ac_node_id=parent.ac_node_id,
            parent_call_id=parent_call_id,
            selected_chunk_ids=(chunk_id,),
        )
        self.nodes[parent_node_id] = replace(
            parent,
            child_node_ids=(*parent.child_node_ids, child_node_id),
        )
        self.work_queue.append(child_node_id)
        self._record_node_transition(
            child_node_id,
            None,
            RLMNodeLifecycleState.QUEUED,
            decision="schedule_benchmark_validation_child",
            causal_parent_event_id=parent_call_id,
        )
        self._transition_run(
            RLMRunLifecycleState.SCHEDULING,
            decision="benchmark_validation_child_queued",
            causal_parent_event_id=parent_call_id,
        )
        return child_node_id, child_ac_node_id

    def prepare_parent_synthesis(self, rlm_node_id: str) -> None:
        """Record that completed children are being rolled into a parent."""
        if self.iteration >= self.max_iterations:
            self.fail_run(RLMTerminationReason.MAX_ITERATIONS_REACHED)
            msg = f"RLM max iterations reached ({self.max_iterations})"
            raise ValueError(msg)

        self.iteration += 1
        self._transition_run(RLMRunLifecycleState.SYNTHESIZING, decision="children_terminal")
        node = self.nodes[rlm_node_id]
        self._transition_node(
            rlm_node_id,
            RLMNodeLifecycleState.PREPARING,
            decision="schedule_parent_synthesis",
            causal_parent_event_id=node.parent_call_id,
        )

    def complete_run(self, reason: RLMTerminationReason) -> None:
        """Mark the outer RLM run completed by an Ouroboros stop condition."""
        self.termination_reason = reason
        self._transition_run(
            RLMRunLifecycleState.COMPLETED,
            decision="termination_condition_satisfied",
            reason=reason,
        )

    def fail_run(self, reason: RLMTerminationReason) -> None:
        """Mark the outer RLM run failed by an Ouroboros stop condition."""
        self.termination_reason = reason
        self._transition_run(
            RLMRunLifecycleState.FAILED,
            decision="termination_condition_failed",
            reason=reason,
        )

    def prompt_context_for_node(self, rlm_node_id: str) -> dict[str, Any]:
        """Return outer-scaffold metadata embedded in Hermes envelopes."""
        node = self.nodes[rlm_node_id]
        return {
            "schema_version": "rlm.outer_scaffold.v1",
            "owner": "ouroboros",
            "run_id": self.run_id,
            "run_state": self.run_state.value,
            "active_node_id": rlm_node_id,
            "active_ac_node_id": node.ac_node_id,
            "active_node_state": node.state.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "work_queue": list(self.work_queue),
            "max_ac_depth": self.max_depth,
            "generated_rlm_tree_depth": self.generated_rlm_tree_depth,
            "ambiguity_threshold": self.ambiguity_threshold,
            "termination_reason": self.termination_reason.value
            if self.termination_reason is not None
            else None,
            "termination_conditions": [
                RLMTerminationReason.ROOT_ATOMIC_COMPLETED.value,
                RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED.value,
                RLMTerminationReason.WORK_QUEUE_EXHAUSTED.value,
                RLMTerminationReason.MAX_DEPTH_REACHED.value,
                RLMTerminationReason.MAX_ITERATIONS_REACHED.value,
                RLMTerminationReason.NODE_FAILED.value,
                RLMTerminationReason.CANCELLED.value,
            ],
        }

    @property
    def generated_rlm_tree_depth(self) -> int:
        """Return the maximum depth of RLM nodes generated by this run."""
        if not self.nodes:
            return 0
        return max(node.depth for node in self.nodes.values())

    @property
    def is_terminal(self) -> bool:
        """Return whether the outer scaffold has reached a terminal run state."""
        return self.run_state in {
            RLMRunLifecycleState.COMPLETED,
            RLMRunLifecycleState.FAILED,
            RLMRunLifecycleState.CANCELLED,
        }

    @property
    def has_converged(self) -> bool:
        """Return whether root work is terminal and no recursive work remains."""
        if self.run_state != RLMRunLifecycleState.COMPLETED:
            return False
        if self.termination_reason not in {
            RLMTerminationReason.ROOT_ATOMIC_COMPLETED,
            RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED,
            RLMTerminationReason.WORK_QUEUE_EXHAUSTED,
        }:
            return False
        if self.work_queue:
            return False

        root_node = self.nodes.get(RLM_ROOT_NODE_ID)
        if root_node is None or not root_node.state.is_terminal:
            return False

        root_ac = self.ac_tree.get_node(RLM_ROOT_AC_NODE_ID)
        return root_ac is not None and root_ac.status == ACStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        """Serialize the outer scaffold, RLM tree, and linked AC tree."""
        return {
            "schema_version": "rlm.outer_scaffold.v1",
            "run_id": self.run_id,
            "target": self.target,
            "run_state": self.run_state.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "max_depth": self.max_depth,
            "generated_rlm_tree_depth": self.generated_rlm_tree_depth,
            "ambiguity_threshold": self.ambiguity_threshold,
            "termination_reason": self.termination_reason.value
            if self.termination_reason is not None
            else None,
            "work_queue": list(self.work_queue),
            "rlm_nodes": {node_id: node.to_dict() for node_id, node in self.nodes.items()},
            "ac_tree": self.ac_tree.to_dict(),
            "transitions": [transition.to_dict() for transition in self.transitions],
        }

    def _mark_ac_node_terminal(
        self,
        rlm_node_id: str,
        terminal_state: RLMNodeLifecycleState,
    ) -> None:
        ac_node = self.ac_tree.get_node(self.nodes[rlm_node_id].ac_node_id)
        if ac_node is None:
            return
        if terminal_state == RLMNodeLifecycleState.FAILED:
            self.ac_tree.update_node(ac_node.with_status(ACStatus.FAILED))
        elif terminal_state in {
            RLMNodeLifecycleState.ATOMIC_COMPLETE,
            RLMNodeLifecycleState.SUMMARY_COMPLETE,
            RLMNodeLifecycleState.SYNTHESIS_COMPLETE,
        }:
            self.ac_tree.update_node(ac_node.with_status(ACStatus.COMPLETED))

    def _transition_run(
        self,
        to_state: RLMRunLifecycleState,
        *,
        decision: str,
        reason: RLMTerminationReason | None = None,
        causal_parent_event_id: str | None = None,
    ) -> None:
        from_state = self.run_state
        self.run_state = to_state
        self._record_run_transition(
            from_state,
            to_state,
            decision=decision,
            reason=reason,
            causal_parent_event_id=causal_parent_event_id,
        )

    def _transition_node(
        self,
        rlm_node_id: str,
        to_state: RLMNodeLifecycleState,
        *,
        decision: str,
        reason: RLMTerminationReason | None = None,
        causal_parent_event_id: str | None = None,
    ) -> None:
        node = self.nodes[rlm_node_id]
        from_state = node.state
        self.nodes[rlm_node_id] = replace(node, state=to_state)
        self._record_node_transition(
            rlm_node_id,
            from_state,
            to_state,
            decision=decision,
            reason=reason,
            causal_parent_event_id=causal_parent_event_id,
        )

    def _record_run_transition(
        self,
        from_state: RLMRunLifecycleState | None,
        to_state: RLMRunLifecycleState,
        *,
        decision: str,
        reason: RLMTerminationReason | None = None,
        causal_parent_event_id: str | None = None,
    ) -> None:
        self.transitions.append(
            RLMScaffoldTransition(
                iteration=self.iteration,
                subject="run",
                subject_id=self.run_id,
                from_state=from_state.value if from_state is not None else None,
                to_state=to_state.value,
                decision=decision,
                reason=reason.value if reason is not None else None,
                causal_parent_event_id=causal_parent_event_id,
            )
        )

    def _record_node_transition(
        self,
        rlm_node_id: str,
        from_state: RLMNodeLifecycleState | None,
        to_state: RLMNodeLifecycleState,
        *,
        decision: str,
        reason: RLMTerminationReason | None = None,
        causal_parent_event_id: str | None = None,
    ) -> None:
        self.transitions.append(
            RLMScaffoldTransition(
                iteration=self.iteration,
                subject="node",
                subject_id=rlm_node_id,
                from_state=from_state.value if from_state is not None else None,
                to_state=to_state.value,
                decision=decision,
                reason=reason.value if reason is not None else None,
                causal_parent_event_id=causal_parent_event_id,
            )
        )


@dataclass(frozen=True, slots=True)
class RLMHermesCallContext:
    """Recursive call ancestry carried into one Hermes invocation."""

    call_id: str
    parent_call_id: str | None = None
    depth: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            msg = "RLM Hermes call context call_id must be a non-empty string"
            raise ValueError(msg)
        if self.parent_call_id is not None:
            if not isinstance(self.parent_call_id, str) or not self.parent_call_id.strip():
                msg = "RLM Hermes call context parent_call_id must be a non-empty string or None"
                raise ValueError(msg)
        if isinstance(self.depth, bool) or not isinstance(self.depth, int):
            msg = "RLM Hermes call context depth must be an integer"
            raise TypeError(msg)
        if self.depth < 0:
            msg = "RLM Hermes call context depth must be non-negative"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the call ancestry embedded in Hermes prompt envelopes."""
        return {
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "depth": self.depth,
        }

    def child(self, call_id: str) -> RLMHermesCallContext:
        """Create the recursive call context for a direct child Hermes call."""
        return RLMHermesCallContext(
            call_id=call_id,
            parent_call_id=self.call_id,
            depth=self.depth + 1,
        )


@dataclass(frozen=True, slots=True)
class RLMHermesSubcall:
    """One Hermes inner-LM call made by the RLM outer scaffold."""

    mode: RLMHermesMode
    generation_id: str
    rlm_node_id: str
    ac_node_id: str
    prompt: str = ""
    completion: str = ""
    parent_call_id: str | None = None
    depth: int = 0
    exit_code: int = 0
    call_id: str | None = None
    subcall_id: str | None = None
    trace_id: str | None = None
    parent_trace_id: str | None = None
    causal_parent_event_id: str | None = None
    chunk_id: str | None = None
    selected_chunk_ids: tuple[str, ...] = ()
    generated_child_ac_node_ids: tuple[str, ...] = ()
    resume_handle_id: str | None = None
    runtime_handle_id: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    success: bool | None = None
    elapsed_ms: int | None = None
    adapter_error: dict[str, Any] | None = None
    system_prompt_hash: str | None = None
    resume_handle: RuntimeHandle | None = field(default=None, compare=False, repr=False)

    def to_trace_record(self) -> RLMHermesTraceRecord:
        """Return the replayable Hermes trace record for this sub-call."""
        prompt_trace = _trace_payload_from_prompt(self.prompt)
        selected_chunk_ids = self.selected_chunk_ids
        if not selected_chunk_ids and self.chunk_id is not None:
            selected_chunk_ids = (self.chunk_id,)
        if not selected_chunk_ids and self.prompt:
            selected_chunk_ids = _selected_chunk_ids_from_prompt(self.prompt)
        generated_child_ac_node_ids = self.generated_child_ac_node_ids
        if not generated_child_ac_node_ids and self.prompt:
            generated_child_ac_node_ids = _string_tuple(
                prompt_trace.get("generated_child_ac_node_ids")
            )
        runtime_handle_id = self.runtime_handle_id or _runtime_handle_trace_id(self.resume_handle)
        return RLMHermesTraceRecord(
            prompt=self.prompt,
            completion=self.completion,
            parent_call_id=self.parent_call_id,
            depth=self.depth,
            trace_id=self.trace_id or _string_or_none(prompt_trace.get("trace_id")),
            subcall_id=self.subcall_id or _string_or_none(prompt_trace.get("subcall_id")),
            parent_trace_id=self.parent_trace_id
            or _string_or_none(prompt_trace.get("parent_trace_id")),
            causal_parent_event_id=self.causal_parent_event_id
            or _string_or_none(prompt_trace.get("causal_parent_event_id")),
            call_id=self.call_id,
            mode=self.mode,
            generation_id=self.generation_id,
            rlm_node_id=self.rlm_node_id,
            ac_node_id=self.ac_node_id,
            selected_chunk_ids=selected_chunk_ids,
            generated_child_ac_node_ids=generated_child_ac_node_ids,
            resume_handle_id=self.resume_handle_id or runtime_handle_id,
            runtime_handle_id=runtime_handle_id,
            prompt_hash=self.prompt_hash or hash_trace_text(self.prompt),
            response_hash=self.response_hash or hash_trace_text(self.completion),
            success=self.success if self.success is not None else self.exit_code == 0,
            exit_code=self.exit_code,
            elapsed_ms=self.elapsed_ms,
            adapter_error=self.adapter_error,
            system_prompt_hash=self.system_prompt_hash,
        )


RLMSubcallCompletionStatus = Literal["completed", "failed"]


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    """Return a mapping when the value is an object, otherwise an empty object."""
    return value if isinstance(value, Mapping) else {}


def _string_or_none(value: object) -> str | None:
    """Return a string value without coercing non-strings into prompt context."""
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    """Return only string items from a prompt list for deterministic context."""
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return only string items from JSON-ish scalar or sequence fields."""
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _new_rlm_hermes_subcall_id() -> str:
    """Return a unique RLM-owned Hermes sub-call ID."""
    return f"rlm_subcall_{uuid4().hex}"


def _required_string_tuple(data: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    """Return a tuple of strings from a summary payload field."""
    value = data.get(field_name)
    if isinstance(value, str) or not isinstance(value, list | tuple):
        msg = f"parent node summary {field_name} must be an array"
        raise ValueError(msg)

    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            msg = f"parent node summary {field_name}[{index}] must be a non-empty string"
            raise ValueError(msg)
        strings.append(item)
    return tuple(strings)


def _required_non_negative_int(data: Mapping[str, Any], field_name: str) -> int:
    """Return a non-negative integer from a summary payload field."""
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"parent node summary {field_name} must be an integer"
        raise ValueError(msg)
    if value < 0:
        msg = f"parent node summary {field_name} must be non-negative"
        raise ValueError(msg)
    return value


def _require_summary_string(data: Mapping[str, Any], field_name: str) -> str:
    """Return a non-empty string from a parent summary payload field."""
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"parent node summary {field_name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _require_parent_summary_schema(
    data: Mapping[str, Any],
) -> Literal["rlm.parent_node_summary.v1"]:
    """Return the validated parent summary schema version literal."""
    value = data.get("schema_version")
    if value != RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION:
        msg = f"parent node summary schema_version must be {RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION}"
        raise ValueError(msg)
    return RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION


def _json_mapping_or_empty(payload: str) -> Mapping[str, Any]:
    """Parse a JSON object string when possible without rejecting raw completions."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _trace_payload_from_prompt(prompt: str) -> Mapping[str, Any]:
    """Extract the trace envelope from a rendered Hermes prompt."""
    envelope = _json_mapping_or_empty(prompt)
    return _mapping_or_empty(envelope.get("trace"))


def _selected_chunk_ids_from_prompt(prompt: str) -> tuple[str, ...]:
    """Extract selected chunk IDs from an internal RLM prompt envelope."""
    trace = _trace_payload_from_prompt(prompt)
    return tuple(_string_list(trace.get("selected_chunk_ids")))


def _trace_id_for_call(call_id: str | None) -> str | None:
    """Return the deterministic trace-record ID for a Hermes call ID."""
    return f"rlm_trace_{call_id}" if call_id else None


def _runtime_handle_trace_id(handle: RuntimeHandle | None) -> str | None:
    """Extract the most stable readable ID from a RuntimeHandle for trace replay."""
    if handle is None:
        return None
    return (
        handle.control_session_id
        or handle.resume_session_id
        or handle.conversation_id
        or handle.previous_response_id
    )


def _subcall_id_from_task_result(task_result: Any) -> str | None:
    """Extract a Hermes adapter sub-call ID from collected runtime messages."""
    for message in reversed(getattr(task_result, "messages", ()) or ()):
        data = getattr(message, "data", None)
        if not isinstance(data, Mapping):
            continue
        subcall_id = _string_or_none(data.get("subcall_id"))
        if subcall_id is not None:
            return subcall_id
    return None


def _question_payload_from_subcall(subcall: RLMHermesSubcall) -> dict[str, Any]:
    """Extract the child AC question shape from the prompt envelope."""
    envelope = _json_mapping_or_empty(subcall.prompt)
    ac_node = _mapping_or_empty(envelope.get("ac_node"))
    objective = _mapping_or_empty(envelope.get("objective"))
    context = _mapping_or_empty(envelope.get("context"))
    trace = _mapping_or_empty(envelope.get("trace"))
    selected_chunk_ids = _string_list(trace.get("selected_chunk_ids"))
    if not selected_chunk_ids and subcall.chunk_id is not None:
        selected_chunk_ids = [subcall.chunk_id]
    return {
        "mode": subcall.mode,
        "rlm_node_id": subcall.rlm_node_id,
        "ac_node_id": subcall.ac_node_id,
        "title": _string_or_none(ac_node.get("title")),
        "statement": _string_or_none(ac_node.get("statement")),
        "prompt_summary": _string_or_none(context.get("prompt_summary")),
        "instruction": _string_or_none(objective.get("instruction")),
        "success_criteria": _string_list(objective.get("success_criteria")),
        "selected_chunk_ids": selected_chunk_ids,
    }


def _result_payload_from_subcall(subcall: RLMHermesSubcall) -> dict[str, Any]:
    """Normalize the Hermes completion while preserving the raw response."""
    parsed_completion = _json_mapping_or_empty(subcall.completion)
    confidence = parsed_completion.get("confidence")
    result_payload: dict[str, Any] = {
        "exit_code": subcall.exit_code,
        "completion": subcall.completion,
        "reported_result": parsed_completion.get("result"),
        "verdict": _string_or_none(parsed_completion.get("verdict")),
        "confidence": confidence
        if isinstance(confidence, int | float) and not isinstance(confidence, bool)
        else None,
        "evidence_references": parsed_completion.get("evidence_references", [])
        if isinstance(parsed_completion.get("evidence_references", []), list)
        else [],
        "residual_gaps": parsed_completion.get("residual_gaps", [])
        if isinstance(parsed_completion.get("residual_gaps", []), list)
        else [],
    }
    return result_payload


def _result_payload_summary(result_payload: Mapping[str, Any]) -> str | None:
    """Extract a compact child-result summary without requiring valid JSON output."""
    reported_result = result_payload.get("reported_result")
    if isinstance(reported_result, Mapping):
        summary = reported_result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    if isinstance(reported_result, str) and reported_result.strip():
        return reported_result

    completion = result_payload.get("completion")
    if isinstance(completion, str) and completion.strip():
        parsed_completion = _json_mapping_or_empty(completion)
        parsed_result = parsed_completion.get("result")
        if isinstance(parsed_result, Mapping):
            summary = parsed_result.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary
        if isinstance(parsed_result, str) and parsed_result.strip():
            return parsed_result

    return None


def _item_count(value: object) -> int:
    """Return a count for JSON-ish list fields carried in child result payloads."""
    return len(value) if isinstance(value, list | tuple) else 0


@dataclass(frozen=True, slots=True)
class RLMNormalizedChildACInput:
    """Deterministic child AC input passed from completed children to a parent."""

    question: dict[str, Any]
    result: dict[str, Any]
    status: dict[str, Any]
    ordering: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", dict(self.question))
        object.__setattr__(self, "result", dict(self.result))
        object.__setattr__(self, "status", dict(self.status))
        object.__setattr__(self, "ordering", dict(self.ordering))

        if not self.question:
            msg = "normalized child AC input question must not be empty"
            raise ValueError(msg)
        if not self.status:
            msg = "normalized child AC input status must not be empty"
            raise ValueError(msg)
        if not self.ordering:
            msg = "normalized child AC input ordering must not be empty"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the normalized child AC input shape for parent prompts."""
        return {
            "question": dict(self.question),
            "result": dict(self.result),
            "status": dict(self.status),
            "ordering": dict(self.ordering),
        }


@dataclass(frozen=True, slots=True)
class RLMRecordedSubcallResult:
    """Stable child result record carried by a parent RLM execution node."""

    order: int
    child_node_id: str
    child_ac_node_id: str
    completion_status: RLMSubcallCompletionStatus
    result_payload: dict[str, Any]
    question_payload: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    subcall_id: str | None = None
    chunk_id: str | None = None
    status_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_payload", dict(self.result_payload))
        question_payload = dict(self.question_payload)
        if not question_payload:
            question_payload = {
                "mode": self.status_metadata.get("mode"),
                "rlm_node_id": self.child_node_id,
                "ac_node_id": self.child_ac_node_id,
                "title": None,
                "statement": None,
                "prompt_summary": None,
                "instruction": None,
                "success_criteria": [],
                "selected_chunk_ids": [self.chunk_id] if self.chunk_id is not None else [],
            }
        object.__setattr__(self, "question_payload", question_payload)
        object.__setattr__(self, "status_metadata", dict(self.status_metadata))

        if self.order < 0:
            msg = "recorded sub-call order must be non-negative"
            raise ValueError(msg)
        if not self.child_node_id.strip():
            msg = "recorded sub-call child_node_id must not be empty"
            raise ValueError(msg)
        if not self.child_ac_node_id.strip():
            msg = "recorded sub-call child_ac_node_id must not be empty"
            raise ValueError(msg)
        if self.completion_status not in ("completed", "failed"):
            msg = "recorded sub-call completion_status must be completed or failed"
            raise ValueError(msg)

    @classmethod
    def from_hermes_subcall(
        cls,
        *,
        order: int,
        subcall: RLMHermesSubcall,
    ) -> RLMRecordedSubcallResult:
        """Create a parent-owned child result record from one Hermes sub-call."""
        completion_status: RLMSubcallCompletionStatus = (
            "completed" if subcall.exit_code == 0 else "failed"
        )
        status_metadata: dict[str, Any] = {
            "mode": subcall.mode,
            "generation_id": subcall.generation_id,
            "parent_call_id": subcall.parent_call_id,
            "subcall_id": subcall.subcall_id,
            "depth": subcall.depth,
            "exit_code": subcall.exit_code,
            "resume_handle_present": subcall.resume_handle is not None,
        }
        return cls(
            order=order,
            child_node_id=subcall.rlm_node_id,
            child_ac_node_id=subcall.ac_node_id,
            call_id=subcall.call_id,
            subcall_id=subcall.subcall_id,
            chunk_id=subcall.chunk_id,
            completion_status=completion_status,
            question_payload=_question_payload_from_subcall(subcall),
            status_metadata=status_metadata,
            result_payload=_result_payload_from_subcall(subcall),
        )

    def to_child_ac_input(self) -> RLMNormalizedChildACInput:
        """Normalize the child sub-call result for parent AC synthesis."""
        status = dict(self.status_metadata)
        status["completion_status"] = self.completion_status
        return RLMNormalizedChildACInput(
            question=dict(self.question_payload),
            result=dict(self.result_payload),
            status=status,
            ordering={
                "order": self.order,
                "sibling_index": self.order,
                "child_node_id": self.child_node_id,
                "child_ac_node_id": self.child_ac_node_id,
                "call_id": self.call_id,
                "subcall_id": self.subcall_id,
                "chunk_id": self.chunk_id,
                "generation_id": self.status_metadata.get("generation_id"),
                "parent_call_id": self.status_metadata.get("parent_call_id"),
                "depth": self.status_metadata.get("depth"),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the raw child output record shape."""
        return {
            "order": self.order,
            "child_node_id": self.child_node_id,
            "child_ac_node_id": self.child_ac_node_id,
            "call_id": self.call_id,
            "subcall_id": self.subcall_id,
            "chunk_id": self.chunk_id,
            "completion_status": self.completion_status,
            "status_metadata": dict(self.status_metadata),
            "question_payload": dict(self.question_payload),
            "result_payload": dict(self.result_payload),
        }


@dataclass(frozen=True, slots=True)
class RLMParentNodeSummary:
    """Structured parent rollup kept separate from raw child outputs."""

    parent_node_id: str
    parent_ac_node_id: str
    generation_id: str
    child_result_count: int
    completed_child_count: int
    failed_child_count: int
    child_result_ids: tuple[str, ...] = ()
    child_node_ids: tuple[str, ...] = ()
    child_ac_node_ids: tuple[str, ...] = ()
    child_call_ids: tuple[str, ...] = ()
    child_chunk_ids: tuple[str, ...] = ()
    child_completion_statuses: tuple[RLMSubcallCompletionStatus, ...] = ()
    schema_version: Literal["rlm.parent_node_summary.v1"] = RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION:
            msg = f"parent node summary schema_version must be {RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION}"
            raise ValueError(msg)
        if not self.parent_node_id.strip():
            msg = "parent node summary parent_node_id must not be empty"
            raise ValueError(msg)
        if not self.parent_ac_node_id.strip():
            msg = "parent node summary parent_ac_node_id must not be empty"
            raise ValueError(msg)
        if not self.generation_id.strip():
            msg = "parent node summary generation_id must not be empty"
            raise ValueError(msg)
        if self.child_result_count < 0:
            msg = "parent node summary child_result_count must be non-negative"
            raise ValueError(msg)
        if self.completed_child_count < 0 or self.failed_child_count < 0:
            msg = "parent node summary child status counts must be non-negative"
            raise ValueError(msg)
        if self.completed_child_count + self.failed_child_count != self.child_result_count:
            msg = "parent node summary child status counts must equal child_result_count"
            raise ValueError(msg)
        counted_fields = (
            ("child_result_ids", self.child_result_ids),
            ("child_node_ids", self.child_node_ids),
            ("child_ac_node_ids", self.child_ac_node_ids),
            ("child_completion_statuses", self.child_completion_statuses),
        )
        for field_name, values in counted_fields:
            if len(values) != self.child_result_count:
                msg = f"parent node summary {field_name} length must equal child_result_count"
                raise ValueError(msg)
        if len(self.child_call_ids) > self.child_result_count:
            msg = "parent node summary child_call_ids cannot exceed child_result_count"
            raise ValueError(msg)
        if len(self.child_chunk_ids) > self.child_result_count:
            msg = "parent node summary child_chunk_ids cannot exceed child_result_count"
            raise ValueError(msg)
        invalid_statuses = [
            status
            for status in self.child_completion_statuses
            if status not in ("completed", "failed")
        ]
        if invalid_statuses:
            msg = "parent node summary child_completion_statuses must be completed or failed"
            raise ValueError(msg)

    @classmethod
    def from_recorded_results(
        cls,
        *,
        parent_node_id: str,
        parent_ac_node_id: str,
        generation_id: str,
        recorded_results: tuple[RLMRecordedSubcallResult, ...],
    ) -> RLMParentNodeSummary:
        """Create a summary from ordered raw child output records."""
        completed_count = sum(
            1 for result in recorded_results if result.completion_status == "completed"
        )
        failed_count = sum(1 for result in recorded_results if result.completion_status == "failed")
        return cls(
            parent_node_id=parent_node_id,
            parent_ac_node_id=parent_ac_node_id,
            generation_id=generation_id,
            child_result_count=len(recorded_results),
            completed_child_count=completed_count,
            failed_child_count=failed_count,
            child_result_ids=tuple(
                f"{parent_node_id}:child_result:{result.order:03d}" for result in recorded_results
            ),
            child_node_ids=tuple(result.child_node_id for result in recorded_results),
            child_ac_node_ids=tuple(result.child_ac_node_id for result in recorded_results),
            child_call_ids=tuple(
                result.call_id for result in recorded_results if result.call_id is not None
            ),
            child_chunk_ids=tuple(
                result.chunk_id for result in recorded_results if result.chunk_id is not None
            ),
            child_completion_statuses=tuple(
                result.completion_status for result in recorded_results
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary metadata without raw child completions or payloads."""
        return {
            "schema_version": self.schema_version,
            "parent_node_id": self.parent_node_id,
            "parent_ac_node_id": self.parent_ac_node_id,
            "generation_id": self.generation_id,
            "child_result_count": self.child_result_count,
            "completed_child_count": self.completed_child_count,
            "failed_child_count": self.failed_child_count,
            "child_result_ids": list(self.child_result_ids),
            "child_node_ids": list(self.child_node_ids),
            "child_ac_node_ids": list(self.child_ac_node_ids),
            "child_call_ids": list(self.child_call_ids),
            "child_chunk_ids": list(self.child_chunk_ids),
            "child_completion_statuses": list(self.child_completion_statuses),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMParentNodeSummary:
        """Deserialize and validate the parent summary schema payload."""
        statuses = _required_string_tuple(data, "child_completion_statuses")
        invalid_statuses = [status for status in statuses if status not in ("completed", "failed")]
        if invalid_statuses:
            msg = "parent node summary child_completion_statuses must be completed or failed"
            raise ValueError(msg)

        return cls(
            schema_version=_require_parent_summary_schema(data),
            parent_node_id=_require_summary_string(data, "parent_node_id"),
            parent_ac_node_id=_require_summary_string(data, "parent_ac_node_id"),
            generation_id=_require_summary_string(data, "generation_id"),
            child_result_count=_required_non_negative_int(data, "child_result_count"),
            completed_child_count=_required_non_negative_int(data, "completed_child_count"),
            failed_child_count=_required_non_negative_int(data, "failed_child_count"),
            child_result_ids=_required_string_tuple(data, "child_result_ids"),
            child_node_ids=_required_string_tuple(data, "child_node_ids"),
            child_ac_node_ids=_required_string_tuple(data, "child_ac_node_ids"),
            child_call_ids=_required_string_tuple(data, "child_call_ids"),
            child_chunk_ids=_required_string_tuple(data, "child_chunk_ids"),
            child_completion_statuses=statuses,
        )


@dataclass(frozen=True, slots=True)
class RLMParentExecutionState:
    """Parent execution state for deterministic synthesis of child sub-calls."""

    parent_node_id: str
    parent_ac_node_id: str
    generation_id: str
    recorded_subcall_results: tuple[RLMRecordedSubcallResult, ...] = ()
    synthesized_summary: RLMParentNodeSummary | None = None

    def __post_init__(self) -> None:
        if not self.parent_node_id.strip():
            msg = "parent execution state parent_node_id must not be empty"
            raise ValueError(msg)
        if not self.parent_ac_node_id.strip():
            msg = "parent execution state parent_ac_node_id must not be empty"
            raise ValueError(msg)
        if not self.generation_id.strip():
            msg = "parent execution state generation_id must not be empty"
            raise ValueError(msg)

        orders = [result.order for result in self.recorded_subcall_results]
        if len(set(orders)) != len(orders):
            msg = "recorded sub-call order values must be unique within a parent"
            raise ValueError(msg)

        child_node_ids = [result.child_node_id for result in self.recorded_subcall_results]
        if len(set(child_node_ids)) != len(child_node_ids):
            msg = "recorded sub-call child_node_id values must be unique within a parent"
            raise ValueError(msg)

        child_ac_node_ids = [result.child_ac_node_id for result in self.recorded_subcall_results]
        if len(set(child_ac_node_ids)) != len(child_ac_node_ids):
            msg = "recorded sub-call child_ac_node_id values must be unique within a parent"
            raise ValueError(msg)

        if self.synthesized_summary is not None:
            if not isinstance(self.synthesized_summary, RLMParentNodeSummary):
                msg = "parent execution state synthesized_summary must be a parent node summary"
                raise TypeError(msg)
            expected_summary = synthesize_parent_node_summary(self)
            if self.synthesized_summary != expected_summary:
                msg = "parent execution state synthesized_summary must match recorded child results"
                raise ValueError(msg)

    @classmethod
    def from_hermes_subcalls(
        cls,
        *,
        parent_node_id: str,
        parent_ac_node_id: str,
        generation_id: str,
        subcalls: list[RLMHermesSubcall] | tuple[RLMHermesSubcall, ...],
    ) -> RLMParentExecutionState:
        """Record Hermes sub-call results in their scheduling order."""
        return cls(
            parent_node_id=parent_node_id,
            parent_ac_node_id=parent_ac_node_id,
            generation_id=generation_id,
            recorded_subcall_results=tuple(
                RLMRecordedSubcallResult.from_hermes_subcall(
                    order=index,
                    subcall=subcall,
                )
                for index, subcall in enumerate(subcalls)
            ),
        )

    def with_recorded_result(
        self,
        result: RLMRecordedSubcallResult,
    ) -> RLMParentExecutionState:
        """Return a new state with one additional recorded child result."""
        recorded_results = tuple(
            sorted(
                (*self.recorded_subcall_results, result),
                key=lambda recorded_result: recorded_result.order,
            )
        )
        return RLMParentExecutionState(
            parent_node_id=self.parent_node_id,
            parent_ac_node_id=self.parent_ac_node_id,
            generation_id=self.generation_id,
            recorded_subcall_results=recorded_results,
        )

    def with_synthesized_summary(
        self,
        summary: RLMParentNodeSummary | None = None,
    ) -> RLMParentExecutionState:
        """Return a parent state with a stored summary and unchanged child records."""
        synthesized_summary = summary or synthesize_parent_node_summary(self)
        return RLMParentExecutionState(
            parent_node_id=self.parent_node_id,
            parent_ac_node_id=self.parent_ac_node_id,
            generation_id=self.generation_id,
            recorded_subcall_results=self.recorded_subcall_results,
            synthesized_summary=synthesized_summary,
        )

    def ordered_child_results(self) -> tuple[RLMRecordedSubcallResult, ...]:
        """Return child results sorted by stable parent-owned order."""
        return tuple(sorted(self.recorded_subcall_results, key=lambda result: result.order))

    def to_child_results_context(self) -> list[dict[str, Any]]:
        """Serialize ordered child results for a parent Hermes synthesis prompt."""
        return [result.to_dict() for result in self.ordered_child_results()]

    def to_child_ac_input_context(self) -> list[dict[str, Any]]:
        """Serialize normalized child AC inputs separately from raw records."""
        return [result.to_child_ac_input().to_dict() for result in self.ordered_child_results()]

    def to_parent_node_summary(self) -> RLMParentNodeSummary:
        """Return the parent-stored summary, or build it without raw child outputs."""
        if self.synthesized_summary is not None:
            return self.synthesized_summary
        return synthesize_parent_node_summary(self)

    def to_parent_node_summary_context(self) -> dict[str, Any]:
        """Serialize the parent summary for Hermes synthesis prompts."""
        return self.to_parent_node_summary().to_dict()

    def to_synthesized_subcall_summary_context(self) -> dict[str, Any]:
        """Serialize the compact child rollup used when the parent LM resumes."""
        return synthesize_subcall_summary(self)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the parent execution state for traces or debug output."""
        return {
            "parent_node_id": self.parent_node_id,
            "parent_ac_node_id": self.parent_ac_node_id,
            "generation_id": self.generation_id,
            "parent_node_summary": self.to_parent_node_summary_context(),
            "synthesized_subcall_summary": self.to_synthesized_subcall_summary_context(),
            "recorded_subcall_results": self.to_child_results_context(),
            "normalized_child_ac_inputs": self.to_child_ac_input_context(),
        }


def capture_completed_hermes_subcall_result(
    parent_state: RLMParentExecutionState,
    *,
    order: int,
    subcall: RLMHermesSubcall,
) -> RLMParentExecutionState:
    """Capture one completed Hermes child result before parent control resumes."""
    if subcall.generation_id != parent_state.generation_id:
        msg = (
            "completed Hermes sub-call generation_id must match parent execution "
            "state generation_id"
        )
        raise ValueError(msg)

    return parent_state.with_recorded_result(
        RLMRecordedSubcallResult.from_hermes_subcall(
            order=order,
            subcall=subcall,
        )
    )


def synthesize_parent_node_summary(
    parent_state: RLMParentExecutionState,
) -> RLMParentNodeSummary:
    """Consume attached child sub-call results and return the parent rollup schema."""
    return RLMParentNodeSummary.from_recorded_results(
        parent_node_id=parent_state.parent_node_id,
        parent_ac_node_id=parent_state.parent_ac_node_id,
        generation_id=parent_state.generation_id,
        recorded_results=parent_state.ordered_child_results(),
    )


def synthesize_subcall_summary(parent_state: RLMParentExecutionState) -> dict[str, Any]:
    """Create the compact sub-call summary passed back into the resumed parent LM."""
    parent_node_summary = parent_state.to_parent_node_summary()
    ordered_results = parent_state.ordered_child_results()
    child_result_summaries: list[dict[str, Any]] = []

    for child_result_id, result in zip(
        parent_node_summary.child_result_ids,
        ordered_results,
        strict=True,
    ):
        result_payload = result.result_payload
        child_result_summaries.append(
            {
                "child_result_id": child_result_id,
                "order": result.order,
                "child_node_id": result.child_node_id,
                "child_ac_node_id": result.child_ac_node_id,
                "call_id": result.call_id,
                "chunk_id": result.chunk_id,
                "completion_status": result.completion_status,
                "mode": result.status_metadata.get("mode"),
                "exit_code": result.status_metadata.get("exit_code"),
                "verdict": _string_or_none(result_payload.get("verdict")),
                "confidence": result_payload.get("confidence"),
                "reported_summary": _result_payload_summary(result_payload),
                "evidence_reference_count": _item_count(result_payload.get("evidence_references")),
                "residual_gap_count": _item_count(result_payload.get("residual_gaps")),
            }
        )

    child_count = parent_node_summary.child_result_count
    return {
        "schema_version": RLM_SYNTHESIZED_SUBCALL_SUMMARY_SCHEMA_VERSION,
        "parent_node_id": parent_state.parent_node_id,
        "parent_ac_node_id": parent_state.parent_ac_node_id,
        "generation_id": parent_state.generation_id,
        "summary": (
            f"{child_count} child sub-call(s) recorded for parent synthesis: "
            f"{parent_node_summary.completed_child_count} completed, "
            f"{parent_node_summary.failed_child_count} failed."
        ),
        "parent_node_summary": parent_node_summary.to_dict(),
        "child_result_summaries": child_result_summaries,
    }


def _build_parent_execution_context(
    *,
    mode: RLMHermesMode,
    call_context: RLMHermesCallContext,
    rlm_node_id: str,
    ac_node_id: str,
    trace_id: str | None,
    parent_trace_id: str | None,
    parent_state: RLMParentExecutionState | None = None,
    child_order: int | None = None,
    sibling_count: int | None = None,
) -> dict[str, Any]:
    """Build parent-owned execution metadata for a Hermes prompt envelope."""
    if child_order is not None and child_order < 0:
        msg = "parent execution context child_order must be non-negative"
        raise ValueError(msg)
    if sibling_count is not None and sibling_count < 0:
        msg = "parent execution context sibling_count must be non-negative"
        raise ValueError(msg)

    recorded_results = parent_state.ordered_child_results() if parent_state is not None else ()
    completed_count = sum(
        1 for result in recorded_results if result.completion_status == "completed"
    )
    failed_count = sum(1 for result in recorded_results if result.completion_status == "failed")
    resolved_sibling_count = sibling_count
    if resolved_sibling_count is None:
        resolved_sibling_count = len(recorded_results) if parent_state is not None else 1

    return {
        "schema_version": RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
        "generation_id": parent_state.generation_id
        if parent_state is not None
        else RLM_GENERATION_ID,
        "mode": mode,
        "parent_node_id": parent_state.parent_node_id if parent_state is not None else None,
        "parent_ac_node_id": parent_state.parent_ac_node_id if parent_state is not None else None,
        "parent_call_id": call_context.parent_call_id,
        "parent_trace_id": parent_trace_id,
        "current_node_id": rlm_node_id,
        "current_ac_node_id": ac_node_id,
        "current_call_id": call_context.call_id,
        "current_trace_id": trace_id,
        "current_depth": call_context.depth,
        "child_order": child_order,
        "sibling_count": resolved_sibling_count,
        "prior_sibling_result_count": len(recorded_results),
        "completed_sibling_count": completed_count,
        "failed_sibling_count": failed_count,
        "recorded_child_result_ids": [
            f"{parent_state.parent_node_id}:child_result:{result.order:03d}"
            for result in recorded_results
        ]
        if parent_state is not None
        else [],
        "recorded_child_node_ids": [result.child_node_id for result in recorded_results],
        "recorded_child_ac_node_ids": [result.child_ac_node_id for result in recorded_results],
        "recorded_child_call_ids": [
            result.call_id for result in recorded_results if result.call_id is not None
        ],
        "recorded_child_chunk_ids": [
            result.chunk_id for result in recorded_results if result.chunk_id is not None
        ],
        "synthesized_summary_present": (
            parent_state.synthesized_summary is not None if parent_state is not None else False
        ),
    }


@dataclass(frozen=True, slots=True)
class RLMAtomicExecutionResult:
    """Atomic AC execution result produced through a Hermes sub-call."""

    ac_node_id: str
    generation_id: str
    hermes_subcall: RLMHermesSubcall
    success: bool
    final_message: str
    chunk_subcalls: tuple[RLMHermesSubcall, ...] = ()
    nested_benchmark_subcalls: tuple[RLMHermesSubcall, ...] = ()
    parent_execution_state: RLMParentExecutionState | None = None
    outer_scaffold_state: RLMOuterScaffoldState | None = None


@dataclass(frozen=True, slots=True)
class RLMRunResult:
    """Minimal result emitted by the RLM command bootstrap."""

    status: Literal["ready", "completed"]
    target: str
    target_kind: Literal["path", "prompt"]
    cwd: Path
    max_depth: int
    ambiguity_threshold: float
    message: str
    generation_id: str = RLM_GENERATION_ID
    hermes_subcall_count: int = 0
    atomic_execution: RLMAtomicExecutionResult | None = None
    benchmark_output: RLMBenchmarkOutput | None = None
    outer_scaffold_state: RLMOuterScaffoldState | None = None
    termination_reason: RLMTerminationReason | None = None


@dataclass(frozen=True, slots=True)
class RLMBenchmarkSourceEvidence:
    """One grounded source-file citation emitted by the RLM benchmark."""

    evidence_id: str
    source_path: str
    start_line: int
    end_line: int
    claim_categories: tuple[str, ...]
    claim: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the machine-readable benchmark evidence shape."""
        return {
            "evidence_id": self.evidence_id,
            "source_path": self.source_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "claim_categories": list(self.claim_categories),
            "claim": self.claim,
        }


@dataclass(frozen=True, slots=True)
class RLMBenchmarkOutput:
    """Machine-readable and Markdown benchmark artifact for one RLM run."""

    benchmark_id: str
    schema_version: str
    source_evidence: tuple[RLMBenchmarkSourceEvidence, ...]
    report_markdown: str
    generated_rlm_tree_depth: int | None = None

    @property
    def cited_source_file_count(self) -> int:
        """Return the number of distinct source files cited by the benchmark."""
        return len({evidence.source_path for evidence in self.source_evidence})

    def to_dict(self) -> dict[str, Any]:
        """Serialize the benchmark output with stable source evidence fields."""
        return {
            "schema_version": self.schema_version,
            "benchmark_id": self.benchmark_id,
            "generated_rlm_tree_depth": self.generated_rlm_tree_depth,
            "source_evidence": [evidence.to_dict() for evidence in self.source_evidence],
            "cited_source_file_count": self.cited_source_file_count,
            "report_markdown": self.report_markdown,
        }


def _is_within_path(path: Path, parent: Path) -> bool:
    """Return whether path is inside parent after best-effort resolution."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _target_path(config: RLMRunConfig) -> Path:
    """Return the expanded target path for path-like RLM invocations."""
    return (config.cwd / config.target).expanduser()


def _benchmark_evidence_paths_for_target(config: RLMRunConfig) -> tuple[Path, ...]:
    """Return existing benchmark evidence files that are inside the target scope."""
    target_path = _target_path(config)
    if not target_path.exists():
        return ()

    target_scope = target_path if target_path.is_dir() else target_path.parent
    paths: list[Path] = []
    seen: set[Path] = set()
    for spec in RLM_BENCHMARK_EVIDENCE_SPECS:
        candidate = (config.cwd / spec.source_path).expanduser()
        if not candidate.is_file() or not _is_within_path(candidate, target_scope):
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(candidate)
    return tuple(paths)


def _find_marker_line(lines: Sequence[str], marker: str, *, start_index: int = 0) -> int | None:
    """Return a one-based line number for marker, if found."""
    for index in range(max(0, start_index), len(lines)):
        line = lines[index]
        if ("start_marker=" in line or "end_marker=" in line) and marker in line:
            continue
        if marker in line:
            return index + 1
    return None


def _source_span_for_spec(path: Path, spec: RLMBenchmarkEvidenceSpec) -> tuple[int, int]:
    """Find a stable source span for a benchmark evidence spec."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return (1, 1)

    if not lines:
        return (1, 1)

    start_line = _find_marker_line(lines, spec.start_marker) or 1
    if spec.end_marker is None:
        end_line = min(len(lines), start_line + 40)
    else:
        end_line = _find_marker_line(lines, spec.end_marker, start_index=start_line - 1)
        if end_line is None:
            end_line = min(len(lines), start_line + 80)
    if end_line < start_line:
        end_line = start_line
    return (start_line, end_line)


def _benchmark_source_evidence_from_specs(
    config: RLMRunConfig,
) -> list[RLMBenchmarkSourceEvidence]:
    """Build grounded benchmark citations from known source files when present."""
    target_path = _target_path(config)
    if not target_path.exists():
        return []

    selected_source_paths = {
        str(chunk["source_path"])
        for chunk in _selected_target_chunks(config)
        if isinstance(chunk.get("source_path"), str)
    }
    target_scope = target_path if target_path.is_dir() else target_path.parent
    evidence: list[RLMBenchmarkSourceEvidence] = []
    for spec in RLM_BENCHMARK_EVIDENCE_SPECS:
        path = (config.cwd / spec.source_path).expanduser()
        if (
            spec.source_path not in selected_source_paths
            or not path.is_file()
            or not _is_within_path(path, target_scope)
        ):
            continue
        start_line, end_line = _source_span_for_spec(path, spec)
        evidence.append(
            RLMBenchmarkSourceEvidence(
                evidence_id=f"{spec.source_path}:{start_line}-{end_line}",
                source_path=spec.source_path,
                start_line=start_line,
                end_line=end_line,
                claim_categories=spec.claim_categories,
                claim=spec.claim,
            )
        )
    return evidence


def _fallback_source_evidence_from_chunks(
    config: RLMRunConfig,
    existing_source_paths: set[str],
) -> list[RLMBenchmarkSourceEvidence]:
    """Cite selected target chunks when known benchmark files are unavailable."""
    fallback: list[RLMBenchmarkSourceEvidence] = []
    for chunk in _selected_target_chunks(config):
        source_path = chunk.get("source_path")
        if (
            not isinstance(source_path, str)
            or not source_path
            or source_path in existing_source_paths
        ):
            continue
        start_line = chunk.get("start_line")
        end_line = chunk.get("end_line")
        if isinstance(start_line, bool) or not isinstance(start_line, int):
            start_line = 1
        if isinstance(end_line, bool) or not isinstance(end_line, int):
            end_line = start_line
        fallback.append(
            RLMBenchmarkSourceEvidence(
                evidence_id=f"{source_path}:{start_line}-{end_line}",
                source_path=source_path,
                start_line=start_line,
                end_line=max(start_line, end_line),
                claim_categories=("Target context",),
                claim="The RLM benchmark selected this source chunk as bounded target context.",
            )
        )
        existing_source_paths.add(source_path)
        if len(existing_source_paths) >= 3:
            break
    return fallback


def _render_benchmark_report(
    config: RLMRunConfig,
    source_evidence: Sequence[RLMBenchmarkSourceEvidence],
    *,
    atomic_execution: RLMAtomicExecutionResult | None = None,
    generated_rlm_tree_depth: int | None = None,
) -> str:
    """Render a compact Markdown benchmark artifact with source citations."""
    hermes_subcalls = 0
    if atomic_execution is not None:
        hermes_subcalls = (
            1
            + len(atomic_execution.chunk_subcalls)
            + len(atomic_execution.nested_benchmark_subcalls)
        )
    rendered_tree_depth = (
        str(generated_rlm_tree_depth) if generated_rlm_tree_depth is not None else "unknown"
    )

    lines = [
        "# RLM MVP Benchmark",
        "",
        "## Benchmark",
        f"- Benchmark ID: `{RLM_BENCHMARK_ID}`",
        f"- Invocation: `ooo rlm {config.target}`",
        f"- Working directory: `{config.cwd}`",
        f"- Target: `{config.target}`",
        "",
        "## Guardrails",
        f"- Ambiguity threshold: `{config.ambiguity_threshold}`",
        f"- Configured AC max depth: `{config.max_depth}`",
        f"- Generated RLM tree depth: `{rendered_tree_depth}`",
        f"- Hermes sub-calls observed: `{hermes_subcalls}`",
        "",
        "## Source Evidence",
        (
            f"The benchmark output cites `{len({item.source_path for item in source_evidence})}` "
            "distinct source file(s)."
        ),
        "",
        "| Evidence ID | Source file | Claim categories | Claim |",
        "| --- | --- | --- | --- |",
    ]
    for item in source_evidence:
        categories = ", ".join(item.claim_categories)
        lines.append(
            f"| `{item.evidence_id}` | `{item.source_path}` | {categories} | {item.claim} |"
        )
    return "\n".join(lines)


def build_rlm_benchmark_output(
    config: RLMRunConfig,
    *,
    atomic_execution: RLMAtomicExecutionResult | None = None,
    outer_scaffold_state: RLMOuterScaffoldState | None = None,
) -> RLMBenchmarkOutput | None:
    """Build the dogfood benchmark output with at least three source-file citations."""
    benchmark_fixture = _benchmark_fixture_for_config(config)
    source_evidence = _benchmark_source_evidence_from_specs(config)
    source_paths = {item.source_path for item in source_evidence}
    if len(source_paths) < 3:
        source_evidence.extend(_fallback_source_evidence_from_chunks(config, source_paths))
    if not source_evidence:
        return None

    scaffold_state = outer_scaffold_state
    if scaffold_state is None and atomic_execution is not None:
        scaffold_state = atomic_execution.outer_scaffold_state
    generated_rlm_tree_depth = (
        scaffold_state.generated_rlm_tree_depth if scaffold_state is not None else None
    )

    return RLMBenchmarkOutput(
        benchmark_id=benchmark_fixture.benchmark_id if benchmark_fixture else RLM_BENCHMARK_ID,
        schema_version=RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION,
        source_evidence=tuple(source_evidence),
        report_markdown=_render_benchmark_report(
            config,
            source_evidence,
            atomic_execution=atomic_execution,
            generated_rlm_tree_depth=generated_rlm_tree_depth,
        ),
        generated_rlm_tree_depth=generated_rlm_tree_depth,
    )


def _benchmark_fixture_for_config(config: RLMRunConfig) -> RLMBenchmarkFixture | None:
    """Return the benchmark fixture selected explicitly or by target, if any."""
    if config.benchmark_id is not None:
        return benchmark_fixture_for_id(config.benchmark_id)
    return benchmark_fixture_for_target(config.target)


def _validate_config(config: RLMRunConfig) -> None:
    """Validate RLM-specific guardrails before any execution work starts."""
    if not config.target.strip():
        msg = "RLM target must not be empty"
        raise ValueError(msg)
    if config.max_depth < 0 or config.max_depth > MAX_RLM_AC_TREE_DEPTH:
        msg = f"RLM AC tree max depth must be between 0 and {MAX_RLM_AC_TREE_DEPTH}"
        raise ValueError(msg)
    if config.ambiguity_threshold < 0 or config.ambiguity_threshold > MAX_RLM_AMBIGUITY_THRESHOLD:
        msg = f"RLM ambiguity threshold must be between 0 and {MAX_RLM_AMBIGUITY_THRESHOLD}"
        raise ValueError(msg)
    if config.benchmark_id is not None and benchmark_fixture_for_id(config.benchmark_id) is None:
        msg = f"Unknown RLM benchmark ID: {config.benchmark_id}"
        raise ValueError(msg)
    if config.chunk_line_limit <= 0:
        msg = "RLM atomic chunk line limit must be greater than 0"
        raise ValueError(msg)
    if config.max_atomic_chunks <= 0:
        msg = "RLM max atomic chunks must be greater than 0"
        raise ValueError(msg)
    if config.max_iterations <= 0:
        msg = "RLM max iterations must be greater than 0"
        raise ValueError(msg)


def _target_kind(config: RLMRunConfig) -> Literal["path", "prompt"]:
    """Classify the invocation target without requiring it to be a file path."""
    candidate = _target_path(config)
    return "path" if candidate.exists() else "prompt"


def _relative_to_cwd(path: Path, cwd: Path) -> str:
    """Return a stable display path for trace envelopes."""
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _chunk_lines(
    *,
    source_path: str,
    lines: list[str],
    config: RLMRunConfig,
    remaining_slots: int,
) -> list[dict[str, Any]]:
    """Create bounded line chunks for Hermes atomic sub-calls."""
    if remaining_slots <= 0:
        return []

    if not lines:
        return [
            {
                "chunk_id": f"{source_path}:1-1",
                "source_path": source_path,
                "start_line": 1,
                "end_line": 1,
                "content": "",
                "token_estimate": 1,
                "truncated": False,
            }
        ]

    chunks: list[dict[str, Any]] = []
    line_limit = config.chunk_line_limit
    for start_index in range(0, len(lines), line_limit):
        if len(chunks) >= remaining_slots:
            break

        selected = lines[start_index : start_index + line_limit]
        start_line = start_index + 1
        end_line = start_index + len(selected)
        content = "\n".join(selected)
        chunks.append(
            {
                "chunk_id": f"{source_path}:{start_line}-{end_line}",
                "source_path": source_path,
                "start_line": start_line,
                "end_line": end_line,
                "content": content,
                "token_estimate": max(1, len(content.split())),
                "truncated": start_index + line_limit < len(lines),
            }
        )

    return chunks


def _read_text_chunks(
    path: Path,
    config: RLMRunConfig,
    remaining_slots: int,
) -> list[dict[str, Any]]:
    """Read one text file into bounded chunks."""
    source_path = _relative_to_cwd(path, config.cwd)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    return _chunk_lines(
        source_path=source_path,
        lines=lines,
        config=config,
        remaining_slots=remaining_slots,
    )


def _selected_target_chunks(config: RLMRunConfig) -> list[dict[str, Any]]:
    """Select bounded target context for the MVP atomic Hermes call."""
    target_path = _target_path(config)
    if not target_path.exists():
        return [
            {
                "chunk_id": "prompt:target",
                "source_path": None,
                "start_line": None,
                "end_line": None,
                "content": config.target,
                "token_estimate": max(1, len(config.target.split())),
            }
        ]

    if target_path.is_file():
        return _read_text_chunks(target_path, config, config.max_atomic_chunks)

    priority_paths = _benchmark_evidence_paths_for_target(config)
    priority_resolved = {path.resolve() for path in priority_paths}
    paths = [
        *priority_paths,
        *(
            path
            for path in sorted(
                path
                for path in target_path.rglob("*")
                if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
            )
            if path.resolve() not in priority_resolved
        ),
    ]
    chunks: list[dict[str, Any]] = []
    for path in priority_paths:
        remaining_slots = config.max_atomic_chunks - len(chunks)
        if remaining_slots <= 0:
            break
        chunks.extend(_read_text_chunks(path, config, 1))

    for path in paths:
        if path.resolve() in priority_resolved:
            continue
        remaining_slots = config.max_atomic_chunks - len(chunks)
        if remaining_slots <= 0:
            break
        chunks.extend(_read_text_chunks(path, config, remaining_slots))

    if chunks:
        return chunks

    source_path = _relative_to_cwd(target_path, config.cwd)
    return [
        {
            "chunk_id": f"{source_path}:file-list",
            "source_path": source_path,
            "start_line": None,
            "end_line": None,
            "content": "",
            "token_estimate": 1,
            "truncated": False,
        }
    ]


def _build_atomic_execution_prompt(
    config: RLMRunConfig,
    *,
    mode: RLMHermesMode = RLM_HERMES_EXECUTE_ATOMIC_MODE,
    chunks: list[dict[str, Any]] | None = None,
    child_results: list[dict[str, Any]] | None = None,
    parent_node_summary: dict[str, Any] | None = None,
    synthesized_subcall_summary: dict[str, Any] | None = None,
    normalized_child_ac_inputs: list[dict[str, Any]] | None = None,
    call_id: str = "rlm_call_atomic_root",
    parent_call_id: str | None = None,
    parent_trace_id: str | None = None,
    causal_parent_event_id: str | None = None,
    subcall_id: str | None = None,
    parent_execution_state: RLMParentExecutionState | None = None,
    outer_scaffold_context: Mapping[str, Any] | None = None,
    child_order: int | None = None,
    sibling_count: int | None = None,
    rlm_node_id: str = RLM_ROOT_NODE_ID,
    ac_node_id: str = RLM_ROOT_AC_NODE_ID,
    depth: int = 0,
    generated_child_ac_node_ids: Sequence[str] = (),
    prompt_summary: str = "Initial RLM MVP atomic execution generation.",
) -> str:
    """Build the JSON envelope for an atomic or parent-synthesis Hermes sub-call."""
    call_context = RLMHermesCallContext(
        call_id=call_id,
        parent_call_id=parent_call_id,
        depth=depth,
    )
    trace_id = _trace_id_for_call(call_context.call_id)
    resolved_subcall_id = subcall_id or _new_rlm_hermes_subcall_id()
    resolved_parent_trace_id = parent_trace_id
    if resolved_parent_trace_id is None and call_context.parent_call_id is not None:
        resolved_parent_trace_id = _trace_id_for_call(call_context.parent_call_id)
    resolved_causal_parent_event_id = causal_parent_event_id
    if resolved_causal_parent_event_id is None:
        resolved_causal_parent_event_id = call_context.parent_call_id
    child_ac_node_ids = _string_tuple(generated_child_ac_node_ids)
    parent_execution_context = _build_parent_execution_context(
        mode=mode,
        call_context=call_context,
        rlm_node_id=rlm_node_id,
        ac_node_id=ac_node_id,
        trace_id=trace_id,
        parent_trace_id=resolved_parent_trace_id,
        parent_state=parent_execution_state,
        child_order=child_order,
        sibling_count=sibling_count,
    )
    target_kind = _target_kind(config)
    selected_chunks = chunks if chunks is not None else _selected_target_chunks(config)
    child_result_items = child_results or []
    normalized_child_ac_input_items = normalized_child_ac_inputs or []
    selected_chunk_ids = [str(chunk["chunk_id"]) for chunk in selected_chunks]
    initial_prompt = config.initial_prompt or f"Execute RLM target {config.target!r}"
    is_parent_synthesis = mode == RLM_HERMES_SYNTHESIZE_PARENT_MODE
    benchmark_fixture = _benchmark_fixture_for_config(config)
    benchmark_payload = benchmark_fixture.to_dict() if benchmark_fixture else None
    ac_status = "synthesizing" if is_parent_synthesis else "executing"
    objective_instruction = (
        "Synthesize the attached child RLM results into one parent-node summary "
        "and parent AC verdict. Consume every supplied child_result_id."
        if is_parent_synthesis
        else (
            "Produce the atomic execution result for this RLM MVP generation. "
            "Ground the answer in the supplied target context."
        )
    )
    if benchmark_fixture is not None:
        objective_instruction = (
            f"{objective_instruction} For benchmark {benchmark_fixture.benchmark_id}, "
            "answer the root benchmark question and every question in "
            "context.benchmark_fixture."
        )
    success_criteria = (
        [
            "Every child_result_id in parent_node_summary is consumed",
            "The parent verdict reflects completed and failed child statuses",
            "The result references supplied child evidence only",
        ]
        if is_parent_synthesis
        else [
            "Hermes returns a local atomic execution verdict",
            "The result references supplied evidence only",
        ]
    )
    ac_node_payload: dict[str, Any] = {
        "id": ac_node_id,
        "parent_id": RLM_ROOT_AC_NODE_ID if ac_node_id != RLM_ROOT_AC_NODE_ID else None,
        "depth": depth,
        "max_depth": config.max_depth,
        "title": "Execute RLM target atomically",
        "statement": (
            "Execute one bounded atomic RLM step for the initial prompt "
            f"{initial_prompt!r} using only the supplied context."
        ),
        "status": ac_status,
    }
    if child_ac_node_ids:
        ac_node_payload["child_ids"] = list(child_ac_node_ids)

    envelope: dict[str, Any] = {
        "schema_version": "rlm.hermes.input.v1",
        "mode": mode,
        "call_context": call_context.to_dict(),
        "parent_execution_context": parent_execution_context,
        "outer_scaffold": dict(outer_scaffold_context or {}),
        "run": {
            "rlm_run_id": RLM_GENERATION_ID,
            "seed_id": benchmark_fixture.benchmark_id if benchmark_fixture else "rlm-mvp",
            "fixture_id": config.fixture_id,
            "working_directory": str(config.cwd),
            "ambiguity_score": config.ambiguity_threshold,
            "ambiguity_threshold": MAX_RLM_AMBIGUITY_THRESHOLD,
        },
        "rlm_node": {
            "id": rlm_node_id,
            "parent_id": RLM_ROOT_NODE_ID if rlm_node_id != RLM_ROOT_NODE_ID else None,
            "depth": depth,
            "ancestry": [],
        },
        "ac_node": ac_node_payload,
        "objective": {
            "initial_prompt": initial_prompt,
            "instruction": objective_instruction,
            "success_criteria": success_criteria,
            "non_goals": [
                "Do not invoke ooo commands",
                "Do not mutate the AC tree",
                "Do not recurse into Ouroboros",
            ],
        },
        "constraints": {
            "max_ac_depth": MAX_RLM_AC_TREE_DEPTH,
            "must_not_call_ouroboros": True,
            "must_use_supplied_context_only": True,
            "target_kind": target_kind,
            "token_budget": {
                "max_input_tokens": 24000,
                "max_output_tokens": 4000,
            },
        },
        "context": {
            "fixture_id": config.fixture_id,
            "initial_prompt": initial_prompt,
            "prompt_summary": prompt_summary,
            "parent_execution_context": parent_execution_context,
            "parent_result": synthesized_subcall_summary,
            "parent_node_summary": parent_node_summary,
            "synthesized_subcall_summary": synthesized_subcall_summary,
            "benchmark_fixture": benchmark_payload,
            "chunks": selected_chunks,
            "summaries": [],
            "child_results": child_result_items,
            "normalized_child_ac_inputs": normalized_child_ac_input_items,
        },
        "trace": {
            "event_store_session_id": RLM_GENERATION_ID,
            "trace_id": trace_id,
            "subcall_id": resolved_subcall_id,
            "call_id": call_context.call_id,
            "parent_call_id": call_context.parent_call_id,
            "parent_trace_id": resolved_parent_trace_id,
            "causal_parent_event_id": resolved_causal_parent_event_id,
            "depth": call_context.depth,
            "selected_chunk_ids": selected_chunk_ids,
            "generated_child_ac_node_ids": list(child_ac_node_ids),
            "resume_handle_id": None,
        },
        "output_contract": {
            "format": "json",
            "required_fields": [
                "mode",
                "verdict",
                "confidence",
                "result",
                "evidence_references",
                "residual_gaps",
            ],
        },
    }
    return json.dumps(envelope, indent=2, sort_keys=True)


def _default_hermes_runtime(config: RLMRunConfig) -> AgentRuntime:
    """Build the default Hermes runtime only for the isolated RLM path."""
    from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

    return HermesCliRuntime(cwd=config.cwd)


async def _execute_hermes_atomic_subcall(
    *,
    hermes_runtime: AgentRuntime,
    mode: RLMHermesMode = RLM_HERMES_EXECUTE_ATOMIC_MODE,
    prompt: str,
    rlm_node_id: str,
    ac_node_id: str,
    parent_call_id: str | None,
    depth: int,
    call_id: str,
    chunk_id: str | None,
    trace_id: str | None = None,
    parent_trace_id: str | None = None,
    causal_parent_event_id: str | None = None,
    selected_chunk_ids: tuple[str, ...] = (),
    generated_child_ac_node_ids: tuple[str, ...] = (),
    trace_store: RLMTraceStore | None = None,
) -> RLMHermesSubcall:
    """Execute one Hermes RPC sub-call and normalize its trace payload."""
    prompt_trace = _trace_payload_from_prompt(prompt)
    resolved_trace_id = (
        trace_id or _string_or_none(prompt_trace.get("trace_id")) or _trace_id_for_call(call_id)
    )
    resolved_subcall_id = _string_or_none(prompt_trace.get("subcall_id"))
    resolved_parent_trace_id = parent_trace_id or _string_or_none(
        prompt_trace.get("parent_trace_id")
    )
    resolved_causal_parent_event_id = causal_parent_event_id or _string_or_none(
        prompt_trace.get("causal_parent_event_id")
    )
    resolved_generated_child_ac_node_ids = generated_child_ac_node_ids or _string_tuple(
        prompt_trace.get("generated_child_ac_node_ids")
    )
    resolved_selected_chunk_ids = selected_chunk_ids
    if not resolved_selected_chunk_ids and chunk_id is not None:
        resolved_selected_chunk_ids = (chunk_id,)
    if not resolved_selected_chunk_ids:
        resolved_selected_chunk_ids = _selected_chunk_ids_from_prompt(prompt)
    started_at = perf_counter()
    system_prompt_hash = hash_trace_text(HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT)
    await _append_hermes_call_started_trace(
        trace_store,
        RLMHermesTraceRecord(
            prompt=prompt,
            completion="",
            parent_call_id=parent_call_id,
            depth=depth,
            trace_id=resolved_trace_id,
            subcall_id=resolved_subcall_id,
            parent_trace_id=resolved_parent_trace_id,
            causal_parent_event_id=resolved_causal_parent_event_id,
            call_id=call_id,
            mode=mode,
            generation_id=RLM_GENERATION_ID,
            rlm_node_id=rlm_node_id,
            ac_node_id=ac_node_id,
            selected_chunk_ids=resolved_selected_chunk_ids,
            generated_child_ac_node_ids=resolved_generated_child_ac_node_ids,
            prompt_hash=hash_trace_text(prompt),
            response_hash=None,
            success=None,
            exit_code=None,
            elapsed_ms=None,
            system_prompt_hash=system_prompt_hash,
        ),
    )
    hermes_result = await hermes_runtime.execute_task_to_result(
        prompt=prompt,
        tools=[],
        system_prompt=HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT,
    )
    elapsed_ms = int((perf_counter() - started_at) * 1000)

    if hermes_result.is_err:
        error = hermes_result.error
        completion = error.message if isinstance(error, ProviderError) else str(error)
        provider = error.provider if isinstance(error, ProviderError) else None
        adapter_error: dict[str, Any] = {
            "provider": provider or "hermes",
            "message": completion,
        }
        if isinstance(error, ProviderError) and error.details:
            adapter_error["details"] = dict(error.details)
        failed_subcall = RLMHermesSubcall(
            mode=mode,
            generation_id=RLM_GENERATION_ID,
            rlm_node_id=rlm_node_id,
            ac_node_id=ac_node_id,
            prompt=prompt,
            completion=completion,
            parent_call_id=parent_call_id,
            depth=depth,
            exit_code=1,
            call_id=call_id,
            subcall_id=resolved_subcall_id,
            trace_id=resolved_trace_id,
            parent_trace_id=resolved_parent_trace_id,
            causal_parent_event_id=resolved_causal_parent_event_id,
            chunk_id=chunk_id,
            selected_chunk_ids=resolved_selected_chunk_ids,
            generated_child_ac_node_ids=resolved_generated_child_ac_node_ids,
            success=False,
            elapsed_ms=elapsed_ms,
            adapter_error=adapter_error,
            system_prompt_hash=system_prompt_hash,
        )
        await _append_hermes_subcall_trace(
            trace_store,
            failed_subcall,
            event_type=RLM_HERMES_CALL_FAILED_EVENT,
        )
        raise ValueError(f"Hermes atomic execution sub-call failed: {completion}") from None

    task_result = hermes_result.value
    subcall = RLMHermesSubcall(
        mode=mode,
        generation_id=RLM_GENERATION_ID,
        rlm_node_id=rlm_node_id,
        ac_node_id=ac_node_id,
        prompt=prompt,
        completion=task_result.final_message,
        parent_call_id=parent_call_id,
        depth=depth,
        exit_code=0 if task_result.success else 1,
        call_id=call_id,
        subcall_id=resolved_subcall_id or _subcall_id_from_task_result(task_result),
        trace_id=resolved_trace_id,
        parent_trace_id=resolved_parent_trace_id,
        causal_parent_event_id=resolved_causal_parent_event_id,
        chunk_id=chunk_id,
        selected_chunk_ids=resolved_selected_chunk_ids,
        generated_child_ac_node_ids=resolved_generated_child_ac_node_ids,
        resume_handle=task_result.resume_handle,
        success=task_result.success,
        elapsed_ms=elapsed_ms,
        system_prompt_hash=system_prompt_hash,
    )
    await _append_hermes_subcall_trace(
        trace_store,
        subcall,
        event_type=RLM_HERMES_CALL_SUCCEEDED_EVENT
        if task_result.success
        else RLM_HERMES_CALL_FAILED_EVENT,
    )
    return subcall


async def _append_hermes_call_started_trace(
    trace_store: RLMTraceStore | None,
    record: RLMHermesTraceRecord,
) -> None:
    """Persist one Hermes sub-call start trace record when tracing is enabled."""
    if trace_store is None:
        return

    await trace_store.append_hermes_call_started(
        record,
        aggregate_id=record.generation_id,
    )


async def _append_hermes_subcall_trace(
    trace_store: RLMTraceStore | None,
    subcall: RLMHermesSubcall,
    *,
    event_type: str,
) -> None:
    """Persist one terminal Hermes sub-call trace record when tracing is enabled."""
    if trace_store is None:
        return

    await trace_store.append_hermes_subcall(
        subcall.to_trace_record(),
        event_type=event_type,
        aggregate_id=subcall.generation_id,
    )


async def _execute_child_hermes_atomic_subcall(
    *,
    parent_state: RLMParentExecutionState,
    order: int,
    hermes_runtime: AgentRuntime,
    mode: RLMHermesMode = RLM_HERMES_EXECUTE_ATOMIC_MODE,
    prompt: str,
    rlm_node_id: str,
    ac_node_id: str,
    parent_call_id: str,
    depth: int,
    call_id: str,
    chunk_id: str | None,
    selected_chunk_ids: tuple[str, ...] = (),
    trace_store: RLMTraceStore | None = None,
) -> tuple[RLMHermesSubcall, RLMParentExecutionState]:
    """Execute one child sub-call and return with its result captured."""
    subcall = await _execute_hermes_atomic_subcall(
        hermes_runtime=hermes_runtime,
        mode=mode,
        prompt=prompt,
        rlm_node_id=rlm_node_id,
        ac_node_id=ac_node_id,
        parent_call_id=parent_call_id,
        depth=depth,
        call_id=call_id,
        chunk_id=chunk_id,
        selected_chunk_ids=selected_chunk_ids,
        trace_store=trace_store,
    )
    return (
        subcall,
        capture_completed_hermes_subcall_result(
            parent_state,
            order=order,
            subcall=subcall,
        ),
    )


def _benchmark_nested_inner_lm_call_budget(config: RLMRunConfig) -> int:
    """Return benchmark-only nested call count required for depth validation."""
    if config.benchmark_id is None:
        return 0
    fixture = _benchmark_fixture_for_config(config)
    if fixture is None:
        return 0
    return max(0, fixture.execution_config.min_nested_inner_lm_calls)


def _minimum_benchmark_rlm_tree_depth(fixture: RLMBenchmarkFixture) -> int:
    """Return the minimum recorded RLM tree depth required by a benchmark fixture."""
    nested_call_count = max(0, fixture.execution_config.min_nested_inner_lm_calls)
    return 1 + nested_call_count if nested_call_count else 0


def _recorded_rlm_tree_depth(result: RLMRunResult) -> int | None:
    """Read the recorded RLM tree depth from benchmark output or scaffold state."""
    if result.benchmark_output is not None:
        return result.benchmark_output.generated_rlm_tree_depth
    if result.outer_scaffold_state is not None:
        return result.outer_scaffold_state.generated_rlm_tree_depth
    return None


def _validate_rlm_benchmark_result(
    result: RLMRunResult,
    fixture: RLMBenchmarkFixture,
) -> None:
    """Validate benchmark execution facts that must be proven by the recorded trace."""
    minimum_depth = _minimum_benchmark_rlm_tree_depth(fixture)
    if minimum_depth <= 0:
        return

    recorded_depth = _recorded_rlm_tree_depth(result)
    if recorded_depth is None or recorded_depth < minimum_depth:
        msg = (
            f"RLM benchmark {fixture.benchmark_id} recorded RLM tree depth must be "
            f">= {minimum_depth}; observed {recorded_depth if recorded_depth is not None else 'unknown'}"
        )
        raise ValueError(msg)


@dataclass(slots=True)
class RLMOuterScaffoldLoop:
    """Executable Ouroboros-owned recursive loop for atomic RLM execution."""

    config: RLMRunConfig
    scaffold_state: RLMOuterScaffoldState
    hermes_runtime: AgentRuntime
    chunks: list[dict[str, Any]]
    chunk_subcalls: list[RLMHermesSubcall] = field(default_factory=list, init=False)
    nested_benchmark_subcalls: list[RLMHermesSubcall] = field(
        default_factory=list,
        init=False,
    )
    parent_execution_state: RLMParentExecutionState | None = field(default=None, init=False)
    parent_call_context: RLMHermesCallContext | None = field(default=None, init=False)
    nested_benchmark_calls_remaining: int = field(default=0, init=False)
    _chunks_by_id: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.chunks = list(self.chunks)
        self._chunks_by_id = {str(chunk["chunk_id"]): chunk for chunk in self.chunks}

    async def run(self) -> RLMAtomicExecutionResult:
        """Run scaffold iterations until root convergence or a guardrail stops progress."""
        self._ensure_guardrails_completed()
        root_node = self.scaffold_state.select_node(RLM_ROOT_NODE_ID)
        self.scaffold_state.bind_context(
            root_node.rlm_node_id,
            selected_chunk_ids=self._all_chunk_ids(),
        )
        self.nested_benchmark_calls_remaining = _benchmark_nested_inner_lm_call_budget(
            self.config
        )

        if self._should_execute_root_atomically(root_node):
            return await self._execute_root_atomic()

        self._schedule_chunk_recursion()
        await self._drain_atomic_work_queue()
        return await self._synthesize_parent()

    def _ensure_guardrails_completed(self) -> None:
        if self.scaffold_state.run_state == RLMRunLifecycleState.INITIALIZED:
            self.scaffold_state.enter_guarding()
            self.scaffold_state.complete_guarding()

    def _all_chunk_ids(self) -> tuple[str, ...]:
        return tuple(str(chunk["chunk_id"]) for chunk in self.chunks)

    def _should_execute_root_atomically(self, root_node: RLMScaffoldNode) -> bool:
        force_chunk_recursion = self.nested_benchmark_calls_remaining > 0
        return (
            len(self.chunks) == 1
            and not force_chunk_recursion
            or root_node.depth + 1 > self.scaffold_state.max_depth
        )

    async def _execute_root_atomic(self) -> RLMAtomicExecutionResult:
        prompt = _build_atomic_execution_prompt(
            self.config,
            chunks=self.chunks,
            call_id="rlm_call_atomic_root",
            sibling_count=len(self.chunks),
            outer_scaffold_context=self.scaffold_state.prompt_context_for_node(
                RLM_ROOT_NODE_ID
            ),
        )
        self.scaffold_state.mark_awaiting_hermes(RLM_ROOT_NODE_ID)
        subcall = await _execute_hermes_atomic_subcall(
            hermes_runtime=self.hermes_runtime,
            prompt=prompt,
            rlm_node_id=RLM_ROOT_NODE_ID,
            ac_node_id=RLM_ROOT_AC_NODE_ID,
            parent_call_id=None,
            depth=0,
            call_id="rlm_call_atomic_root",
            chunk_id=str(self.chunks[0]["chunk_id"]),
            selected_chunk_ids=self._all_chunk_ids(),
            trace_store=self.config.trace_store,
        )
        self.scaffold_state.begin_response_validation(RLM_ROOT_NODE_ID)
        terminal_state = (
            RLMNodeLifecycleState.ATOMIC_COMPLETE
            if subcall.exit_code == 0
            else RLMNodeLifecycleState.FAILED
        )
        self.scaffold_state.complete_node(
            RLM_ROOT_NODE_ID,
            terminal_state,
            reason=RLMTerminationReason.ROOT_ATOMIC_COMPLETED
            if subcall.exit_code == 0
            else RLMTerminationReason.NODE_FAILED,
            finish_run=True,
        )
        return RLMAtomicExecutionResult(
            ac_node_id=RLM_ROOT_AC_NODE_ID,
            generation_id=RLM_GENERATION_ID,
            hermes_subcall=subcall,
            success=subcall.exit_code == 0,
            final_message=subcall.completion,
            outer_scaffold_state=self.scaffold_state,
        )

    def _schedule_chunk_recursion(self) -> None:
        self.parent_call_context = RLMHermesCallContext(call_id="rlm_call_atomic_synthesis")
        self.parent_execution_state = RLMParentExecutionState(
            parent_node_id=RLM_ROOT_NODE_ID,
            parent_ac_node_id=RLM_ROOT_AC_NODE_ID,
            generation_id=RLM_GENERATION_ID,
        )
        self.scaffold_state.schedule_atomic_chunk_children(
            parent_node_id=RLM_ROOT_NODE_ID,
            chunks=self.chunks,
            parent_call_id=self.parent_call_context.call_id,
        )

    async def _drain_atomic_work_queue(self) -> None:
        while self.scaffold_state.work_queue and not self.scaffold_state.is_terminal:
            rlm_node_id = self.scaffold_state.work_queue[0]
            node = self.scaffold_state.nodes[rlm_node_id]
            if node.parent_node_id != RLM_ROOT_NODE_ID:
                msg = f"Unsupported RLM work queue node: {rlm_node_id}"
                raise ValueError(msg)
            await self._execute_chunk_node(rlm_node_id)

    async def _execute_chunk_node(self, rlm_node_id: str) -> None:
        if self.parent_call_context is None or self.parent_execution_state is None:
            msg = "RLM parent synthesis context has not been initialized"
            raise ValueError(msg)

        node = self.scaffold_state.select_node(rlm_node_id)
        chunk = self._chunk_for_node(node)
        index = _node_order_from_suffix(rlm_node_id)
        call_id = f"rlm_call_atomic_chunk_{index:03d}"
        child_call_context = self.parent_call_context.child(call_id)

        self.scaffold_state.bind_context(
            rlm_node_id,
            selected_chunk_ids=(str(chunk["chunk_id"]),),
        )
        chunk_prompt = _build_atomic_execution_prompt(
            self.config,
            chunks=[chunk],
            call_id=child_call_context.call_id,
            parent_call_id=child_call_context.parent_call_id,
            rlm_node_id=rlm_node_id,
            ac_node_id=node.ac_node_id,
            depth=child_call_context.depth,
            parent_execution_state=self.parent_execution_state,
            outer_scaffold_context=self.scaffold_state.prompt_context_for_node(rlm_node_id),
            child_order=index - 1,
            sibling_count=len(self.chunks),
            prompt_summary=(
                "Chunk-level RLM atomic execution. Process this bounded chunk "
                "and return evidence-grounded partial results for parent synthesis."
            ),
        )
        self.scaffold_state.mark_awaiting_hermes(rlm_node_id)
        chunk_subcall, self.parent_execution_state = await _execute_child_hermes_atomic_subcall(
            parent_state=self.parent_execution_state,
            order=index - 1,
            hermes_runtime=self.hermes_runtime,
            prompt=chunk_prompt,
            rlm_node_id=rlm_node_id,
            ac_node_id=node.ac_node_id,
            parent_call_id=child_call_context.parent_call_id,
            depth=child_call_context.depth,
            call_id=child_call_context.call_id,
            chunk_id=str(chunk["chunk_id"]),
            trace_store=self.config.trace_store,
        )
        self.scaffold_state.begin_response_validation(rlm_node_id)

        if self.nested_benchmark_calls_remaining > 0:
            await self._execute_nested_benchmark_validation(
                parent_node_id=rlm_node_id,
                parent_call_context=child_call_context,
                parent_subcall=chunk_subcall,
                chunk=chunk,
            )

        self.scaffold_state.complete_node(
            rlm_node_id,
            RLMNodeLifecycleState.ATOMIC_COMPLETE
            if chunk_subcall.exit_code == 0
            else RLMNodeLifecycleState.FAILED,
            reason=RLMTerminationReason.ROOT_ATOMIC_COMPLETED
            if chunk_subcall.exit_code == 0
            else RLMTerminationReason.NODE_FAILED,
        )
        self.chunk_subcalls.append(chunk_subcall)

    async def _execute_nested_benchmark_validation(
        self,
        *,
        parent_node_id: str,
        parent_call_context: RLMHermesCallContext,
        parent_subcall: RLMHermesSubcall,
        chunk: Mapping[str, Any],
    ) -> None:
        nested_order = len(self.nested_benchmark_subcalls) + 1
        scheduled_nested = self.scaffold_state.schedule_benchmark_validation_child(
            parent_node_id=parent_node_id,
            parent_call_id=parent_subcall.call_id,
            chunk_id=str(chunk["chunk_id"]),
            benchmark_id=self.config.benchmark_id or RLM_BENCHMARK_ID,
            order=nested_order,
        )
        if scheduled_nested is None:
            return

        nested_rlm_node_id, nested_ac_node_id = scheduled_nested
        nested_call_context = parent_call_context.child(
            f"rlm_call_benchmark_validation_{nested_order:03d}"
        )
        self.scaffold_state.select_node(nested_rlm_node_id)
        self.scaffold_state.bind_context(
            nested_rlm_node_id,
            selected_chunk_ids=(str(chunk["chunk_id"]),),
        )
        nested_prompt = _build_atomic_execution_prompt(
            self.config,
            chunks=[dict(chunk)],
            call_id=nested_call_context.call_id,
            parent_call_id=nested_call_context.parent_call_id,
            rlm_node_id=nested_rlm_node_id,
            ac_node_id=nested_ac_node_id,
            depth=nested_call_context.depth,
            outer_scaffold_context=self.scaffold_state.prompt_context_for_node(
                nested_rlm_node_id
            ),
            child_order=0,
            sibling_count=1,
            prompt_summary=(
                "Nested RLM benchmark validation. Confirm this bounded child "
                "result participates in a recorded recursive RLM tree."
            ),
        )
        self.scaffold_state.mark_awaiting_hermes(nested_rlm_node_id)
        nested_subcall = await _execute_hermes_atomic_subcall(
            hermes_runtime=self.hermes_runtime,
            prompt=nested_prompt,
            rlm_node_id=nested_rlm_node_id,
            ac_node_id=nested_ac_node_id,
            parent_call_id=nested_call_context.parent_call_id,
            depth=nested_call_context.depth,
            call_id=nested_call_context.call_id,
            chunk_id=str(chunk["chunk_id"]),
            selected_chunk_ids=(str(chunk["chunk_id"]),),
            trace_store=self.config.trace_store,
        )
        self.scaffold_state.begin_response_validation(nested_rlm_node_id)
        self.scaffold_state.complete_node(
            nested_rlm_node_id,
            RLMNodeLifecycleState.ATOMIC_COMPLETE
            if nested_subcall.exit_code == 0
            else RLMNodeLifecycleState.FAILED,
            reason=RLMTerminationReason.ROOT_ATOMIC_COMPLETED
            if nested_subcall.exit_code == 0
            else RLMTerminationReason.NODE_FAILED,
        )
        self.nested_benchmark_subcalls.append(nested_subcall)
        self.nested_benchmark_calls_remaining -= 1

    async def _synthesize_parent(self) -> RLMAtomicExecutionResult:
        if self.parent_call_context is None or self.parent_execution_state is None:
            msg = "RLM parent synthesis context has not been initialized"
            raise ValueError(msg)

        parent_node_summary = synthesize_parent_node_summary(self.parent_execution_state)
        self.parent_execution_state = self.parent_execution_state.with_synthesized_summary(
            parent_node_summary
        )
        generated_child_ac_node_ids = tuple(
            subcall.ac_node_id for subcall in self.chunk_subcalls
        )
        synthesized_subcall_summary = (
            self.parent_execution_state.to_synthesized_subcall_summary_context()
        )
        self.scaffold_state.prepare_parent_synthesis(RLM_ROOT_NODE_ID)
        self.scaffold_state.bind_context(
            RLM_ROOT_NODE_ID,
            selected_chunk_ids=self._all_chunk_ids(),
        )
        synthesis_prompt = _build_atomic_execution_prompt(
            self.config,
            mode=RLM_HERMES_SYNTHESIZE_PARENT_MODE,
            chunks=self.chunks,
            child_results=self.parent_execution_state.to_child_results_context(),
            parent_node_summary=self.parent_execution_state.to_parent_node_summary_context(),
            synthesized_subcall_summary=synthesized_subcall_summary,
            normalized_child_ac_inputs=self.parent_execution_state.to_child_ac_input_context(),
            call_id=self.parent_call_context.call_id,
            rlm_node_id=RLM_ROOT_NODE_ID,
            ac_node_id=RLM_ROOT_AC_NODE_ID,
            depth=self.parent_call_context.depth,
            parent_execution_state=self.parent_execution_state,
            outer_scaffold_context=self.scaffold_state.prompt_context_for_node(
                RLM_ROOT_NODE_ID
            ),
            sibling_count=len(self.chunks),
            generated_child_ac_node_ids=generated_child_ac_node_ids,
            prompt_summary=(
                "Parent RLM atomic synthesis. Combine chunk-level Hermes results "
                "into one atomic AC execution verdict."
            ),
        )
        self.scaffold_state.mark_awaiting_hermes(RLM_ROOT_NODE_ID)
        synthesis_subcall = await _execute_hermes_atomic_subcall(
            hermes_runtime=self.hermes_runtime,
            mode=RLM_HERMES_SYNTHESIZE_PARENT_MODE,
            prompt=synthesis_prompt,
            rlm_node_id=RLM_ROOT_NODE_ID,
            ac_node_id=RLM_ROOT_AC_NODE_ID,
            parent_call_id=None,
            depth=self.parent_call_context.depth,
            call_id=self.parent_call_context.call_id,
            chunk_id=None,
            selected_chunk_ids=self._all_chunk_ids(),
            generated_child_ac_node_ids=generated_child_ac_node_ids,
            trace_store=self.config.trace_store,
        )
        self.scaffold_state.begin_response_validation(RLM_ROOT_NODE_ID)
        synthesis_success = (
            synthesis_subcall.exit_code == 0
            and all(subcall.exit_code == 0 for subcall in self.chunk_subcalls)
            and all(subcall.exit_code == 0 for subcall in self.nested_benchmark_subcalls)
        )
        self.scaffold_state.complete_node(
            RLM_ROOT_NODE_ID,
            RLMNodeLifecycleState.SYNTHESIS_COMPLETE
            if synthesis_success
            else RLMNodeLifecycleState.FAILED,
            reason=RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED
            if synthesis_success
            else RLMTerminationReason.NODE_FAILED,
            finish_run=True,
        )
        return RLMAtomicExecutionResult(
            ac_node_id=RLM_ROOT_AC_NODE_ID,
            generation_id=RLM_GENERATION_ID,
            hermes_subcall=synthesis_subcall,
            success=synthesis_success,
            final_message=synthesis_subcall.completion,
            chunk_subcalls=tuple(self.chunk_subcalls),
            nested_benchmark_subcalls=tuple(self.nested_benchmark_subcalls),
            parent_execution_state=self.parent_execution_state,
            outer_scaffold_state=self.scaffold_state,
        )

    def _chunk_for_node(self, node: RLMScaffoldNode) -> dict[str, Any]:
        chunk_id = node.selected_chunk_ids[0] if node.selected_chunk_ids else None
        if chunk_id is None or chunk_id not in self._chunks_by_id:
            msg = f"RLM node {node.rlm_node_id} has no selected chunk"
            raise ValueError(msg)
        return self._chunks_by_id[chunk_id]


def _node_order_from_suffix(rlm_node_id: str) -> int:
    """Parse the stable numeric suffix used by scaffold-generated child nodes."""
    try:
        return int(rlm_node_id.rsplit("_", 1)[1])
    except (IndexError, ValueError) as exc:
        msg = f"RLM node ID does not end in a numeric order: {rlm_node_id}"
        raise ValueError(msg) from exc


async def execute_atomic_ac_with_hermes(
    config: RLMRunConfig,
    *,
    scaffold_state: RLMOuterScaffoldState | None = None,
) -> RLMAtomicExecutionResult:
    """Execute the RLM root atomic AC through Hermes RPC sub-calls."""
    hermes_runtime = config.hermes_runtime or _default_hermes_runtime(config)
    chunks = _selected_target_chunks(config)
    scaffold = scaffold_state or RLMOuterScaffoldState.initialize(config)
    return await RLMOuterScaffoldLoop(
        config=config,
        scaffold_state=scaffold,
        hermes_runtime=hermes_runtime,
        chunks=chunks,
    ).run()


async def run_rlm_loop(config: RLMRunConfig) -> RLMRunResult:
    """Run the isolated RLM MVP entry path.

    The MVP executes one outer-owned generation. During non-dry-run execution,
    the atomic AC path calls Hermes through the existing runtime adapter, using
    chunk-level sub-calls when the selected context spans multiple chunks.
    """
    _validate_config(config)
    scaffold_state = RLMOuterScaffoldState.initialize(config)
    scaffold_state.enter_guarding()
    scaffold_state.complete_guarding()
    await asyncio.sleep(0)

    if config.dry_run:
        scaffold_state.mark_dry_run_ready()
        benchmark_output = build_rlm_benchmark_output(
            config,
            outer_scaffold_state=scaffold_state,
        )
        return RLMRunResult(
            status="ready",
            target=config.target,
            target_kind=_target_kind(config),
            cwd=config.cwd,
            max_depth=config.max_depth,
            ambiguity_threshold=config.ambiguity_threshold,
            message="RLM command path ready; run/evolve command paths were not invoked.",
            benchmark_output=benchmark_output,
            outer_scaffold_state=scaffold_state,
            termination_reason=scaffold_state.termination_reason,
        )

    atomic_execution = await execute_atomic_ac_with_hermes(
        config,
        scaffold_state=scaffold_state,
    )
    hermes_subcall_count = (
        1 + len(atomic_execution.chunk_subcalls) + len(atomic_execution.nested_benchmark_subcalls)
    )
    benchmark_output = build_rlm_benchmark_output(
        config,
        atomic_execution=atomic_execution,
    )
    return RLMRunResult(
        status="completed",
        target=config.target,
        target_kind=_target_kind(config),
        cwd=config.cwd,
        max_depth=config.max_depth,
        ambiguity_threshold=config.ambiguity_threshold,
        message=(
            "RLM command path completed with "
            f"{hermes_subcall_count} Hermes atomic execution sub-call(s); "
            "run/evolve command paths were not invoked."
        ),
        hermes_subcall_count=hermes_subcall_count,
        atomic_execution=atomic_execution,
        benchmark_output=benchmark_output,
        outer_scaffold_state=atomic_execution.outer_scaffold_state,
        termination_reason=(
            atomic_execution.outer_scaffold_state.termination_reason
            if atomic_execution.outer_scaffold_state is not None
            else None
        ),
    )


async def run_rlm_benchmark(
    config: RLMRunConfig,
    *,
    benchmark_id: str = RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
) -> RLMRunResult:
    """Run a built-in RLM benchmark through the recursive loop entry point."""
    benchmark_fixture = benchmark_fixture_for_id(benchmark_id)
    if benchmark_fixture is None:
        msg = f"Unknown RLM benchmark ID: {benchmark_id}"
        raise ValueError(msg)

    benchmark_config = replace(
        config,
        target=benchmark_fixture.target,
        benchmark_id=benchmark_fixture.benchmark_id,
        chunk_line_limit=benchmark_fixture.execution_config.chunk_line_limit,
        max_atomic_chunks=benchmark_fixture.execution_config.max_atomic_chunks,
    )
    result = await run_rlm_loop(benchmark_config)
    if not benchmark_config.dry_run:
        _validate_rlm_benchmark_result(result, benchmark_fixture)
    return result
