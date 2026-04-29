"""Fixture loading for recursive RLM loop executions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.rlm.loop import (
    MAX_RLM_AC_TREE_DEPTH,
    MAX_RLM_AMBIGUITY_THRESHOLD,
    RLMOuterScaffoldState,
    RLMRunConfig,
)

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.rlm.trace import RLMTraceStore

RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION = "rlm.recursive_fixture_config.v1"


@dataclass(frozen=True, slots=True)
class RLMRecursiveFixture:
    """Executable fixture for the isolated recursive RLM path."""

    source_path: Path
    payload: Mapping[str, Any]
    fixture_id: str
    schema_version: str
    description: str
    target_path: str
    target_encoding: str
    target_lines: tuple[str, ...]
    initial_prompt: str
    initial_state: Mapping[str, Any]
    iteration_limits: Mapping[str, Any]
    expected_outputs: Mapping[str, Any]
    completion_requirements: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def write_target(self, cwd: Path) -> Path:
        """Materialize the fixture target file under ``cwd`` and return its path."""
        target_path = cwd / self.target_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            "\n".join(self.target_lines) + "\n",
            encoding=self.target_encoding,
        )
        return target_path

    def to_run_config(
        self,
        *,
        cwd: Path,
        hermes_runtime: AgentRuntime | None = None,
        trace_store: RLMTraceStore | None = None,
        dry_run: bool = False,
        debug: bool = False,
    ) -> RLMRunConfig:
        """Build an ``RLMRunConfig`` using the fixture-owned execution knobs."""
        return RLMRunConfig(
            target=self.target_path,
            cwd=cwd,
            fixture_id=self.fixture_id,
            initial_prompt=self.initial_prompt,
            max_depth=_bounded_int(
                self.iteration_limits.get("max_depth"),
                "recursive_run.iteration_limits.max_depth",
                minimum=0,
                maximum=MAX_RLM_AC_TREE_DEPTH,
            ),
            ambiguity_threshold=_bounded_float(
                self.iteration_limits.get("ambiguity_threshold"),
                "recursive_run.iteration_limits.ambiguity_threshold",
                minimum=0.0,
                maximum=MAX_RLM_AMBIGUITY_THRESHOLD,
            ),
            chunk_line_limit=_positive_int(
                self.iteration_limits.get("chunk_line_limit"),
                "recursive_run.iteration_limits.chunk_line_limit",
            ),
            max_atomic_chunks=_positive_int(
                self.iteration_limits.get("max_atomic_chunks"),
                "recursive_run.iteration_limits.max_atomic_chunks",
            ),
            max_iterations=_positive_int(
                self.iteration_limits.get("max_iterations"),
                "recursive_run.iteration_limits.max_iterations",
            ),
            dry_run=dry_run,
            debug=debug,
            hermes_runtime=hermes_runtime,
            trace_store=trace_store,
        )

    def initial_scaffold_state(self, *, cwd: Path) -> RLMOuterScaffoldState:
        """Create and validate the fixture-declared initial outer scaffold state."""
        state = RLMOuterScaffoldState.initialize(self.to_run_config(cwd=cwd))
        self.assert_initial_state_matches(state)
        return state

    def assert_initial_state_matches(self, state: RLMOuterScaffoldState) -> None:
        """Validate an initialized scaffold against the fixture's state contract."""
        actual = state.to_dict()
        expected = self.initial_state

        _assert_equal(actual.get("run_id"), expected.get("run_id"), "initial_state.run_id")
        _assert_equal(
            actual.get("run_state"),
            expected.get("run_state"),
            "initial_state.run_state",
        )
        _assert_equal(
            actual.get("work_queue"),
            expected.get("work_queue"),
            "initial_state.work_queue",
        )
        _assert_equal(
            actual.get("max_iterations"),
            expected.get("max_iterations"),
            "initial_state.max_iterations",
        )
        _assert_equal(
            actual.get("max_depth"),
            expected.get("max_depth"),
            "initial_state.max_depth",
        )
        _assert_equal(
            actual.get("ambiguity_threshold"),
            expected.get("ambiguity_threshold"),
            "initial_state.ambiguity_threshold",
        )

        root_node_id = _string(
            expected.get("root_rlm_node_id"),
            "initial_state.root_rlm_node_id",
        )
        root_ac_node_id = _string(
            expected.get("root_ac_node_id"),
            "initial_state.root_ac_node_id",
        )
        root_node = _mapping(
            _mapping(actual.get("rlm_nodes"), "actual.rlm_nodes").get(root_node_id),
            f"actual.rlm_nodes.{root_node_id}",
        )
        ac_tree_payload = _mapping(actual.get("ac_tree"), "actual.ac_tree")
        ac_nodes = _mapping(ac_tree_payload.get("nodes"), "actual.ac_tree.nodes")
        root_ac = _mapping(
            ac_nodes.get(root_ac_node_id),
            f"actual.ac_tree.nodes.{root_ac_node_id}",
        )
        _assert_equal(
            root_node.get("state"),
            expected.get("root_node_state"),
            "initial_state.root_node_state",
        )
        _assert_equal(
            root_ac.get("status"),
            expected.get("root_ac_status"),
            "initial_state.root_ac_status",
        )
        expected_root_content = expected.get("root_ac_content")
        if expected_root_content is not None:
            _assert_equal(
                root_ac.get("content"),
                expected_root_content,
                "initial_state.root_ac_content",
            )

    def assert_result_matches(self, result: object) -> None:
        """Validate a completed recursive RLM result against expected fixture output."""
        expected = self.expected_outputs
        _assert_attr_equal(result, "status", expected.get("status"), "expected_outputs.status")
        _assert_attr_equal(
            result,
            "target_kind",
            expected.get("target_kind"),
            "expected_outputs.target_kind",
        )
        _assert_attr_equal(
            result,
            "hermes_subcall_count",
            expected.get("hermes_subcall_count"),
            "expected_outputs.hermes_subcall_count",
        )

        expected_termination_reason = expected.get("termination_reason")
        if expected_termination_reason is not None:
            actual_reason = getattr(result, "termination_reason", None)
            actual_value = getattr(actual_reason, "value", actual_reason)
            _assert_equal(
                actual_value,
                expected_termination_reason,
                "expected_outputs.termination_reason",
            )

        expected_selected = _string_tuple(expected.get("selected_chunk_ids"))
        if expected_selected:
            actual_selected = _result_selected_chunk_ids(result)
            _assert_equal(
                list(actual_selected),
                list(expected_selected),
                "expected_outputs.selected_chunk_ids",
            )

    @property
    def expected_selected_chunk_ids(self) -> tuple[str, ...]:
        """Return fixture-declared selected chunk IDs."""
        return _string_tuple(self.expected_outputs.get("selected_chunk_ids"))

    @property
    def expected_omitted_chunk_ids(self) -> tuple[str, ...]:
        """Return fixture-declared omitted chunk IDs."""
        return _string_tuple(self.expected_outputs.get("omitted_chunk_ids"))


