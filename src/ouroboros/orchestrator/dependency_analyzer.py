"""Hybrid AC dependency analysis and staged execution planning."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import re
from typing import TYPE_CHECKING, Any

from ouroboros.config import get_dependency_analysis_model
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter

log = get_logger(__name__)

_REFERENCE_PATTERN = re.compile(r"^(?:ac|criterion)?\s*#?\s*(\d+)$", re.IGNORECASE)
_SERIAL_METADATA_KEYS = (
    "serial",
    "serialize",
    "serialized",
    "parallel_safe",
    "parallelizable",
    "requires_serial_execution",
    "serial_only",
    "exclusive_runtime",
    "exclusive_workspace",
)
_DEPENDENCY_METADATA_KEYS = (
    "depends_on",
    "dependencies",
    "blocked_by",
    "after",
    "requires",
    "prerequisites",
)
_RESOURCE_METADATA_KEYS = (
    "shared_runtime_resources",
    "runtime_resources",
    "resources",
)
_CONTEXT_METADATA_KEYS = (
    "context",
    "dependency_context",
    "execution_context",
)
_PROVIDER_METADATA_KEYS = (
    "provides",
    "provides_prerequisites",
    "satisfies",
    "fulfills",
    "outputs",
    "produces",
)
_SHARED_PREREQUISITE_KEYS = (
    "shared_prerequisites",
    "required_prerequisites",
)
_REFERENCE_DICT_KEYS = (
    "reference",
    "ref",
    "id",
    "key",
    "ac",
    "ac_id",
    "name",
)


@dataclass(frozen=True, slots=True)
class ACNode:
    """Represents an AC in the dependency graph."""

    index: int
    content: str
    depends_on: tuple[int, ...] = field(default_factory=tuple)
    can_run_independently: bool = True
    requires_serial_stage: bool = False
    serialization_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ACSharedRuntimeResource:
    """A runtime resource claim that can constrain parallelism."""

    name: str
    access_mode: str = "write"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip().lower())
        object.__setattr__(self, "access_mode", _normalize_resource_access_mode(self.access_mode))


@dataclass(frozen=True, slots=True)
class ACDependencySpec:
    """Structured AC input for dependency analysis."""

    index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    prerequisites: tuple[str | int, ...] = field(default_factory=tuple)
    shared_runtime_resources: tuple[ACSharedRuntimeResource, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str | None:
        """Return an optional stable identifier from metadata."""
        for candidate in _iter_identity_candidates(self.metadata, self.context):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip().lower()
        return None


@dataclass(frozen=True, slots=True)
class ExecutionStage:
    """A serial stage containing ACs that may execute concurrently."""

    index: int
    ac_indices: tuple[int, ...] = field(default_factory=tuple)
    depends_on_stages: tuple[int, ...] = field(default_factory=tuple)

    @property
    def stage_number(self) -> int:
        """Return 1-based stage number for display."""
        return self.index + 1

    @property
    def is_parallel(self) -> bool:
        """True when the stage contains multiple ACs."""
        return len(self.ac_indices) > 1


@dataclass(frozen=True, slots=True)
class StagedExecutionPlan:
    """Normalized execution plan consumed by the runtime executor."""

    nodes: tuple[ACNode, ...]
    stages: tuple[ExecutionStage, ...] = field(default_factory=tuple)

    @property
    def total_stages(self) -> int:
        """Number of serial stages in the plan."""
        return len(self.stages)

    @property
    def is_parallelizable(self) -> bool:
        """True if any stage contains concurrent AC work."""
        return any(stage.is_parallel for stage in self.stages)

    @property
    def execution_levels(self) -> tuple[tuple[int, ...], ...]:
        """Legacy level view for callers that still expect grouped indices."""
        return tuple(stage.ac_indices for stage in self.stages)

    def get_dependencies(self, index: int) -> tuple[int, ...]:
        """Get dependencies for a specific AC."""
        for node in self.nodes:
            if node.index == index:
                return node.depends_on
        return ()

    def get_stage_for_ac(self, index: int) -> ExecutionStage | None:
        """Return the stage containing the given AC index."""
        for stage in self.stages:
            if index in stage.ac_indices:
                return stage
        return None


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Dependency graph for AC execution."""

    nodes: tuple[ACNode, ...]
    execution_levels: tuple[tuple[int, ...], ...] = field(default_factory=tuple)

    @property
    def total_levels(self) -> int:
        """Number of sequential execution levels."""
        return len(self.execution_levels)

    @property
    def is_parallelizable(self) -> bool:
        """True if any level has multiple ACs."""
        return any(len(level) > 1 for level in self.execution_levels)

    @property
    def independent_indices(self) -> tuple[int, ...]:
        """Return AC indices safe to run in shared parallel stages."""
        return tuple(node.index for node in self.nodes if node.can_run_independently)

    @property
    def serialized_indices(self) -> tuple[int, ...]:
        """Return AC indices that should remain serialized."""
        return tuple(node.index for node in self.nodes if not node.can_run_independently)

    def get_dependencies(self, index: int) -> tuple[int, ...]:
        """Get dependencies for a specific AC."""
        for node in self.nodes:
            if node.index == index:
                return node.depends_on
        return ()

    def get_node(self, index: int) -> ACNode | None:
        """Get the node definition for a specific AC."""
        for node in self.nodes:
            if node.index == index:
                return node
        return None

    def to_execution_plan(self) -> StagedExecutionPlan:
        """Normalize dependency levels into a staged execution plan."""
        return HybridExecutionPlanner().create_plan(self)

    def to_runtime_execution_plan(self) -> StagedExecutionPlan:
        """Build the runtime-oriented staged execution plan for this graph."""
        return HybridExecutionPlanner().build_runtime_plan(self)


class DependencyAnalysisError(Exception):
    """Error during dependency analysis."""


class ExecutionPlanningError(Exception):
    """Raised when dependency analysis cannot produce safe execution stages."""


DEPENDENCY_ANALYSIS_PROMPT = """Analyze the following acceptance criteria and determine their dependencies.

Acceptance Criteria:
{ac_list}

Instructions:
1. For each AC, identify which OTHER ACs it depends on (if any)
2. An AC depends on another if:
   - It requires files/code created by the other AC
   - It needs functionality implemented by the other AC
   - It builds upon or extends the other AC's work
3. If ACs are independent (can be done in any order), they have no dependencies

Return ONLY a valid JSON object in this exact format:
{{
  "dependencies": [
    {{"ac_index": 0, "depends_on": []}},
    {{"ac_index": 1, "depends_on": [0]}},
    {{"ac_index": 2, "depends_on": []}}
  ]
}}

Rules:
- Use 0-based indexing (AC 0, AC 1, etc.)
- If an AC has no dependencies, use empty array []
- Return ONLY valid JSON, no explanations or markdown
- Every AC must appear in the dependencies array
"""


class HybridExecutionPlanner:
    """Build a serial-stage execution plan from dependency analysis output."""

    def build_runtime_plan(self, dependency_graph: DependencyGraph) -> StagedExecutionPlan:
        """Convert dependency analysis output into staged runtime execution batches."""
        return self.create_plan(dependency_graph)

    def create_plan(self, dependency_graph: DependencyGraph) -> StagedExecutionPlan:
        """Convert a dependency graph into validated serial stages."""
        node_map = {node.index: node for node in dependency_graph.nodes}
        if len(node_map) != len(dependency_graph.nodes):
            msg = "Dependency graph contains duplicate AC node indices"
            raise ExecutionPlanningError(msg)

        execution_levels = dependency_graph.execution_levels
        if not execution_levels and dependency_graph.nodes:
            execution_levels = _apply_serial_only_constraints(
                _compute_execution_levels(dependency_graph.nodes),
                dependency_graph.nodes,
            )

        normalized_levels = tuple(tuple(sorted(level)) for level in execution_levels if level)
        ac_to_stage: dict[int, int] = {}
        for stage_index, level in enumerate(normalized_levels):
            for ac_index in level:
                if ac_index in ac_to_stage:
                    msg = f"AC {ac_index} appears in multiple execution stages"
                    raise ExecutionPlanningError(msg)
                ac_to_stage[ac_index] = stage_index

        if node_map:
            expected = set(node_map)
            planned = set(ac_to_stage)
            if expected != planned:
                missing = sorted(expected - planned)
                extra = sorted(planned - expected)
                details: list[str] = []
                if missing:
                    details.append(f"missing={missing}")
                if extra:
                    details.append(f"extra={extra}")
                msg = "Execution stages do not match dependency graph nodes: " + ", ".join(details)
                raise ExecutionPlanningError(msg)

        stages: list[ExecutionStage] = []
        for stage_index, level in enumerate(normalized_levels):
            depends_on_stages: set[int] = set()
            for ac_index in level:
                node = node_map.get(ac_index)
                if node is None:
                    continue
                if node.requires_serial_stage and len(level) > 1:
                    msg = f"Serialized AC {ac_index} cannot share stage {stage_index + 1}"
                    raise ExecutionPlanningError(msg)
                for dependency in node.depends_on:
                    dependency_stage = ac_to_stage.get(dependency)
                    if dependency_stage is None:
                        msg = f"AC {ac_index} depends on missing AC {dependency}"
                        raise ExecutionPlanningError(msg)
                    if dependency_stage >= stage_index:
                        msg = (
                            f"AC {ac_index} depends on AC {dependency}, but both are assigned "
                            f"to stage {stage_index + 1}"
                        )
                        raise ExecutionPlanningError(msg)
                    depends_on_stages.add(dependency_stage)

            stages.append(
                ExecutionStage(
                    index=stage_index,
                    ac_indices=level,
                    depends_on_stages=tuple(sorted(depends_on_stages)),
                )
            )

        return StagedExecutionPlan(nodes=dependency_graph.nodes, stages=tuple(stages))