def load_recursive_fixture(path: Path) -> RLMRecursiveFixture:
    """Load and validate a recursive RLM fixture JSON document."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"Unable to read RLM recursive fixture: {path}"
        raise ValueError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Invalid RLM recursive fixture JSON: {path}"
        raise ValueError(msg) from exc

    if not isinstance(payload, Mapping):
        msg = f"RLM recursive fixture must be a JSON object: {path}"
        raise ValueError(msg)
    return recursive_fixture_from_mapping(payload, source_path=path)


def recursive_fixture_from_mapping(
    payload: Mapping[str, Any],
    *,
    source_path: Path | None = None,
) -> RLMRecursiveFixture:
    """Build a recursive fixture object from an already-loaded mapping."""
    target = _mapping(payload.get("target"), "target")
    recursive_run = _mapping(payload.get("recursive_run"), "recursive_run")
    recursive_schema_version = _string(
        recursive_run.get("schema_version"),
        "recursive_run.schema_version",
    )
    if recursive_schema_version != RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION:
        msg = (
            "RLM recursive fixture field 'recursive_run.schema_version' must be "
            f"{RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION!r}"
        )
        raise ValueError(msg)
    iteration_limits = _mapping(
        recursive_run.get("iteration_limits"),
        "recursive_run.iteration_limits",
    )
    initial_state = _mapping(recursive_run.get("initial_state"), "recursive_run.initial_state")
    expected_outputs = _mapping(
        recursive_run.get("expected_outputs"),
        "recursive_run.expected_outputs",
    )

    target_lines = _string_sequence(target.get("lines"), "target.lines")
    if not target_lines:
        msg = "RLM recursive fixture target.lines must contain at least one line"
        raise ValueError(msg)
    declared_line_count = target.get("line_count")
    if isinstance(declared_line_count, int) and not isinstance(declared_line_count, bool):
        if declared_line_count != len(target_lines):
            msg = "RLM recursive fixture target.line_count does not match target.lines"
            raise ValueError(msg)

    _validate_required_recursive_fields(iteration_limits, initial_state, expected_outputs)

    completion_requirements = _mapping_tuple(
        payload.get("completion_requirements", ()),
        "completion_requirements",
    )
    return RLMRecursiveFixture(
        source_path=source_path or Path("<memory>"),
        payload=payload,
        fixture_id=_string(payload.get("fixture_id"), "fixture_id"),
        schema_version=_string(payload.get("schema_version"), "schema_version"),
        description=_string(payload.get("description"), "description"),
        target_path=_string(target.get("path"), "target.path"),
        target_encoding=_string(target.get("encoding", "utf-8"), "target.encoding"),
        target_lines=target_lines,
        initial_prompt=_string(
            recursive_run.get("initial_prompt"),
            "recursive_run.initial_prompt",
        ),
        initial_state=initial_state,
        iteration_limits=iteration_limits,
        expected_outputs=expected_outputs,
        completion_requirements=completion_requirements,
    )


def _validate_required_recursive_fields(
    iteration_limits: Mapping[str, Any],
    initial_state: Mapping[str, Any],
    expected_outputs: Mapping[str, Any],
) -> None:
    _bounded_int(
        iteration_limits.get("max_depth"),
        "recursive_run.iteration_limits.max_depth",
        minimum=0,
        maximum=MAX_RLM_AC_TREE_DEPTH,
    )
    _bounded_float(
        iteration_limits.get("ambiguity_threshold"),
        "recursive_run.iteration_limits.ambiguity_threshold",
        minimum=0.0,
        maximum=MAX_RLM_AMBIGUITY_THRESHOLD,
    )
    for field_name in ("chunk_line_limit", "max_atomic_chunks", "max_iterations"):
        _positive_int(
            iteration_limits.get(field_name),
            f"recursive_run.iteration_limits.{field_name}",
        )
    for field_name in (
        "run_id",
        "run_state",
        "root_rlm_node_id",
        "root_ac_node_id",
        "root_node_state",
        "root_ac_status",
    ):
        _string(initial_state.get(field_name), f"recursive_run.initial_state.{field_name}")
    _string_sequence(initial_state.get("work_queue"), "recursive_run.initial_state.work_queue")
    for field_name in ("status", "target_kind", "termination_reason"):
        _string(expected_outputs.get(field_name), f"recursive_run.expected_outputs.{field_name}")
    _positive_int(
        expected_outputs.get("hermes_subcall_count"),
        "recursive_run.expected_outputs.hermes_subcall_count",
    )


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"RLM recursive fixture field {field_name!r} must be an object"
        raise ValueError(msg)
    return value


def _mapping_tuple(value: object, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        msg = f"RLM recursive fixture field {field_name!r} must be an array"
        raise ValueError(msg)
    mappings: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        mappings.append(_mapping(item, f"{field_name}[{index}]"))
    return tuple(mappings)


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"RLM recursive fixture field {field_name!r} must be a non-empty string"
        raise ValueError(msg)
    return value


def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        msg = f"RLM recursive fixture field {field_name!r} must be an array of strings"
        raise ValueError(msg)
    items = tuple(item for item in value if isinstance(item, str) and item)
    if len(items) != len(value):
        msg = f"RLM recursive fixture field {field_name!r} must contain only strings"
        raise ValueError(msg)
    return items


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"RLM recursive fixture field {field_name!r} must be a positive integer"
        raise ValueError(msg)
    return value


def _bounded_int(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        msg = (
            f"RLM recursive fixture field {field_name!r} must be an integer "
            f"between {minimum} and {maximum}"
        )
        raise ValueError(msg)
    return value


def _bounded_float(value: object, field_name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = (
            f"RLM recursive fixture field {field_name!r} must be a number "
            f"between {minimum} and {maximum}"
        )
        raise ValueError(msg)
    converted = float(value)
    if converted < minimum or converted > maximum:
        msg = (
            f"RLM recursive fixture field {field_name!r} must be a number "
            f"between {minimum} and {maximum}"
        )
        raise ValueError(msg)
    return converted


def _assert_equal(actual: object, expected: object, field_name: str) -> None:
    if actual != expected:
        msg = f"RLM recursive fixture mismatch for {field_name}: expected {expected!r}, got {actual!r}"
        raise ValueError(msg)


def _assert_attr_equal(
    obj: object,
    attr_name: str,
    expected: object,
    field_name: str,
) -> None:
    if expected is None:
        return
    _assert_equal(getattr(obj, attr_name, None), expected, field_name)


def _result_selected_chunk_ids(result: object) -> tuple[str, ...]:
    atomic_execution = getattr(result, "atomic_execution", None)
    if atomic_execution is None:
        return ()
    hermes_subcall = getattr(atomic_execution, "hermes_subcall", None)
    if hermes_subcall is None:
        return ()
    selected = getattr(hermes_subcall, "selected_chunk_ids", ())
    return _string_tuple(selected)


__all__ = [
    "RLM_RECURSIVE_FIXTURE_SCHEMA_VERSION",
    "RLMRecursiveFixture",
    "load_recursive_fixture",
    "recursive_fixture_from_mapping",
]