class DependencyAnalyzer:
    """Analyzes AC dependencies using structured signals and an LLM pass."""

    def __init__(
        self,
        llm_adapter: LLMAdapter | None = None,
        model: str | None = None,
    ) -> None:
        self._llm = llm_adapter
        self._model = model or get_dependency_analysis_model()

    async def analyze(
        self,
        acceptance_criteria: Sequence[str] | Sequence[ACDependencySpec],
    ) -> Result[DependencyGraph, DependencyAnalysisError]:
        """Analyze AC dependencies and return a graph with execution levels."""
        specs = self._normalize_specs(acceptance_criteria)
        count = len(specs)

        log.info("dependency_analyzer.analysis.started", ac_count=count)

        if count <= 1:
            nodes = tuple(ACNode(index=spec.index, content=spec.content) for spec in specs)
            levels = ((specs[0].index,),) if specs else ()
            return Result.ok(DependencyGraph(nodes=nodes, execution_levels=levels))

        structured_dependencies, serialization_reasons = self._analyze_structured_dependencies(
            specs
        )

        dependencies = {index: set(values) for index, values in structured_dependencies.items()}
        if self._llm is not None:
            try:
                llm_dependencies = await self._analyze_with_llm(
                    tuple(spec.content for spec in specs)
                )
                for index, values in llm_dependencies.items():
                    dependencies.setdefault(index, set()).update(values)
                method = "llm+structured"
            except Exception as exc:
                log.warning(
                    "dependency_analyzer.analysis.failed",
                    error=str(exc),
                    ac_count=count,
                )
                method = "structured_fallback"
        else:
            method = "structured_only"

        nodes = self._build_nodes(specs, dependencies, serialization_reasons)
        levels = _apply_serial_only_constraints(_compute_execution_levels(nodes), nodes)
        graph = DependencyGraph(nodes=nodes, execution_levels=levels)

        log.info(
            "dependency_analyzer.analysis.completed",
            ac_count=count,
            levels=graph.total_levels,
            parallelizable=graph.is_parallelizable,
            method=method,
        )

        return Result.ok(graph)

    def _normalize_specs(
        self,
        acceptance_criteria: Sequence[str] | Sequence[ACDependencySpec],
    ) -> tuple[ACDependencySpec, ...]:
        specs: list[ACDependencySpec] = []
        for index, item in enumerate(acceptance_criteria):
            if isinstance(item, ACDependencySpec):
                specs.append(
                    ACDependencySpec(
                        index=item.index,
                        content=item.content,
                        metadata=dict(item.metadata),
                        context=dict(item.context),
                        prerequisites=tuple(item.prerequisites),
                        shared_runtime_resources=tuple(item.shared_runtime_resources),
                    )
                )
            else:
                specs.append(ACDependencySpec(index=index, content=str(item)))
        return tuple(specs)

    def _analyze_structured_dependencies(
        self,
        specs: tuple[ACDependencySpec, ...],
    ) -> tuple[dict[int, set[int]], dict[int, list[str]]]:
        dependencies: dict[int, set[int]] = {spec.index: set() for spec in specs}
        reasons: dict[int, list[str]] = defaultdict(list)
        key_to_index = _build_reference_index(specs)

        for spec in specs:
            for raw_reference in spec.prerequisites:
                resolved = self._resolve_reference(raw_reference, key_to_index, len(specs))
                if resolved is None or resolved == spec.index:
                    continue
                dependencies[spec.index].add(resolved)
                reasons[spec.index].append(f"prerequisite AC {resolved + 1}")

            for metadata_key in _DEPENDENCY_METADATA_KEYS:
                raw_value = spec.metadata.get(metadata_key)
                for raw_reference in _coerce_reference_list(raw_value):
                    resolved = self._resolve_reference(raw_reference, key_to_index, len(specs))
                    if resolved is None or resolved == spec.index:
                        continue
                    dependencies[spec.index].add(resolved)
                    reasons[spec.index].append(f"metadata dependency on AC {resolved + 1}")

            for context_name, context in _iter_dependency_contexts(spec):
                for raw_reference in _collect_context_dependency_references(context):
                    resolved = self._resolve_reference(raw_reference, key_to_index, len(specs))
                    if resolved is None or resolved == spec.index:
                        continue
                    dependencies[spec.index].add(resolved)
                    reasons[spec.index].append(f"{context_name} dependency on AC {resolved + 1}")

                for raw_reference in _collect_context_shared_prerequisites(context):
                    resolved = self._resolve_reference(raw_reference, key_to_index, len(specs))
                    if resolved is None or resolved == spec.index:
                        continue
                    dependencies[spec.index].add(resolved)
                    reasons[spec.index].append(
                        f"{context_name} shared prerequisite AC {resolved + 1}"
                    )

            if _requires_serial_execution(spec.metadata):
                reasons[spec.index].append("metadata requires serialized execution")
            for context_name, context in _iter_dependency_contexts(spec):
                if _requires_serial_execution(context):
                    reasons[spec.index].append(f"{context_name} requires serialized execution")

        self._apply_shared_resource_constraints(specs, dependencies, reasons)
        return dependencies, reasons

    def _apply_shared_resource_constraints(
        self,
        specs: tuple[ACDependencySpec, ...],
        dependencies: dict[int, set[int]],
        reasons: dict[int, list[str]],
    ) -> None:
        resource_claims: dict[str, list[tuple[int, str]]] = defaultdict(list)

        for spec in specs:
            for resource in _collect_shared_runtime_resources(spec):
                resource_claims[resource.name].append((spec.index, resource.access_mode))

        for resource_name, claims in resource_claims.items():
            if len(claims) < 2:
                continue

            if not _resource_claims_conflict(claims):
                continue

            reason = f"shared runtime resource '{resource_name}'"
            ordered_indices = sorted(index for index, _mode in claims)
            for ac_index in ordered_indices:
                reasons[ac_index].append(reason)
            for predecessor, current in zip(ordered_indices, ordered_indices[1:], strict=False):
                dependencies[current].add(predecessor)

    async def _analyze_with_llm(
        self,
        criteria: tuple[str, ...],
    ) -> dict[int, list[int]]:
        """Use the LLM to detect additional dependency edges."""
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        if self._llm is None:
            raise DependencyAnalysisError("LLM adapter not configured for dependency analysis")

        ac_list = "\n".join(f"AC {index}: {content}" for index, content in enumerate(criteria))
        prompt = DEPENDENCY_ANALYSIS_PROMPT.format(ac_list=ac_list)

        response = await self._llm.complete(
            messages=[Message(role=MessageRole.USER, content=prompt)],
            config=CompletionConfig(
                model=self._model,
                temperature=0.0,
                max_tokens=1000,
            ),
        )
        if response.is_err:
            raise DependencyAnalysisError(f"LLM call failed: {response.error}")

        content = response.value.content.strip()
        if content.startswith("```"):
            lines = []
            in_block = False
            for line in content.splitlines():
                if line.startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    lines.append(line)
            content = "\n".join(lines)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DependencyAnalysisError(f"Failed to parse LLM response: {exc}") from exc

        dependencies: dict[int, list[int]] = {}
        for item in data.get("dependencies", []):
            ac_index = item.get("ac_index", 0)
            raw_dependencies = item.get("depends_on", [])
            valid_dependencies = [
                dep
                for dep in raw_dependencies
                if isinstance(dep, int) and 0 <= dep < len(criteria) and dep != ac_index
            ]
            dependencies[ac_index] = valid_dependencies

        return dependencies

    def _build_nodes(
        self,
        specs: tuple[ACDependencySpec, ...],
        dependencies: dict[int, set[int]],
        serialization_reasons: dict[int, list[str]],
    ) -> tuple[ACNode, ...]:
        nodes: list[ACNode] = []
        for spec in specs:
            reasons = tuple(dict.fromkeys(serialization_reasons.get(spec.index, ())))
            nodes.append(
                ACNode(
                    index=spec.index,
                    content=spec.content,
                    depends_on=tuple(sorted(dependencies.get(spec.index, set()))),
                    can_run_independently=not reasons,
                    requires_serial_stage=_requires_serial_execution(spec.metadata),
                    serialization_reasons=reasons,
                )
            )
        return tuple(nodes)

    def _resolve_reference(
        self,
        reference: str | int,
        key_to_index: dict[str, int],
        spec_count: int,
    ) -> int | None:
        if isinstance(reference, int):
            if 0 <= reference < spec_count:
                return reference
            if 1 <= reference <= spec_count:
                return reference - 1
            return None

        normalized = str(reference).strip().lower()
        if not normalized:
            return None
        if normalized in key_to_index:
            return key_to_index[normalized]

        match = _REFERENCE_PATTERN.match(normalized)
        if match:
            value = int(match.group(1))
            if 1 <= value <= spec_count:
                return value - 1
            if 0 <= value < spec_count:
                return value
        return None


def _compute_execution_levels(
    nodes: tuple[ACNode, ...],
) -> tuple[tuple[int, ...], ...]:
    """Compute execution levels using a deterministic topological walk."""
    if not nodes:
        return ()

    node_map = {node.index: node for node in nodes}
    if len(node_map) != len(nodes):
        msg = "Dependency graph contains duplicate AC node indices"
        raise ExecutionPlanningError(msg)

    in_degree = {node.index: 0 for node in nodes}
    dependents: dict[int, list[int]] = {node.index: [] for node in nodes}

    for node in nodes:
        for dependency in node.depends_on:
            if dependency not in node_map:
                msg = f"AC {node.index} depends on missing AC {dependency}"
                raise ExecutionPlanningError(msg)
            in_degree[node.index] += 1
            dependents[dependency].append(node.index)

    levels: list[tuple[int, ...]] = []
    remaining = set(node_map)

    while remaining:
        ready = tuple(sorted(index for index in remaining if in_degree[index] == 0))
        if not ready:
            log.warning(
                "dependency_analyzer.circular_dependency_detected",
                remaining=sorted(remaining),
            )
            ready = tuple(sorted(remaining))

        levels.append(ready)
        for node_index in ready:
            remaining.discard(node_index)
            for dependent in dependents[node_index]:
                in_degree[dependent] -= 1

    return tuple(levels)


def _apply_serial_only_constraints(
    execution_levels: tuple[tuple[int, ...], ...],
    nodes: tuple[ACNode, ...],
) -> tuple[tuple[int, ...], ...]:
    """Split serial-only ACs into their own stages while keeping level order."""
    node_map = {node.index: node for node in nodes}
    stages: list[tuple[int, ...]] = []

    for level in execution_levels:
        parallel_safe = tuple(
            index
            for index in level
            if not node_map.get(index, ACNode(index, "")).requires_serial_stage
        )
        serial_only = tuple(
            index for index in level if node_map.get(index, ACNode(index, "")).requires_serial_stage
        )

        if parallel_safe:
            stages.append(parallel_safe)
        for index in serial_only:
            stages.append((index,))

    return tuple(stages)


def _requires_serial_execution(metadata: dict[str, Any]) -> bool:
    """Return True when metadata explicitly disables parallel execution."""
    for key in _SERIAL_METADATA_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        if key == "parallel_safe":
            if value is False:
                return True
            continue
        if key == "parallelizable":
            if value is False:
                return True
            continue
        if bool(value):
            return True
    return False


def _format_reference(reference: str | int) -> str:
    if isinstance(reference, int):
        return f"AC {reference + 1}" if reference >= 0 else str(reference)
    return str(reference).strip()


def _coerce_reference_list(value: Any) -> tuple[str | int, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        return (value,)
    if isinstance(value, dict):
        extracted = _extract_reference_from_mapping(value)
        return (extracted,) if extracted is not None else ()
    if isinstance(value, Sequence):
        refs: list[str | int] = []
        for item in value:
            if isinstance(item, (str, int)):
                refs.append(item)
            elif isinstance(item, dict):
                extracted = _extract_reference_from_mapping(item)
                if extracted is not None:
                    refs.append(extracted)
        return tuple(refs)
    return ()


def _collect_shared_runtime_resources(
    spec: ACDependencySpec,
) -> tuple[ACSharedRuntimeResource, ...]:
    resources = list(spec.shared_runtime_resources)
    for source in _iter_resource_sources(spec):
        for key in _RESOURCE_METADATA_KEYS:
            raw_value = source.get(key)
            if raw_value is None:
                continue
            if isinstance(raw_value, str):
                resources.append(ACSharedRuntimeResource(name=raw_value))
            elif isinstance(raw_value, dict):
                name = raw_value.get("name")
                if isinstance(name, str) and name.strip():
                    resources.append(
                        ACSharedRuntimeResource(
                            name=name,
                            access_mode=str(
                                raw_value.get("mode", raw_value.get("access_mode", "write"))
                            ),
                        )
                    )
            elif isinstance(raw_value, Sequence):
                for item in raw_value:
                    if isinstance(item, str):
                        resources.append(ACSharedRuntimeResource(name=item))
                    elif isinstance(item, dict):
                        name = item.get("name")
                        if isinstance(name, str) and name.strip():
                            resources.append(
                                ACSharedRuntimeResource(
                                    name=name,
                                    access_mode=str(
                                        item.get("mode", item.get("access_mode", "write"))
                                    ),
                                )
                            )
    return tuple(resources)


def _iter_identity_candidates(*sources: dict[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("key", "id", "ac_id", "slug", "name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    return tuple(candidates)


def _build_reference_index(specs: tuple[ACDependencySpec, ...]) -> dict[str, int]:
    alias_to_index: dict[str, int] = {}
    for spec in specs:
        aliases = [*filter(None, (spec.key,)), *_collect_provider_aliases(spec)]
        for alias in aliases:
            normalized = alias.strip().lower()
            if not normalized or normalized in alias_to_index:
                continue
            alias_to_index[normalized] = spec.index
    return alias_to_index


def _collect_provider_aliases(spec: ACDependencySpec) -> tuple[str, ...]:
    aliases: list[str] = []
    for source in _iter_resource_sources(spec):
        for key in _PROVIDER_METADATA_KEYS:
            aliases.extend(
                str(reference).strip()
                for reference in _coerce_reference_list(source.get(key))
                if str(reference).strip()
            )
    return tuple(dict.fromkeys(aliases))


def _iter_dependency_contexts(spec: ACDependencySpec) -> tuple[tuple[str, dict[str, Any]], ...]:
    contexts: list[tuple[str, dict[str, Any]]] = []
    if spec.context:
        contexts.append(("context", spec.context))
    for key in _CONTEXT_METADATA_KEYS:
        raw_context = spec.metadata.get(key)
        if isinstance(raw_context, dict):
            contexts.append((f"metadata {key}", raw_context))
    return tuple(contexts)


def _collect_context_dependency_references(context: dict[str, Any]) -> tuple[str | int, ...]:
    references: list[str | int] = []
    for key in _DEPENDENCY_METADATA_KEYS:
        references.extend(_coerce_reference_list(context.get(key)))
    return tuple(references)


def _collect_context_shared_prerequisites(context: dict[str, Any]) -> tuple[str | int, ...]:
    references: list[str | int] = []
    for key in _SHARED_PREREQUISITE_KEYS:
        references.extend(_coerce_reference_list(context.get(key)))
    return tuple(references)


def _iter_resource_sources(spec: ACDependencySpec) -> tuple[dict[str, Any], ...]:
    sources: list[dict[str, Any]] = [spec.metadata]
    if spec.context:
        sources.append(spec.context)
    for _context_name, context in _iter_dependency_contexts(spec):
        if context not in sources:
            sources.append(context)
    return tuple(sources)


def _extract_reference_from_mapping(value: dict[str, Any]) -> str | int | None:
    for key in _REFERENCE_DICT_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, (str, int)):
            return candidate
    return None


def _normalize_resource_access_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"read", "readonly", "r"}:
        return "read"
    return "write"


def _resource_claims_conflict(claims: Sequence[tuple[int, str]]) -> bool:
    return any(mode != "read" for _index, mode in claims)


__all__ = [
    "ACDependencySpec",
    "ACNode",
    "ACSharedRuntimeResource",
    "DependencyAnalysisError",
    "DependencyAnalyzer",
    "DependencyGraph",
    "ExecutionPlanningError",
    "ExecutionStage",
    "HybridExecutionPlanner",
    "StagedExecutionPlan",
]
