"""Unit tests for the isolated RLM MVP loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.ac_tree import ACStatus
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult
from ouroboros.persistence.event_store import EventStore
from ouroboros.rlm import (
    HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT,
    RLM_HERMES_CALL_FAILED_EVENT,
    RLM_HERMES_CALL_STARTED_EVENT,
    RLM_HERMES_CALL_SUCCEEDED_EVENT,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
    RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION,
    RLM_SYNTHESIZED_SUBCALL_SUMMARY_SCHEMA_VERSION,
    RLMHermesCallContext,
    RLMHermesSubcall,
    RLMHermesTraceRecord,
    RLMNodeLifecycleState,
    RLMOuterScaffoldState,
    RLMParentExecutionState,
    RLMParentNodeSummary,
    RLMRecordedSubcallResult,
    RLMRunConfig,
    RLMRunLifecycleState,
    RLMTerminationReason,
    RLMTraceStore,
    capture_completed_hermes_subcall_result,
    run_rlm_loop,
    synthesize_parent_node_summary,
    synthesize_subcall_summary,
)
import ouroboros.rlm.loop as rlm_loop


class _FakeHermesRuntime:
    """Capture Hermes RPC-style calls made by the RLM loop."""

    def __init__(self, responses: list[TaskResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )
        if self._responses:
            return Result.ok(self._responses.pop(0))

        return Result.ok(
            TaskResult(
                success=True,
                final_message=json.dumps(
                    {
                        "schema_version": "rlm.hermes.output.v1",
                        "mode": "execute_atomic",
                        "verdict": "passed",
                        "confidence": 0.9,
                        "result": {"summary": "atomic execution complete"},
                        "evidence_references": [],
                        "residual_gaps": [],
                    }
                ),
                messages=(),
            )
        )


class _FailingHermesRuntime:
    """Hermes RPC test double that returns an adapter-level failure."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_handle": resume_handle,
                "resume_session_id": resume_session_id,
            }
        )
        return Result.err(
            ProviderError(
                "adapter down",
                provider="hermes",
                details={"error_type": "UnitTestFailure"},
            )
        )


def _assert_rlm_subcall_id(value: object) -> str:
    """Assert and return a generated RLM Hermes sub-call ID."""
    assert isinstance(value, str)
    assert value.startswith("rlm_subcall_")
    assert len(value) == len("rlm_subcall_") + 32
    return value


def _write_benchmark_source(root: Path, relative_path: str, content: str) -> None:
    """Create a source file used by the RLM dogfood benchmark fixture."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_minimal_benchmark_sources(root: Path) -> None:
    """Create enough source files for benchmark evidence citation tests."""
    _write_benchmark_source(
        root,
        "src/ouroboros/cli/commands/rlm.py",
        "\n".join(
            [
                "from ouroboros.rlm import RLMRunConfig, run_rlm_loop",
                "async def _run_with_default_trace_store(config, benchmark_id=None):",
                "    return await run_rlm_loop(config)",
                "def command(",
                "    target='src',",
                "):",
                "    return run_rlm_loop(RLMRunConfig(target=target, cwd='.'))",
                '__all__ = ["command"]',
            ]
        ),
    )
    _write_benchmark_source(
        root,
        "src/ouroboros/cli/main.py",
        "\n".join(
            [
                "from ouroboros.cli.commands import (",
                "    rlm,",
                "    run,",
                ")",
                'app.command(name="rlm")(rlm.command)',
            ]
        ),
    )
    _write_benchmark_source(
        root,
        "src/ouroboros/rlm/loop.py",
        "\n".join(
            [
                "MAX_RLM_AC_TREE_DEPTH = 5",
                "MAX_RLM_AMBIGUITY_THRESHOLD = 0.2",
                "HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT = 'Do not invoke ooo'",
                "Return a single JSON object",
                "def _chunk_lines(",
                "    pass",
                "def _build_atomic_execution_prompt(",
                "    pass",
                "async def _execute_hermes_atomic_subcall(",
                "    await hermes_runtime.execute_task_to_result(",
                "        system_prompt=HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT",
                "    )",
            ]
        ),
    )
    _write_benchmark_source(
        root,
        "src/ouroboros/orchestrator/hermes_runtime.py",
        "\n".join(
            [
                "class HermesCliRuntime:",
                "    async def execute_task(self):",
                "        args = ['hermes', 'chat']",
                "        args.extend(['-Q', '--source', 'tool'])",
                "        args.extend([\"-q\", full_prompt])",
                "    async def execute_task_to_result(",
                "        pass",
            ]
        ),
    )
    _write_benchmark_source(
        root,
        "src/ouroboros/evolution/wonder.py",
        "\n".join(
            [
                "class WonderEngine:",
                "    async def wonder(",
                "        self, current_ontology, evaluation_summary, execution_output, lineage, seed",
                "    ):",
                "        return self._build_prompt(",
                "            current_ontology, evaluation_summary, execution_output, lineage, seed",
                "        )",
                "    def _build_prompt(",
                "        self, ontology, evaluation_summary, execution_output, lineage, seed",
                "    ):",
                "        return 'generation-level wonder output'",
                "    def _parse_response(",
                "        self, content, seed=None",
                "    ):",
                "        pass",
            ]
        ),
    )
    _write_benchmark_source(
        root,
        "src/ouroboros/evolution/reflect.py",
        "\n".join(
            [
                "class ReflectOutput:",
                "    refined_acs = ()",
                "    ontology_mutations = ()",
                "    reasoning = ''",
                "class ReflectEngine:",
                "    async def reflect(",
                "        self, current_seed, execution_output, evaluation_summary, wonder_output, lineage",
                "    ):",
                "        return ReflectOutput()",
            ]
        ),
    )


def test_hermes_trace_record_schema_defaults() -> None:
    """Trace records expose Hermes prompt/completion ancestry defaults."""
    record = RLMHermesTraceRecord()

    assert record.prompt == ""
    assert record.completion == ""
    assert record.parent_call_id is None
    assert record.depth == 0

    payload = record.to_dict()
    assert payload["schema_version"] == "rlm.trace.v1"
    assert payload["prompt"] == ""
    assert payload["completion"] == ""
    assert payload["trace_id"] is None
    assert payload["subcall_id"] is None
    assert payload["parent_trace_id"] is None
    assert payload["causal_parent_event_id"] is None
    assert payload["parent_call_id"] is None
    assert payload["generated_child_ac_node_ids"] == []
    assert payload["depth"] == 0


@pytest.mark.asyncio
async def test_dogfood_benchmark_output_cites_at_least_three_source_files(
    tmp_path: Path,
) -> None:
    """The src dogfood benchmark artifact carries at least three source citations."""
    _write_minimal_benchmark_sources(tmp_path)

    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            dry_run=True,
        )
    )

    benchmark_output = result.benchmark_output
    assert benchmark_output is not None
    assert benchmark_output.benchmark_id == "rlm-mvp-src-dogfood-v1"
    assert benchmark_output.cited_source_file_count >= 3
    assert benchmark_output.generated_rlm_tree_depth == 0

    cited_source_paths = {evidence.source_path for evidence in benchmark_output.source_evidence}
    assert {
        "src/ouroboros/cli/commands/rlm.py",
        "src/ouroboros/cli/main.py",
        "src/ouroboros/rlm/loop.py",
        "src/ouroboros/evolution/wonder.py",
        "src/ouroboros/evolution/reflect.py",
    }.issubset(cited_source_paths)

    payload = benchmark_output.to_dict()
    assert payload["generated_rlm_tree_depth"] == 0
    assert payload["cited_source_file_count"] >= 3
    assert len({item["source_path"] for item in payload["source_evidence"]}) >= 3
    wonder_evidence = [
        item
        for item in payload["source_evidence"]
        if item["source_path"] == "src/ouroboros/evolution/wonder.py"
    ]
    assert len(wonder_evidence) == 1
    assert "Wonder input construction" in wonder_evidence[0]["claim_categories"]
    assert "Benchmark migration question support" in wonder_evidence[0]["claim_categories"]
    assert "Wonder prompt construction" in wonder_evidence[0]["claim"]
    reflect_evidence = [
        item
        for item in payload["source_evidence"]
        if item["source_path"] == "src/ouroboros/evolution/reflect.py"
    ]
    assert len(reflect_evidence) == 1
    assert "Reflect output" in reflect_evidence[0]["claim_categories"]
    assert "Generation-level ontology migration" in reflect_evidence[0]["claim_categories"]
    assert "benchmark inspection" in reflect_evidence[0]["claim"]
    assert "src/ouroboros/cli/commands/rlm.py" in benchmark_output.report_markdown
    assert "src/ouroboros/cli/main.py" in benchmark_output.report_markdown
    assert "src/ouroboros/rlm/loop.py" in benchmark_output.report_markdown
    assert "src/ouroboros/evolution/wonder.py" in benchmark_output.report_markdown
    assert "src/ouroboros/evolution/reflect.py" in benchmark_output.report_markdown
    assert "Generated RLM tree depth: `0`" in benchmark_output.report_markdown


def test_dogfood_benchmark_evidence_specs_resolve_to_precise_repository_spans() -> None:
    """Every built-in benchmark claim points at a concrete source span."""
    repo_root = Path(__file__).resolve().parents[3]

    for spec in rlm_loop.RLM_BENCHMARK_EVIDENCE_SPECS:
        source_path = repo_root / spec.source_path
        assert source_path.is_file(), spec.source_path

        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start_line, end_line = rlm_loop._source_span_for_spec(source_path, spec)
        span = "\n".join(lines[start_line - 1 : end_line])

        assert 1 <= start_line <= end_line <= len(lines), spec.source_path
        assert spec.start_marker in span, spec.source_path
        if spec.end_marker is not None:
            assert spec.end_marker in span, spec.source_path
        assert end_line - start_line <= 180, spec.source_path


@pytest.mark.asyncio
async def test_dogfood_benchmark_cited_sources_are_supplied_as_target_chunks(
    tmp_path: Path,
) -> None:
    """Benchmark source citations come from the chunks supplied to Hermes."""
    _write_minimal_benchmark_sources(tmp_path)
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            max_atomic_chunks=3,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.benchmark_output is not None
    cited_source_paths = {
        evidence.source_path for evidence in result.benchmark_output.source_evidence
    }
    prompts = [json.loads(str(call["prompt"])) for call in hermes_runtime.calls]
    supplied_source_paths = {
        chunk["source_path"]
        for prompt in prompts
        for chunk in prompt["context"]["chunks"]
        if chunk["source_path"] is not None
    }

    assert result.benchmark_output.cited_source_file_count >= 3
    assert cited_source_paths.issubset(supplied_source_paths)


def test_hermes_subcall_serializes_trace_record_fields() -> None:
    """Atomic Hermes sub-calls produce replayable trace record fragments."""
    subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="rlm_generation_test",
        rlm_node_id="rlm_child",
        ac_node_id="ac_child",
        prompt="bounded prompt",
        completion="bounded completion",
        parent_call_id="rlm_parent_call",
        depth=2,
        exit_code=0,
        call_id="rlm_call_child",
    )

    record = subcall.to_trace_record()

    assert record.prompt == "bounded prompt"
    assert record.completion == "bounded completion"
    assert record.parent_call_id == "rlm_parent_call"
    assert record.depth == 2
    assert record.to_dict() == {
        "schema_version": "rlm.trace.v1",
        "trace_id": None,
        "subcall_id": "rlm_call_child",
        "parent_trace_id": None,
        "causal_parent_event_id": None,
        "call_id": "rlm_call_child",
        "parent_call_id": "rlm_parent_call",
        "runtime": "hermes",
        "mode": "execute_atomic",
        "generation_id": "rlm_generation_test",
        "rlm_node_id": "rlm_child",
        "ac_node_id": "ac_child",
        "depth": 2,
        "selected_chunk_ids": [],
        "generated_child_ac_node_ids": [],
        "resume_handle_id": None,
        "runtime_handle_id": None,
        "prompt": "bounded prompt",
        "completion": "bounded completion",
        "prompt_hash": record.prompt_hash,
        "response_hash": record.response_hash,
        "success": True,
        "exit_code": 0,
        "elapsed_ms": None,
        "adapter_error": None,
        "system_prompt_hash": None,
    }


def test_hermes_subcall_uses_trace_field_defaults() -> None:
    """Prompt, completion, parent_call_id, and depth have safe defaults."""
    subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="rlm_generation_test",
        rlm_node_id="rlm_root",
        ac_node_id="ac_root",
    )

    assert subcall.prompt == ""
    assert subcall.completion == ""
    assert subcall.parent_call_id is None
    assert subcall.depth == 0
    assert subcall.to_trace_record().to_dict()["depth"] == 0


def test_hermes_subcall_serializes_causal_child_ac_links() -> None:
    """Generated child AC IDs and trace ancestry flow into persisted records."""
    subcall = RLMHermesSubcall(
        mode=RLM_HERMES_SYNTHESIZE_PARENT_MODE,
        generation_id="rlm_generation_test",
        rlm_node_id="rlm_parent",
        ac_node_id="ac_parent",
        prompt="{}",
        completion="bounded completion",
        depth=1,
        exit_code=0,
        call_id="rlm_call_parent",
        trace_id="rlm_trace_parent",
        parent_trace_id="rlm_trace_grandparent",
        causal_parent_event_id="rlm_call_grandparent",
        generated_child_ac_node_ids=("ac_child_1", "ac_child_2"),
    )

    record = subcall.to_trace_record()

    assert record.trace_id == "rlm_trace_parent"
    assert record.parent_trace_id == "rlm_trace_grandparent"
    assert record.causal_parent_event_id == "rlm_call_grandparent"
    assert record.generated_child_ac_node_ids == ("ac_child_1", "ac_child_2")
    assert record.to_event_data()["ac_node"]["child_ids"] == ["ac_child_1", "ac_child_2"]
    assert record.to_event_data()["replay"]["creates_ac_node_ids"] == [
        "ac_child_1",
        "ac_child_2",
    ]


def test_hermes_call_context_serializes_recursive_ancestry() -> None:
    """Hermes prompt call context carries call ID, parent call ID, and depth."""
    context = RLMHermesCallContext(
        call_id="rlm_call_child",
        parent_call_id="rlm_call_parent",
        depth=2,
    )

    assert context.to_dict() == {
        "call_id": "rlm_call_child",
        "parent_call_id": "rlm_call_parent",
        "depth": 2,
    }


def test_hermes_call_context_creates_nested_child_context() -> None:
    """Nested Hermes calls link to the current call and increment depth."""
    parent_context = RLMHermesCallContext(
        call_id="rlm_call_current",
        parent_call_id="rlm_call_root",
        depth=2,
    )

    child_context = parent_context.child("rlm_call_child")

    assert child_context.to_dict() == {
        "call_id": "rlm_call_child",
        "parent_call_id": "rlm_call_current",
        "depth": 3,
    }


@pytest.mark.asyncio
async def test_same_generation_calls_hermes_during_atomic_execution(tmp_path: Path) -> None:
    """A non-dry RLM generation must invoke Hermes for atomic AC execution."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "example.py").write_text("def example():\n    return 1\n", encoding="utf-8")
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.status == "completed"
    assert result.hermes_subcall_count == 1
    assert result.atomic_execution is not None
    assert result.atomic_execution.generation_id == result.generation_id
    assert result.atomic_execution.hermes_subcall.generation_id == result.generation_id
    assert result.atomic_execution.hermes_subcall.mode == "execute_atomic"

    assert len(hermes_runtime.calls) == 1
    call = hermes_runtime.calls[0]
    assert call["tools"] == []
    assert call["system_prompt"] == HERMES_ATOMIC_EXECUTION_SYSTEM_PROMPT

    prompt_envelope = json.loads(str(call["prompt"]))
    assert prompt_envelope["mode"] == "execute_atomic"
    assert prompt_envelope["run"]["rlm_run_id"] == result.generation_id
    assert prompt_envelope["ac_node"]["status"] == "executing"
    assert prompt_envelope["constraints"]["must_not_call_ouroboros"] is True
    assert prompt_envelope["call_context"] == {
        "call_id": "rlm_call_atomic_root",
        "parent_call_id": None,
        "depth": 0,
    }
    parent_execution_context = prompt_envelope["parent_execution_context"]
    assert parent_execution_context == prompt_envelope["context"]["parent_execution_context"]
    assert parent_execution_context == {
        "schema_version": RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
        "generation_id": result.generation_id,
        "mode": "execute_atomic",
        "parent_node_id": None,
        "parent_ac_node_id": None,
        "parent_call_id": None,
        "parent_trace_id": None,
        "current_node_id": "rlm_node_root",
        "current_ac_node_id": "rlm_ac_root",
        "current_call_id": "rlm_call_atomic_root",
        "current_trace_id": "rlm_trace_rlm_call_atomic_root",
        "current_depth": 0,
        "child_order": None,
        "sibling_count": 1,
        "prior_sibling_result_count": 0,
        "completed_sibling_count": 0,
        "failed_sibling_count": 0,
        "recorded_child_result_ids": [],
        "recorded_child_node_ids": [],
        "recorded_child_ac_node_ids": [],
        "recorded_child_call_ids": [],
        "recorded_child_chunk_ids": [],
        "synthesized_summary_present": False,
    }
    assert prompt_envelope["trace"]["call_id"] == "rlm_call_atomic_root"
    assert prompt_envelope["trace"]["parent_call_id"] is None
    assert prompt_envelope["trace"]["depth"] == 0
    assert prompt_envelope["trace"]["selected_chunk_ids"] == ["src/example.py:1-2"]
    subcall_id = _assert_rlm_subcall_id(prompt_envelope["trace"]["subcall_id"])
    assert result.atomic_execution.hermes_subcall.subcall_id == subcall_id
    assert result.atomic_execution.hermes_subcall.to_trace_record().subcall_id == subcall_id


@pytest.mark.asyncio
async def test_atomic_execution_spawns_chunk_level_hermes_subcalls_for_large_file(
    tmp_path: Path,
) -> None:
    """Multi-chunk atomic execution calls Hermes for each chunk, then synthesizes."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def one(): return 1",
                "def two(): return 2",
                "def three(): return 3",
                "def four(): return 4",
                "def five(): return 5",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="large.py",
            cwd=tmp_path,
            chunk_line_limit=2,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.status == "completed"
    assert result.hermes_subcall_count == 4
    assert result.atomic_execution is not None

    atomic_execution = result.atomic_execution
    assert len(atomic_execution.chunk_subcalls) == 3
    assert atomic_execution.hermes_subcall.mode == RLM_HERMES_SYNTHESIZE_PARENT_MODE
    assert atomic_execution.hermes_subcall.call_id == "rlm_call_atomic_synthesis"
    assert atomic_execution.hermes_subcall.depth == 0
    assert atomic_execution.hermes_subcall.parent_call_id is None

    chunk_ids = [subcall.chunk_id for subcall in atomic_execution.chunk_subcalls]
    assert chunk_ids == [
        "large.py:1-2",
        "large.py:3-4",
        "large.py:5-5",
    ]
    assert all(subcall.depth == 1 for subcall in atomic_execution.chunk_subcalls)
    assert all(
        subcall.parent_call_id == atomic_execution.hermes_subcall.call_id
        for subcall in atomic_execution.chunk_subcalls
    )

    assert len(hermes_runtime.calls) == 4
    chunk_prompts = [json.loads(str(call["prompt"])) for call in hermes_runtime.calls[:3]]
    synthesis_prompt = json.loads(str(hermes_runtime.calls[-1]["prompt"]))
    prompt_subcall_ids = [
        _assert_rlm_subcall_id(prompt_envelope["trace"]["subcall_id"])
        for prompt_envelope in (*chunk_prompts, synthesis_prompt)
    ]
    assert len(set(prompt_subcall_ids)) == 4
    assert [subcall.subcall_id for subcall in atomic_execution.chunk_subcalls] == (
        prompt_subcall_ids[:3]
    )
    assert atomic_execution.hermes_subcall.subcall_id == prompt_subcall_ids[-1]

    for index, prompt_envelope in enumerate(chunk_prompts, start=1):
        assert prompt_envelope["mode"] == "execute_atomic"
        assert prompt_envelope["rlm_node"]["depth"] == 1
        assert prompt_envelope["ac_node"]["depth"] == 1
        assert prompt_envelope["call_context"] == {
            "call_id": f"rlm_call_atomic_chunk_{index:03d}",
            "parent_call_id": "rlm_call_atomic_synthesis",
            "depth": 1,
        }
        parent_execution_context = prompt_envelope["parent_execution_context"]
        prior_indexes = range(1, index)
        assert parent_execution_context == prompt_envelope["context"]["parent_execution_context"]
        assert parent_execution_context["schema_version"] == (
            RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION
        )
        assert parent_execution_context["generation_id"] == result.generation_id
        assert parent_execution_context["mode"] == "execute_atomic"
        assert parent_execution_context["parent_node_id"] == "rlm_node_root"
        assert parent_execution_context["parent_ac_node_id"] == "rlm_ac_root"
        assert parent_execution_context["parent_call_id"] == "rlm_call_atomic_synthesis"
        assert parent_execution_context["parent_trace_id"] == (
            "rlm_trace_rlm_call_atomic_synthesis"
        )
        assert parent_execution_context["current_node_id"] == (f"rlm_node_atomic_chunk_{index:03d}")
        assert parent_execution_context["current_ac_node_id"] == (
            f"rlm_ac_atomic_chunk_{index:03d}"
        )
        assert parent_execution_context["current_call_id"] == (f"rlm_call_atomic_chunk_{index:03d}")
        assert parent_execution_context["current_trace_id"] == (
            f"rlm_trace_rlm_call_atomic_chunk_{index:03d}"
        )
        assert parent_execution_context["current_depth"] == 1
        assert parent_execution_context["child_order"] == index - 1
        assert parent_execution_context["sibling_count"] == 3
        assert parent_execution_context["prior_sibling_result_count"] == index - 1
        assert parent_execution_context["completed_sibling_count"] == index - 1
        assert parent_execution_context["failed_sibling_count"] == 0
        assert parent_execution_context["recorded_child_result_ids"] == [
            f"rlm_node_root:child_result:{prior_index - 1:03d}" for prior_index in prior_indexes
        ]
        assert parent_execution_context["recorded_child_node_ids"] == [
            f"rlm_node_atomic_chunk_{prior_index:03d}" for prior_index in range(1, index)
        ]
        assert parent_execution_context["recorded_child_ac_node_ids"] == [
            f"rlm_ac_atomic_chunk_{prior_index:03d}" for prior_index in range(1, index)
        ]
        assert parent_execution_context["recorded_child_call_ids"] == [
            f"rlm_call_atomic_chunk_{prior_index:03d}" for prior_index in range(1, index)
        ]
        assert parent_execution_context["recorded_child_chunk_ids"] == chunk_ids[: index - 1]
        assert parent_execution_context["synthesized_summary_present"] is False
        assert (
            prompt_envelope["trace"]["trace_id"] == f"rlm_trace_rlm_call_atomic_chunk_{index:03d}"
        )
        assert prompt_envelope["trace"]["parent_trace_id"] == (
            "rlm_trace_rlm_call_atomic_synthesis"
        )
        assert prompt_envelope["trace"]["causal_parent_event_id"] == ("rlm_call_atomic_synthesis")
        assert prompt_envelope["trace"]["parent_call_id"] == "rlm_call_atomic_synthesis"
        assert prompt_envelope["trace"]["depth"] == 1
        assert prompt_envelope["context"]["child_results"] == []
        assert len(prompt_envelope["context"]["chunks"]) == 1
        assert prompt_envelope["trace"]["selected_chunk_ids"] == [chunk_ids[index - 1]]

    assert synthesis_prompt["rlm_node"]["depth"] == 0
    assert synthesis_prompt["mode"] == RLM_HERMES_SYNTHESIZE_PARENT_MODE
    assert synthesis_prompt["ac_node"]["status"] == "synthesizing"
    assert synthesis_prompt["ac_node"]["depth"] == 0
    assert synthesis_prompt["call_context"] == {
        "call_id": "rlm_call_atomic_synthesis",
        "parent_call_id": None,
        "depth": 0,
    }
    assert synthesis_prompt["trace"]["call_id"] == "rlm_call_atomic_synthesis"
    assert synthesis_prompt["trace"]["trace_id"] == "rlm_trace_rlm_call_atomic_synthesis"
    assert synthesis_prompt["trace"]["parent_trace_id"] is None
    assert synthesis_prompt["trace"]["generated_child_ac_node_ids"] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
        "rlm_ac_atomic_chunk_003",
    ]
    assert synthesis_prompt["ac_node"]["child_ids"] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
        "rlm_ac_atomic_chunk_003",
    ]
    assert synthesis_prompt["trace"]["parent_call_id"] is None
    assert synthesis_prompt["trace"]["depth"] == 0
    assert synthesis_prompt["trace"]["selected_chunk_ids"] == chunk_ids
    synthesis_parent_context = synthesis_prompt["parent_execution_context"]
    assert synthesis_parent_context == synthesis_prompt["context"]["parent_execution_context"]
    assert synthesis_parent_context == {
        "schema_version": RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
        "generation_id": result.generation_id,
        "mode": RLM_HERMES_SYNTHESIZE_PARENT_MODE,
        "parent_node_id": "rlm_node_root",
        "parent_ac_node_id": "rlm_ac_root",
        "parent_call_id": None,
        "parent_trace_id": None,
        "current_node_id": "rlm_node_root",
        "current_ac_node_id": "rlm_ac_root",
        "current_call_id": "rlm_call_atomic_synthesis",
        "current_trace_id": "rlm_trace_rlm_call_atomic_synthesis",
        "current_depth": 0,
        "child_order": None,
        "sibling_count": 3,
        "prior_sibling_result_count": 3,
        "completed_sibling_count": 3,
        "failed_sibling_count": 0,
        "recorded_child_result_ids": [
            "rlm_node_root:child_result:000",
            "rlm_node_root:child_result:001",
            "rlm_node_root:child_result:002",
        ],
        "recorded_child_node_ids": [
            "rlm_node_atomic_chunk_001",
            "rlm_node_atomic_chunk_002",
            "rlm_node_atomic_chunk_003",
        ],
        "recorded_child_ac_node_ids": [
            "rlm_ac_atomic_chunk_001",
            "rlm_ac_atomic_chunk_002",
            "rlm_ac_atomic_chunk_003",
        ],
        "recorded_child_call_ids": [
            "rlm_call_atomic_chunk_001",
            "rlm_call_atomic_chunk_002",
            "rlm_call_atomic_chunk_003",
        ],
        "recorded_child_chunk_ids": chunk_ids,
        "synthesized_summary_present": True,
    }
    child_results = synthesis_prompt["context"]["child_results"]
    normalized_child_ac_inputs = synthesis_prompt["context"]["normalized_child_ac_inputs"]
    synthesized_subcall_summary = synthesis_prompt["context"]["synthesized_subcall_summary"]

    assert [child_result["chunk_id"] for child_result in child_results] == chunk_ids
    assert [child_result["order"] for child_result in child_results] == [0, 1, 2]
    assert [child_result["child_node_id"] for child_result in child_results] == [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
        "rlm_node_atomic_chunk_003",
    ]
    assert [child_result["subcall_id"] for child_result in child_results] == (
        prompt_subcall_ids[:3]
    )
    assert all(
        set(child_result)
        == {
            "order",
            "child_node_id",
            "child_ac_node_id",
            "call_id",
            "subcall_id",
            "chunk_id",
            "completion_status",
            "status_metadata",
            "question_payload",
            "result_payload",
        }
        for child_result in child_results
    )
    assert all(child_result["completion_status"] == "completed" for child_result in child_results)
    assert all(child_result["status_metadata"]["exit_code"] == 0 for child_result in child_results)
    assert all(child_result["status_metadata"]["depth"] == 1 for child_result in child_results)
    assert all("completion" in child_result["result_payload"] for child_result in child_results)

    assert [
        child_result["ordering"]["chunk_id"] for child_result in normalized_child_ac_inputs
    ] == chunk_ids
    assert all(
        set(child_result) == {"question", "result", "status", "ordering"}
        for child_result in normalized_child_ac_inputs
    )
    assert [
        child_result["question"]["selected_chunk_ids"]
        for child_result in normalized_child_ac_inputs
    ] == [[chunk_id] for chunk_id in chunk_ids]
    assert all(
        child_result["question"]["statement"].startswith("Execute one bounded atomic RLM step")
        for child_result in normalized_child_ac_inputs
    )

    parent_state = atomic_execution.parent_execution_state
    assert parent_state is not None
    assert parent_state.parent_node_id == "rlm_node_root"
    assert parent_state.parent_ac_node_id == "rlm_ac_root"
    assert parent_state.synthesized_summary is not None
    assert (
        parent_state.synthesized_summary.to_dict()
        == synthesis_prompt["context"]["parent_node_summary"]
    )
    assert parent_state.to_child_results_context() == child_results
    assert parent_state.to_child_ac_input_context() == normalized_child_ac_inputs
    assert (
        parent_state.to_parent_node_summary_context()
        == synthesis_prompt["context"]["parent_node_summary"]
    )
    assert parent_state.to_synthesized_subcall_summary_context() == synthesized_subcall_summary
    assert synthesis_prompt["context"]["parent_result"] == synthesized_subcall_summary
    assert synthesized_subcall_summary["schema_version"] == (
        RLM_SYNTHESIZED_SUBCALL_SUMMARY_SCHEMA_VERSION
    )
    assert synthesized_subcall_summary["parent_node_id"] == "rlm_node_root"
    assert synthesized_subcall_summary["parent_ac_node_id"] == "rlm_ac_root"
    assert synthesized_subcall_summary["summary"] == (
        "3 child sub-call(s) recorded for parent synthesis: 3 completed, 0 failed."
    )
    assert (
        synthesized_subcall_summary["parent_node_summary"]
        == synthesis_prompt["context"]["parent_node_summary"]
    )
    assert [
        child_summary["child_result_id"]
        for child_summary in synthesized_subcall_summary["child_result_summaries"]
    ] == [
        "rlm_node_root:child_result:000",
        "rlm_node_root:child_result:001",
        "rlm_node_root:child_result:002",
    ]
    assert [
        child_summary["reported_summary"]
        for child_summary in synthesized_subcall_summary["child_result_summaries"]
    ] == ["atomic execution complete", "atomic execution complete", "atomic execution complete"]
    assert synthesis_prompt["context"]["parent_node_summary"] == {
        "schema_version": RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION,
        "parent_node_id": "rlm_node_root",
        "parent_ac_node_id": "rlm_ac_root",
        "generation_id": "rlm_generation_0",
        "child_result_count": 3,
        "completed_child_count": 3,
        "failed_child_count": 0,
        "child_result_ids": [
            "rlm_node_root:child_result:000",
            "rlm_node_root:child_result:001",
            "rlm_node_root:child_result:002",
        ],
        "child_node_ids": [
            "rlm_node_atomic_chunk_001",
            "rlm_node_atomic_chunk_002",
            "rlm_node_atomic_chunk_003",
        ],
        "child_ac_node_ids": [
            "rlm_ac_atomic_chunk_001",
            "rlm_ac_atomic_chunk_002",
            "rlm_ac_atomic_chunk_003",
        ],
        "child_call_ids": [
            "rlm_call_atomic_chunk_001",
            "rlm_call_atomic_chunk_002",
            "rlm_call_atomic_chunk_003",
        ],
        "child_chunk_ids": chunk_ids,
        "child_completion_statuses": ["completed", "completed", "completed"],
    }


@pytest.mark.asyncio
async def test_atomic_execution_preserves_child_parent_links_for_synthesis(
    tmp_path: Path,
) -> None:
    """Chunk child results keep parent call IDs in traces and synthesis input."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def first(): return 1",
                "def second(): return 2",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="large.py",
            cwd=tmp_path,
            chunk_line_limit=1,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.atomic_execution is not None
    atomic_execution = result.atomic_execution
    parent_call_id = atomic_execution.hermes_subcall.call_id
    assert parent_call_id == "rlm_call_atomic_synthesis"
    assert atomic_execution.hermes_subcall.parent_call_id is None
    assert atomic_execution.hermes_subcall.to_trace_record().parent_call_id is None

    child_trace_records = [
        chunk_subcall.to_trace_record() for chunk_subcall in atomic_execution.chunk_subcalls
    ]
    assert [record.call_id for record in child_trace_records] == [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
    ]
    assert [record.parent_call_id for record in child_trace_records] == [
        parent_call_id,
        parent_call_id,
    ]
    assert [record.rlm_node_id for record in child_trace_records] == [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
    ]
    assert [record.ac_node_id for record in child_trace_records] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]
    assert [record.depth for record in child_trace_records] == [1, 1]

    synthesis_prompt = json.loads(atomic_execution.hermes_subcall.prompt)
    child_results = synthesis_prompt["context"]["child_results"]
    normalized_child_ac_inputs = synthesis_prompt["context"]["normalized_child_ac_inputs"]
    assert [result_item["status_metadata"]["parent_call_id"] for result_item in child_results] == [
        parent_call_id,
        parent_call_id,
    ]
    assert [item["ordering"]["parent_call_id"] for item in normalized_child_ac_inputs] == [
        parent_call_id,
        parent_call_id,
    ]
    assert [item["ordering"]["child_node_id"] for item in normalized_child_ac_inputs] == [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
    ]
    assert [item["ordering"]["child_ac_node_id"] for item in normalized_child_ac_inputs] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]


@pytest.mark.asyncio
async def test_root_and_nested_hermes_calls_preserve_parent_call_id_and_depth(
    tmp_path: Path,
) -> None:
    """Root and nested Hermes calls agree on parent linkage across trace surfaces."""
    target = tmp_path / "large.py"
    target.write_text("VALUE_1 = 1\nVALUE_2 = 2\n", encoding="utf-8")
    hermes_runtime = _FakeHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-call-depth.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    assert result.atomic_execution is not None
    root_subcall = result.atomic_execution.hermes_subcall
    nested_subcalls = result.atomic_execution.chunk_subcalls
    expected_call_links = {
        "rlm_call_atomic_synthesis": (None, 0),
        "rlm_call_atomic_chunk_001": ("rlm_call_atomic_synthesis", 1),
        "rlm_call_atomic_chunk_002": ("rlm_call_atomic_synthesis", 1),
    }

    subcalls_by_id = {subcall.call_id: subcall for subcall in (root_subcall, *nested_subcalls)}
    prompts_by_id = {
        json.loads(str(call["prompt"]))["call_context"]["call_id"]: json.loads(str(call["prompt"]))
        for call in hermes_runtime.calls
    }
    records_by_id = {record.call_id: record for record in records}
    events_by_id = {event.data["hermes"]["call_id"]: event for event in events}

    assert set(subcalls_by_id) == set(expected_call_links)
    assert set(prompts_by_id) == set(expected_call_links)
    assert set(records_by_id) == set(expected_call_links)
    assert set(events_by_id) == set(expected_call_links)

    for call_id, (parent_call_id, depth) in expected_call_links.items():
        subcall = subcalls_by_id[call_id]
        prompt = prompts_by_id[call_id]
        record = records_by_id[call_id]
        event = events_by_id[call_id]

        assert subcall.parent_call_id == parent_call_id
        assert subcall.depth == depth
        assert prompt["call_context"] == {
            "call_id": call_id,
            "parent_call_id": parent_call_id,
            "depth": depth,
        }
        assert prompt["trace"]["parent_call_id"] == parent_call_id
        assert prompt["trace"]["depth"] == depth
        assert _assert_rlm_subcall_id(prompt["trace"]["subcall_id"]) == subcall.subcall_id
        assert record.subcall_id == subcall.subcall_id
        assert event.data["trace"]["subcall_id"] == subcall.subcall_id
        assert record.parent_call_id == parent_call_id
        assert record.depth == depth
        assert event.data["trace"]["parent_call_id"] == parent_call_id
        assert event.data["trace"]["depth"] == depth
        assert event.data["recursion"]["parent_call_id"] == parent_call_id
        assert event.data["recursion"]["depth"] == depth


@pytest.mark.asyncio
async def test_subcall_trace_records_include_unique_ids_and_parent_execution_context(
    tmp_path: Path,
) -> None:
    """Persisted sub-call traces carry stable IDs and parent execution context."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "VALUE_1 = 1",
                "VALUE_2 = 2",
                "VALUE_3 = 3",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-context-trace.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    expected_chunk_ids = ["large.py:1-1", "large.py:2-2", "large.py:3-3"]
    expected_call_ids = [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_chunk_003",
        "rlm_call_atomic_synthesis",
    ]

    assert result.hermes_subcall_count == len(expected_call_ids)
    assert len(events) == len(expected_call_ids) * 2
    assert len(records) == len(expected_call_ids) * 2
    assert len({event.id for event in events}) == len(events)

    records_by_call_id: dict[str, list[RLMHermesTraceRecord]] = {}
    for record in records:
        assert record.call_id is not None
        records_by_call_id.setdefault(record.call_id, []).append(record)

    assert set(records_by_call_id) == set(expected_call_ids)

    subcall_ids_by_call: dict[str, str] = {}
    trace_ids_by_call: dict[str, str] = {}
    for call_id, call_records in records_by_call_id.items():
        assert len(call_records) == 2
        assert {record.success for record in call_records} in ({None, True}, {False, None})

        subcall_ids = {record.subcall_id for record in call_records}
        trace_ids = {record.trace_id for record in call_records}
        assert len(subcall_ids) == 1
        assert len(trace_ids) == 1

        subcall_id = _assert_rlm_subcall_id(next(iter(subcall_ids)))
        trace_id = next(iter(trace_ids))
        assert isinstance(trace_id, str)
        assert trace_id == f"rlm_trace_{call_id}"

        subcall_ids_by_call[call_id] = subcall_id
        trace_ids_by_call[call_id] = trace_id

    assert len(set(subcall_ids_by_call.values())) == len(expected_call_ids)
    assert len(set(trace_ids_by_call.values())) == len(expected_call_ids)

    expected_contexts: dict[str, dict[str, Any]] = {}
    for index, _chunk_id in enumerate(expected_chunk_ids, start=1):
        prior_indexes = range(1, index)
        call_id = f"rlm_call_atomic_chunk_{index:03d}"
        expected_contexts[call_id] = {
            "schema_version": RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
            "generation_id": result.generation_id,
            "mode": "execute_atomic",
            "parent_node_id": "rlm_node_root",
            "parent_ac_node_id": "rlm_ac_root",
            "parent_call_id": "rlm_call_atomic_synthesis",
            "parent_trace_id": "rlm_trace_rlm_call_atomic_synthesis",
            "current_node_id": f"rlm_node_atomic_chunk_{index:03d}",
            "current_ac_node_id": f"rlm_ac_atomic_chunk_{index:03d}",
            "current_call_id": call_id,
            "current_trace_id": f"rlm_trace_{call_id}",
            "current_depth": 1,
            "child_order": index - 1,
            "sibling_count": 3,
            "prior_sibling_result_count": index - 1,
            "completed_sibling_count": index - 1,
            "failed_sibling_count": 0,
            "recorded_child_result_ids": [
                f"rlm_node_root:child_result:{prior_index - 1:03d}" for prior_index in prior_indexes
            ],
            "recorded_child_node_ids": [
                f"rlm_node_atomic_chunk_{prior_index:03d}" for prior_index in prior_indexes
            ],
            "recorded_child_ac_node_ids": [
                f"rlm_ac_atomic_chunk_{prior_index:03d}" for prior_index in prior_indexes
            ],
            "recorded_child_call_ids": [
                f"rlm_call_atomic_chunk_{prior_index:03d}" for prior_index in prior_indexes
            ],
            "recorded_child_chunk_ids": expected_chunk_ids[: index - 1],
            "synthesized_summary_present": False,
        }

    expected_contexts["rlm_call_atomic_synthesis"] = {
        "schema_version": RLM_PARENT_EXECUTION_CONTEXT_SCHEMA_VERSION,
        "generation_id": result.generation_id,
        "mode": RLM_HERMES_SYNTHESIZE_PARENT_MODE,
        "parent_node_id": "rlm_node_root",
        "parent_ac_node_id": "rlm_ac_root",
        "parent_call_id": None,
        "parent_trace_id": None,
        "current_node_id": "rlm_node_root",
        "current_ac_node_id": "rlm_ac_root",
        "current_call_id": "rlm_call_atomic_synthesis",
        "current_trace_id": "rlm_trace_rlm_call_atomic_synthesis",
        "current_depth": 0,
        "child_order": None,
        "sibling_count": 3,
        "prior_sibling_result_count": 3,
        "completed_sibling_count": 3,
        "failed_sibling_count": 0,
        "recorded_child_result_ids": [
            "rlm_node_root:child_result:000",
            "rlm_node_root:child_result:001",
            "rlm_node_root:child_result:002",
        ],
        "recorded_child_node_ids": [
            "rlm_node_atomic_chunk_001",
            "rlm_node_atomic_chunk_002",
            "rlm_node_atomic_chunk_003",
        ],
        "recorded_child_ac_node_ids": [
            "rlm_ac_atomic_chunk_001",
            "rlm_ac_atomic_chunk_002",
            "rlm_ac_atomic_chunk_003",
        ],
        "recorded_child_call_ids": [
            "rlm_call_atomic_chunk_001",
            "rlm_call_atomic_chunk_002",
            "rlm_call_atomic_chunk_003",
        ],
        "recorded_child_chunk_ids": expected_chunk_ids,
        "synthesized_summary_present": True,
    }

    for call_id, call_records in records_by_call_id.items():
        for record in call_records:
            prompt_envelope = json.loads(record.prompt)
            parent_execution_context = prompt_envelope["parent_execution_context"]
            assert (
                parent_execution_context == prompt_envelope["context"]["parent_execution_context"]
            )
            assert parent_execution_context == expected_contexts[call_id]
            assert record.trace_id == parent_execution_context["current_trace_id"]
            assert record.subcall_id == prompt_envelope["trace"]["subcall_id"]
            assert record.call_id == parent_execution_context["current_call_id"]
            assert record.parent_call_id == parent_execution_context["parent_call_id"]
            assert record.parent_trace_id == parent_execution_context["parent_trace_id"]
            assert record.depth == parent_execution_context["current_depth"]

    for event in events:
        prompt_envelope = json.loads(event.data["hermes"]["prompt"])
        call_id = event.data["hermes"]["call_id"]
        assert event.data["trace"]["trace_id"] == trace_ids_by_call[call_id]
        assert event.data["trace"]["subcall_id"] == subcall_ids_by_call[call_id]
        assert prompt_envelope["parent_execution_context"] == expected_contexts[call_id]


@pytest.mark.asyncio
async def test_rlm_loop_emits_started_and_succeeded_trace_records_per_hermes_call(
    tmp_path: Path,
) -> None:
    """RLM tracing persists start and success records for each Hermes sub-call."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def first(): return 1",
                "def second(): return 2",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-loop-trace.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    assert result.hermes_subcall_count == 3
    assert len(hermes_runtime.calls) == result.hermes_subcall_count
    assert len(events) == result.hermes_subcall_count * 2
    assert len(records) == result.hermes_subcall_count * 2
    assert [event.type for event in events] == [
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
    ]

    calls_by_id = {
        json.loads(str(call["prompt"]))["trace"]["call_id"]: str(call["prompt"])
        for call in hermes_runtime.calls
    }
    started_records_by_id = {record.call_id: record for record in records if record.success is None}
    records_by_id = {record.call_id: record for record in records if record.success is True}

    assert set(records_by_id) == {
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_synthesis",
    }
    assert set(started_records_by_id) == set(records_by_id)
    assert len({record.subcall_id for record in records}) == result.hermes_subcall_count
    for call_id, prompt in calls_by_id.items():
        started_record = started_records_by_id[call_id]
        record = records_by_id[call_id]
        assert started_record.prompt == prompt
        assert started_record.completion == ""
        assert started_record.response_hash is None
        assert started_record.elapsed_ms is None
        assert record.prompt == prompt
        assert record.completion
        assert record.response_hash is not None
        assert record.prompt_hash is not None
        assert record.elapsed_ms is not None
        assert (
            _assert_rlm_subcall_id(record.subcall_id) == json.loads(prompt)["trace"]["subcall_id"]
        )

    first_chunk = records_by_id["rlm_call_atomic_chunk_001"]
    second_chunk = records_by_id["rlm_call_atomic_chunk_002"]
    synthesis = records_by_id["rlm_call_atomic_synthesis"]
    events_by_call_id = {
        event.data["hermes"]["call_id"]: event
        for event in events
        if event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
    }
    started_events_by_call_id = {
        event.data["hermes"]["call_id"]: event
        for event in events
        if event.type == RLM_HERMES_CALL_STARTED_EVENT
    }
    assert set(started_events_by_call_id) == set(events_by_call_id)
    assert all(
        event.data["lifecycle"]["status"] == "started"
        for event in started_events_by_call_id.values()
    )
    assert all(
        event.data["lifecycle"]["status"] == "succeeded" for event in events_by_call_id.values()
    )
    assert first_chunk.selected_chunk_ids == ("large.py:1-1",)
    assert second_chunk.selected_chunk_ids == ("large.py:2-2",)
    assert synthesis.selected_chunk_ids == ("large.py:1-1", "large.py:2-2")
    assert first_chunk.trace_id == "rlm_trace_rlm_call_atomic_chunk_001"
    assert first_chunk.parent_trace_id == "rlm_trace_rlm_call_atomic_synthesis"
    assert first_chunk.causal_parent_event_id == "rlm_call_atomic_synthesis"
    assert second_chunk.trace_id == "rlm_trace_rlm_call_atomic_chunk_002"
    assert second_chunk.parent_trace_id == "rlm_trace_rlm_call_atomic_synthesis"
    assert second_chunk.causal_parent_event_id == "rlm_call_atomic_synthesis"
    assert synthesis.trace_id == "rlm_trace_rlm_call_atomic_synthesis"
    assert synthesis.parent_trace_id is None
    assert synthesis.causal_parent_event_id is None
    assert synthesis.generated_child_ac_node_ids == (
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    )
    assert first_chunk.parent_call_id == synthesis.call_id
    assert second_chunk.parent_call_id == synthesis.call_id
    assert synthesis.parent_call_id is None

    first_chunk_event = events_by_call_id["rlm_call_atomic_chunk_001"]
    synthesis_event = events_by_call_id["rlm_call_atomic_synthesis"]
    assert first_chunk_event.data["trace"]["parent_call_id"] == ("rlm_call_atomic_synthesis")
    assert first_chunk_event.data["trace"]["depth"] == 1
    assert first_chunk_event.data["trace"]["parent_trace_id"] == (
        "rlm_trace_rlm_call_atomic_synthesis"
    )
    assert first_chunk_event.data["trace"]["causal_parent_event_id"] == (
        "rlm_call_atomic_synthesis"
    )
    assert synthesis_event.data["ac_node"]["child_ids"] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]
    assert synthesis_event.data["trace"]["parent_call_id"] is None
    assert synthesis_event.data["trace"]["depth"] == 0
    assert synthesis_event.data["recursion"]["generated_child_ac_node_ids"] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]
    assert synthesis_event.data["replay"]["creates_ac_node_ids"] == [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
    ]


@pytest.mark.asyncio
async def test_rlm_loop_trace_records_match_each_hermes_prompt_and_completion(
    tmp_path: Path,
) -> None:
    """Each Hermes sub-call trace preserves its own prompt and completion."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def first(): return 1",
                "def second(): return 2",
            ]
        ),
        encoding="utf-8",
    )
    final_messages_by_call_id = {
        "rlm_call_atomic_chunk_001": json.dumps(
            {
                "schema_version": "rlm.hermes.output.v1",
                "mode": "execute_atomic",
                "verdict": "passed",
                "confidence": 0.91,
                "result": {"summary": "first chunk completion"},
                "evidence_references": ["large.py:1-1"],
                "residual_gaps": [],
            }
        ),
        "rlm_call_atomic_chunk_002": json.dumps(
            {
                "schema_version": "rlm.hermes.output.v1",
                "mode": "execute_atomic",
                "verdict": "passed",
                "confidence": 0.92,
                "result": {"summary": "second chunk completion"},
                "evidence_references": ["large.py:2-2"],
                "residual_gaps": [],
            }
        ),
        "rlm_call_atomic_synthesis": json.dumps(
            {
                "schema_version": "rlm.hermes.output.v1",
                "mode": "synthesize_parent",
                "verdict": "passed",
                "confidence": 0.95,
                "result": {"summary": "synthesis completion"},
                "evidence_references": ["large.py:1-2"],
                "residual_gaps": [],
            }
        ),
    }
    hermes_runtime = _FakeHermesRuntime(
        [
            TaskResult(success=True, final_message=final_message, messages=())
            for final_message in final_messages_by_call_id.values()
        ]
    )
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-loop-match-trace.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    prompts_by_call_id = {
        json.loads(str(call["prompt"]))["trace"]["call_id"]: str(call["prompt"])
        for call in hermes_runtime.calls
    }
    events_by_call_id = {
        event.data["hermes"]["call_id"]: event
        for event in events
        if event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
        and isinstance(event.data.get("hermes"), dict)
    }
    started_events_by_call_id = {
        event.data["hermes"]["call_id"]: event
        for event in events
        if event.type == RLM_HERMES_CALL_STARTED_EVENT
        and isinstance(event.data.get("hermes"), dict)
    }
    records_by_call_id = {record.call_id: record for record in records if record.success is True}
    started_records_by_call_id = {
        record.call_id: record for record in records if record.success is None
    }

    assert result.hermes_subcall_count == 3
    assert len(events) == 6
    assert len(records) == 6
    assert (
        set(events_by_call_id)
        == set(started_events_by_call_id)
        == set(records_by_call_id)
        == set(started_records_by_call_id)
        == set(prompts_by_call_id)
        == set(final_messages_by_call_id)
    )

    for call_id, expected_prompt in prompts_by_call_id.items():
        started_event = started_events_by_call_id[call_id]
        event = events_by_call_id[call_id]
        started_record = started_records_by_call_id[call_id]
        record = records_by_call_id[call_id]
        assert started_event.type == RLM_HERMES_CALL_STARTED_EVENT
        assert started_event.data["hermes"]["prompt"] == expected_prompt
        assert started_event.data["hermes"]["completion"] == ""
        assert started_event.data["lifecycle"]["status"] == "started"
        assert started_record.prompt == expected_prompt
        assert started_record.completion == ""
        assert started_record.success is None
        assert event.type == RLM_HERMES_CALL_SUCCEEDED_EVENT
        assert event.data["lifecycle"]["status"] == "succeeded"
        assert event.data["hermes"]["call_id"] == call_id
        assert event.data["hermes"]["prompt"] == expected_prompt
        assert event.data["hermes"]["completion"] == final_messages_by_call_id[call_id]
        assert event.data["hermes"]["exit_code"] == 0
        assert event.data["atomic_ac_execution"]["input"] == expected_prompt
        assert event.data["atomic_ac_execution"]["output"] == final_messages_by_call_id[call_id]
        assert event.data["atomic_ac_execution"]["exit_code"] == 0
        assert event.data["atomic_ac_execution"]["ac_node_id"] == record.ac_node_id
        assert record.exit_code == 0
        assert record.prompt == expected_prompt
        assert record.completion == final_messages_by_call_id[call_id]


@pytest.mark.asyncio
async def test_chunk_child_ac_node_ids_stay_associated_across_parent_state_and_trace(
    tmp_path: Path,
) -> None:
    """Chunk AC IDs remain stable across child calls, parent inputs, and traces."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "VALUE_1 = 1",
                "VALUE_2 = 2",
                "VALUE_3 = 3",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-child-ac-ids.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="large.py",
                cwd=tmp_path,
                chunk_line_limit=1,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    assert result.atomic_execution is not None
    atomic_execution = result.atomic_execution
    parent_state = atomic_execution.parent_execution_state
    assert parent_state is not None

    expected_child_ac_ids = [
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
        "rlm_ac_atomic_chunk_003",
    ]
    expected_child_node_ids = [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
        "rlm_node_atomic_chunk_003",
    ]

    chunk_ac_ids = [subcall.ac_node_id for subcall in atomic_execution.chunk_subcalls]
    chunk_node_ids = [subcall.rlm_node_id for subcall in atomic_execution.chunk_subcalls]
    assert chunk_ac_ids == expected_child_ac_ids
    assert chunk_node_ids == expected_child_node_ids
    assert [
        result_item["child_ac_node_id"] for result_item in parent_state.to_child_results_context()
    ] == expected_child_ac_ids
    assert [
        result_item["question"]["ac_node_id"]
        for result_item in parent_state.to_child_ac_input_context()
    ] == expected_child_ac_ids
    parent_summary = parent_state.to_parent_node_summary_context()
    assert parent_summary["child_ac_node_ids"] == expected_child_ac_ids
    assert atomic_execution.hermes_subcall.generated_child_ac_node_ids == (
        tuple(expected_child_ac_ids)
    )

    records_by_call_id = {record.call_id: record for record in records}
    assert [
        records_by_call_id[f"rlm_call_atomic_chunk_{index:03d}"].ac_node_id for index in range(1, 4)
    ] == expected_child_ac_ids
    assert records_by_call_id["rlm_call_atomic_synthesis"].generated_child_ac_node_ids == tuple(
        expected_child_ac_ids
    )


def test_parent_execution_state_serializes_recorded_results_in_stable_order() -> None:
    """Parent synthesis state owns ordering and required child result fields."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=2,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                status_metadata={"exit_code": 1, "mode": "execute_atomic"},
                result_payload={"summary": "second result"},
            ),
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                status_metadata={"exit_code": 0, "mode": "execute_atomic"},
                result_payload={"summary": "first result"},
            ),
        ),
    )

    child_results = state.to_child_results_context()

    assert [result["order"] for result in child_results] == [1, 2]
    assert child_results[0]["child_node_id"] == "rlm_child_1"
    assert child_results[0]["question_payload"]["ac_node_id"] == "ac_child_1"
    assert child_results[0]["completion_status"] == "completed"
    assert child_results[0]["status_metadata"] == {
        "exit_code": 0,
        "mode": "execute_atomic",
    }
    assert child_results[0]["result_payload"] == {"summary": "first result"}

    normalized_input = state.to_child_ac_input_context()[0]
    assert normalized_input["ordering"]["order"] == 1
    assert normalized_input["ordering"]["child_node_id"] == "rlm_child_1"
    assert normalized_input["status"]["completion_status"] == "completed"
    assert normalized_input["result"] == {"summary": "first result"}


def test_parent_execution_state_records_hermes_subcall_status_and_payload() -> None:
    """Recorded sub-call results preserve child IDs, status, and opaque payload."""
    subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_child",
        ac_node_id="ac_child",
        prompt="{}",
        completion='{"result":"done"}',
        parent_call_id="rlm_parent_call",
        depth=1,
        exit_code=1,
        call_id="rlm_child_call",
        chunk_id="chunk_1",
    )

    state = RLMParentExecutionState.from_hermes_subcalls(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        subcalls=[subcall],
    )

    child_result = state.to_child_results_context()[0]
    assert child_result["order"] == 0
    assert child_result["child_node_id"] == "rlm_child"
    assert child_result["child_ac_node_id"] == "ac_child"
    assert child_result["question_payload"]["selected_chunk_ids"] == ["chunk_1"]
    assert child_result["completion_status"] == "failed"
    assert child_result["status_metadata"] == {
        "mode": "execute_atomic",
        "generation_id": "rlm_generation_0",
        "parent_call_id": "rlm_parent_call",
        "subcall_id": None,
        "depth": 1,
        "exit_code": 1,
        "resume_handle_present": False,
    }
    assert child_result["result_payload"] == {
        "exit_code": 1,
        "completion": '{"result":"done"}',
        "reported_result": "done",
        "verdict": None,
        "confidence": None,
        "evidence_references": [],
        "residual_gaps": [],
    }

    normalized_input = state.to_child_ac_input_context()[0]
    assert normalized_input["ordering"]["order"] == 0
    assert normalized_input["ordering"]["child_node_id"] == "rlm_child"
    assert normalized_input["status"]["completion_status"] == "failed"
    assert normalized_input["result"]["completion"] == '{"result":"done"}'


def test_parent_node_summary_schema_is_separate_from_raw_child_outputs() -> None:
    """Parent summary exposes rollup metadata without raw child payload records."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                status_metadata={"exit_code": 1},
                result_payload={"completion": "raw child output two"},
            ),
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                status_metadata={"exit_code": 0},
                result_payload={"completion": "raw child output one"},
            ),
        ),
    )

    summary = state.to_parent_node_summary_context()
    raw_child_records = state.to_child_results_context()

    assert summary["schema_version"] == RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION
    assert summary["parent_node_id"] == "rlm_parent"
    assert summary["child_result_count"] == 2
    assert summary["completed_child_count"] == 1
    assert summary["failed_child_count"] == 1
    assert summary["child_result_ids"] == [
        "rlm_parent:child_result:000",
        "rlm_parent:child_result:001",
    ]
    assert summary["child_completion_statuses"] == ["completed", "failed"]
    assert "result_payload" not in summary
    assert "status_metadata" not in summary
    assert "raw child output one" not in json.dumps(summary)
    assert raw_child_records[0]["result_payload"]["completion"] == "raw child output one"
    assert raw_child_records[1]["result_payload"]["completion"] == "raw child output two"


def test_parent_node_summary_is_structured_derived_and_not_raw_child_output() -> None:
    """Parent summaries are schema payloads derived from children, not child copies."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=2,
                child_node_id="rlm_child_3",
                child_ac_node_id="ac_child_3",
                call_id="call_3",
                chunk_id="chunk_3",
                completion_status="completed",
                status_metadata={"exit_code": 0, "notes": "raw status metadata three"},
                question_payload={"statement": "raw question payload three"},
                result_payload={
                    "completion": "raw child output three",
                    "reported_result": {"summary": "third raw report"},
                },
            ),
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                status_metadata={"exit_code": 0, "notes": "raw status metadata one"},
                question_payload={"statement": "raw question payload one"},
                result_payload={
                    "completion": "raw child output one",
                    "reported_result": {"summary": "first raw report"},
                },
            ),
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                status_metadata={"exit_code": 1, "notes": "raw status metadata two"},
                question_payload={"statement": "raw question payload two"},
                result_payload={
                    "completion": "raw child output two",
                    "reported_result": {"summary": "second raw report"},
                },
            ),
        ),
    )

    summary = synthesize_parent_node_summary(state)
    summary_payload = summary.to_dict()
    raw_child_records = state.to_child_results_context()

    assert isinstance(summary, RLMParentNodeSummary)
    assert set(summary_payload) == {
        "schema_version",
        "parent_node_id",
        "parent_ac_node_id",
        "generation_id",
        "child_result_count",
        "completed_child_count",
        "failed_child_count",
        "child_result_ids",
        "child_node_ids",
        "child_ac_node_ids",
        "child_call_ids",
        "child_chunk_ids",
        "child_completion_statuses",
    }
    assert summary_payload == {
        "schema_version": RLM_PARENT_NODE_SUMMARY_SCHEMA_VERSION,
        "parent_node_id": "rlm_parent",
        "parent_ac_node_id": "ac_parent",
        "generation_id": "rlm_generation_0",
        "child_result_count": 3,
        "completed_child_count": 2,
        "failed_child_count": 1,
        "child_result_ids": [
            "rlm_parent:child_result:000",
            "rlm_parent:child_result:001",
            "rlm_parent:child_result:002",
        ],
        "child_node_ids": ["rlm_child_1", "rlm_child_2", "rlm_child_3"],
        "child_ac_node_ids": ["ac_child_1", "ac_child_2", "ac_child_3"],
        "child_call_ids": ["call_1", "call_2", "call_3"],
        "child_chunk_ids": ["chunk_1", "chunk_2", "chunk_3"],
        "child_completion_statuses": ["completed", "failed", "completed"],
    }
    assert RLMParentNodeSummary.from_dict(summary_payload) == summary

    raw_child_json = json.dumps(raw_child_records, sort_keys=True)
    summary_json = json.dumps(summary_payload, sort_keys=True)
    assert "raw child output one" in raw_child_json
    assert "raw child output two" in raw_child_json
    assert "raw child output three" in raw_child_json
    assert "raw question payload" in raw_child_json
    assert "raw status metadata" in raw_child_json
    assert "raw child output" not in summary_json
    assert "raw question payload" not in summary_json
    assert "raw status metadata" not in summary_json
    assert summary_payload != raw_child_records


def test_parent_state_rejects_summary_not_derived_from_child_results() -> None:
    """A stored parent summary must match the currently attached child records."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                result_payload={"completion": "raw child output one"},
            ),
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                result_payload={"completion": "raw child output two"},
            ),
        ),
    )
    tampered_summary_payload = state.to_parent_node_summary_context()
    tampered_summary_payload["child_node_ids"] = ["rlm_child_1", "unrelated_child"]
    tampered_summary = RLMParentNodeSummary.from_dict(tampered_summary_payload)

    with pytest.raises(ValueError, match="must match recorded child results"):
        RLMParentExecutionState(
            parent_node_id=state.parent_node_id,
            parent_ac_node_id=state.parent_ac_node_id,
            generation_id=state.generation_id,
            recorded_subcall_results=state.recorded_subcall_results,
            synthesized_summary=tampered_summary,
        )


def test_parent_state_stores_synthesized_summary_without_rewriting_child_outputs() -> None:
    """Attaching the parent summary leaves raw child output records unchanged."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                status_metadata={"exit_code": 0},
                result_payload={"completion": "raw child output one"},
            ),
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                status_metadata={"exit_code": 1},
                result_payload={"completion": "raw child output two"},
            ),
        ),
    )
    raw_child_records_before = state.to_child_results_context()
    raw_child_records_json_before = json.dumps(raw_child_records_before, sort_keys=True)
    summary = synthesize_parent_node_summary(state)

    parent_state = state.with_synthesized_summary(summary)

    assert parent_state.synthesized_summary == summary
    assert parent_state.to_parent_node_summary() == summary
    assert parent_state.recorded_subcall_results == state.recorded_subcall_results
    assert parent_state.to_child_results_context() == raw_child_records_before
    assert (
        json.dumps(parent_state.to_child_results_context(), sort_keys=True)
        == raw_child_records_json_before
    )
    assert parent_state.to_child_results_context()[0]["result_payload"]["completion"] == (
        "raw child output one"
    )
    assert parent_state.to_child_results_context()[1]["result_payload"]["completion"] == (
        "raw child output two"
    )


def test_parent_node_summary_round_trips_schema_payload() -> None:
    """Parent summary payloads can be validated from the documented schema."""
    summary = RLMParentNodeSummary(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        child_result_count=1,
        completed_child_count=1,
        failed_child_count=0,
        child_result_ids=("rlm_parent:child_result:000",),
        child_node_ids=("rlm_child_1",),
        child_ac_node_ids=("ac_child_1",),
        child_call_ids=("call_1",),
        child_chunk_ids=("chunk_1",),
        child_completion_statuses=("completed",),
    )

    assert RLMParentNodeSummary.from_dict(summary.to_dict()) == summary


def test_parent_synthesis_helper_consumes_attached_results_for_summary() -> None:
    """The synthesis helper rolls attached child records into the parent schema."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                completion_status="failed",
                result_payload={"completion": "second failed"},
            ),
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                completion_status="completed",
                result_payload={"completion": "first ok"},
            ),
        ),
    )

    summary = synthesize_parent_node_summary(state)

    assert summary.to_dict() == state.to_parent_node_summary_context()
    assert summary.child_result_count == 2
    assert summary.completed_child_count == 1
    assert summary.failed_child_count == 1
    assert summary.child_result_ids == (
        "rlm_parent:child_result:000",
        "rlm_parent:child_result:001",
    )
    assert summary.child_node_ids == ("rlm_child_1", "rlm_child_2")


def test_synthesized_subcall_summary_is_compact_parent_resume_context() -> None:
    """Parent resume input gets a compact child summary separate from raw records."""
    state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
        recorded_subcall_results=(
            RLMRecordedSubcallResult(
                order=1,
                child_node_id="rlm_child_2",
                child_ac_node_id="ac_child_2",
                call_id="call_2",
                chunk_id="chunk_2",
                completion_status="failed",
                status_metadata={"mode": "execute_atomic", "exit_code": 1},
                result_payload={
                    "completion": "",
                    "verdict": None,
                    "confidence": None,
                    "evidence_references": [],
                    "residual_gaps": [{"gap": "missing evidence"}],
                },
            ),
            RLMRecordedSubcallResult(
                order=0,
                child_node_id="rlm_child_1",
                child_ac_node_id="ac_child_1",
                call_id="call_1",
                chunk_id="chunk_1",
                completion_status="completed",
                status_metadata={"mode": "execute_atomic", "exit_code": 0},
                result_payload={
                    "completion": "raw child output one",
                    "reported_result": {"summary": "first child summary"},
                    "verdict": "passed",
                    "confidence": 0.8,
                    "evidence_references": [{"chunk_id": "chunk_1"}],
                    "residual_gaps": [],
                },
            ),
        ),
    ).with_synthesized_summary()

    summary = synthesize_subcall_summary(state)

    assert summary == state.to_synthesized_subcall_summary_context()
    assert summary["schema_version"] == RLM_SYNTHESIZED_SUBCALL_SUMMARY_SCHEMA_VERSION
    assert summary["summary"] == (
        "2 child sub-call(s) recorded for parent synthesis: 1 completed, 1 failed."
    )
    assert summary["parent_node_summary"] == state.to_parent_node_summary_context()
    assert [
        child_summary["child_result_id"] for child_summary in summary["child_result_summaries"]
    ] == ["rlm_parent:child_result:000", "rlm_parent:child_result:001"]
    assert [
        child_summary["reported_summary"] for child_summary in summary["child_result_summaries"]
    ] == [
        "first child summary",
        None,
    ]
    assert [
        child_summary["completion_status"] for child_summary in summary["child_result_summaries"]
    ] == [
        "completed",
        "failed",
    ]
    assert summary["child_result_summaries"][0]["evidence_reference_count"] == 1
    assert summary["child_result_summaries"][1]["residual_gap_count"] == 1
    assert "raw child output one" not in json.dumps(summary, sort_keys=True)
    assert state.to_dict()["synthesized_subcall_summary"] == summary


def test_parent_node_summary_rejects_inconsistent_counts() -> None:
    """The summary schema validates counts independently from child records."""
    with pytest.raises(ValueError, match="must equal child_result_count"):
        RLMParentNodeSummary(
            parent_node_id="rlm_parent",
            parent_ac_node_id="ac_parent",
            generation_id="rlm_generation_0",
            child_result_count=2,
            completed_child_count=1,
            failed_child_count=0,
            child_result_ids=("result_1", "result_2"),
            child_node_ids=("rlm_child_1", "rlm_child_2"),
            child_ac_node_ids=("ac_child_1", "ac_child_2"),
            child_completion_statuses=("completed", "failed"),
        )


def test_capture_attaches_child_records_to_parent_state_in_stable_order() -> None:
    """Captured children are stored by parent-owned order with status metadata."""
    parent_state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
    )
    later_subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_child_2",
        ac_node_id="ac_child_2",
        prompt="{}",
        completion="failed child",
        parent_call_id="rlm_parent_call",
        depth=1,
        exit_code=1,
        call_id="call_2",
    )
    earlier_subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="rlm_generation_0",
        rlm_node_id="rlm_child_1",
        ac_node_id="ac_child_1",
        prompt="{}",
        completion="completed child",
        parent_call_id="rlm_parent_call",
        depth=1,
        exit_code=0,
        call_id="call_1",
    )

    parent_state = capture_completed_hermes_subcall_result(
        parent_state,
        order=1,
        subcall=later_subcall,
    )
    parent_state = capture_completed_hermes_subcall_result(
        parent_state,
        order=0,
        subcall=earlier_subcall,
    )

    assert [result.order for result in parent_state.recorded_subcall_results] == [0, 1]
    assert [result.completion_status for result in parent_state.recorded_subcall_results] == [
        "completed",
        "failed",
    ]
    assert [
        result.status_metadata["exit_code"] for result in parent_state.recorded_subcall_results
    ] == [0, 1]


def test_capture_records_success_failure_and_multiple_children_on_parent_state() -> None:
    """Capture keeps parent identity and stores mixed child outcomes in order."""
    parent_state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
    )

    def _subcall(index: int, *, exit_code: int, completion: str) -> RLMHermesSubcall:
        return RLMHermesSubcall(
            mode="execute_atomic",
            generation_id="rlm_generation_0",
            rlm_node_id=f"rlm_child_{index}",
            ac_node_id=f"ac_child_{index}",
            prompt="{}",
            completion=completion,
            parent_call_id="rlm_parent_call",
            depth=1,
            exit_code=exit_code,
            call_id=f"call_{index}",
            chunk_id=f"chunk_{index}",
        )

    for order, subcall in (
        (2, _subcall(3, exit_code=0, completion="third ok")),
        (0, _subcall(1, exit_code=0, completion="first ok")),
        (1, _subcall(2, exit_code=1, completion="second failed")),
    ):
        parent_state = capture_completed_hermes_subcall_result(
            parent_state,
            order=order,
            subcall=subcall,
        )

    assert parent_state.parent_node_id == "rlm_parent"
    assert parent_state.parent_ac_node_id == "ac_parent"
    assert parent_state.generation_id == "rlm_generation_0"

    child_results = parent_state.to_child_results_context()
    assert [result["order"] for result in child_results] == [0, 1, 2]
    assert [result["child_node_id"] for result in child_results] == [
        "rlm_child_1",
        "rlm_child_2",
        "rlm_child_3",
    ]
    assert [result["completion_status"] for result in child_results] == [
        "completed",
        "failed",
        "completed",
    ]
    assert [result["call_id"] for result in child_results] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [result["chunk_id"] for result in child_results] == [
        "chunk_1",
        "chunk_2",
        "chunk_3",
    ]
    assert [result["status_metadata"]["parent_call_id"] for result in child_results] == [
        "rlm_parent_call",
        "rlm_parent_call",
        "rlm_parent_call",
    ]
    assert [result["status_metadata"]["exit_code"] for result in child_results] == [0, 1, 0]
    assert [result["result_payload"]["completion"] for result in child_results] == [
        "first ok",
        "second failed",
        "third ok",
    ]


@pytest.mark.asyncio
async def test_atomic_execution_captures_each_child_at_recursive_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chunk child results are captured as each Hermes call returns."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def one(): return 1",
                "def two(): return 2",
                "def three(): return 3",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime()

    def _fail_batch_capture(*_args: object, **_kwargs: object) -> None:
        pytest.fail("child Hermes results must be captured at the loop boundary")

    monkeypatch.setattr(
        rlm_loop.RLMParentExecutionState,
        "from_hermes_subcalls",
        classmethod(_fail_batch_capture),
    )

    result = await run_rlm_loop(
        RLMRunConfig(
            target="large.py",
            cwd=tmp_path,
            chunk_line_limit=1,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.atomic_execution is not None
    parent_state = result.atomic_execution.parent_execution_state
    assert parent_state is not None
    assert [
        child_result.child_node_id for child_result in parent_state.recorded_subcall_results
    ] == [
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
        "rlm_node_atomic_chunk_003",
    ]


@pytest.mark.asyncio
async def test_atomic_execution_captures_malformed_and_empty_child_responses_for_synthesis(
    tmp_path: Path,
) -> None:
    """Parent synthesis receives raw child completions even when Hermes output is bad."""
    target = tmp_path / "large.py"
    target.write_text(
        "\n".join(
            [
                "def first(): return 1",
                "def second(): return 2",
            ]
        ),
        encoding="utf-8",
    )
    hermes_runtime = _FakeHermesRuntime(
        responses=[
            TaskResult(success=True, final_message="{not-json", messages=()),
            TaskResult(success=False, final_message="", messages=()),
            TaskResult(
                success=True,
                final_message=json.dumps(
                    {
                        "schema_version": "rlm.hermes.output.v1",
                        "mode": "execute_atomic",
                        "verdict": "partial",
                        "confidence": 0.6,
                        "result": {"summary": "synthesis saw child outputs"},
                        "evidence_references": [],
                        "residual_gaps": [],
                    }
                ),
                messages=(),
            ),
        ]
    )

    result = await run_rlm_loop(
        RLMRunConfig(
            target="large.py",
            cwd=tmp_path,
            chunk_line_limit=1,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.status == "completed"
    assert result.hermes_subcall_count == 3
    assert result.atomic_execution is not None
    assert result.atomic_execution.success is False

    synthesis_prompt = json.loads(str(hermes_runtime.calls[-1]["prompt"]))
    child_results = synthesis_prompt["context"]["child_results"]
    assert [item["completion_status"] for item in child_results] == [
        "completed",
        "failed",
    ]
    assert [item["result_payload"]["completion"] for item in child_results] == [
        "{not-json",
        "",
    ]
    assert [item["status_metadata"]["exit_code"] for item in child_results] == [0, 1]


@pytest.mark.asyncio
async def test_single_atomic_execution_records_failed_empty_hermes_response(
    tmp_path: Path,
) -> None:
    """An empty failed Hermes response is preserved as the atomic sub-call result."""
    source = tmp_path / "example.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    hermes_runtime = _FakeHermesRuntime(
        responses=[TaskResult(success=False, final_message="", messages=())]
    )
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-failed-response.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        result = await run_rlm_loop(
            RLMRunConfig(
                target="example.py",
                cwd=tmp_path,
                hermes_runtime=hermes_runtime,
                trace_store=trace_store,
            )
        )
        events = await event_store.replay("rlm_run", result.generation_id)
        records = await trace_store.replay_hermes_subcalls(result.generation_id)
    finally:
        await event_store.close()

    assert result.status == "completed"
    assert result.hermes_subcall_count == 1
    assert result.atomic_execution is not None
    assert result.atomic_execution.success is False
    assert result.atomic_execution.final_message == ""
    assert result.atomic_execution.hermes_subcall.completion == ""
    assert result.atomic_execution.hermes_subcall.exit_code == 1
    assert [event.type for event in events] == [
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_FAILED_EVENT,
    ]
    assert [event.data["lifecycle"]["status"] for event in events] == [
        "started",
        "failed",
    ]
    assert len(records) == 2
    started_record, failed_record = records
    assert started_record.success is None
    assert started_record.completion == ""
    assert failed_record.success is False
    assert failed_record.exit_code == 1
    assert failed_record.completion == ""


@pytest.mark.asyncio
async def test_adapter_error_records_started_and_failed_hermes_trace_events(
    tmp_path: Path,
) -> None:
    """Adapter-level Hermes failures still leave replayable lifecycle traces."""
    source = tmp_path / "example.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    hermes_runtime = _FailingHermesRuntime()
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'rlm-adapter-failure.db'}")
    await event_store.initialize()
    trace_store = RLMTraceStore(event_store)

    try:
        with pytest.raises(ValueError, match="Hermes atomic execution sub-call failed"):
            await run_rlm_loop(
                RLMRunConfig(
                    target="example.py",
                    cwd=tmp_path,
                    hermes_runtime=hermes_runtime,
                    trace_store=trace_store,
                )
            )
        events = await event_store.replay("rlm_run", "rlm_generation_0")
        records = await trace_store.replay_hermes_subcalls("rlm_generation_0")
    finally:
        await event_store.close()

    assert len(hermes_runtime.calls) == 1
    assert [event.type for event in events] == [
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_FAILED_EVENT,
    ]
    assert [event.data["lifecycle"]["status"] for event in events] == [
        "started",
        "failed",
    ]
    assert len(records) == 2
    started_record, failed_record = records
    assert started_record.success is None
    assert started_record.prompt == str(hermes_runtime.calls[0]["prompt"])
    assert failed_record.success is False
    assert failed_record.exit_code == 1
    assert failed_record.completion == "adapter down"
    assert failed_record.adapter_error == {
        "provider": "hermes",
        "message": "adapter down",
        "details": {"error_type": "UnitTestFailure"},
    }


def test_capture_completed_hermes_subcall_result_rejects_generation_mismatch() -> None:
    """Boundary capture cannot attach a child result to the wrong parent run."""
    parent_state = RLMParentExecutionState(
        parent_node_id="rlm_parent",
        parent_ac_node_id="ac_parent",
        generation_id="rlm_generation_0",
    )
    subcall = RLMHermesSubcall(
        mode="execute_atomic",
        generation_id="other_generation",
        rlm_node_id="rlm_child",
        ac_node_id="ac_child",
        prompt="{}",
        completion='{"result":"done"}',
        parent_call_id="rlm_parent_call",
        depth=1,
        exit_code=0,
    )

    with pytest.raises(ValueError, match="generation_id must match"):
        capture_completed_hermes_subcall_result(
            parent_state,
            order=0,
            subcall=subcall,
        )


def test_parent_execution_state_rejects_duplicate_child_order() -> None:
    """Stable replay requires one unique order value per recorded child result."""
    with pytest.raises(ValueError, match="order values must be unique"):
        RLMParentExecutionState(
            parent_node_id="rlm_parent",
            parent_ac_node_id="ac_parent",
            generation_id="rlm_generation_0",
            recorded_subcall_results=(
                RLMRecordedSubcallResult(
                    order=0,
                    child_node_id="rlm_child_1",
                    child_ac_node_id="ac_child_1",
                    completion_status="completed",
                    result_payload={},
                ),
                RLMRecordedSubcallResult(
                    order=0,
                    child_node_id="rlm_child_2",
                    child_ac_node_id="ac_child_2",
                    completion_status="completed",
                    result_payload={},
                ),
            ),
        )


@pytest.mark.asyncio
async def test_dry_run_does_not_call_hermes_atomic_execution(tmp_path: Path) -> None:
    """Dry-run validates the isolated path without executing the atomic Hermes call."""
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            dry_run=True,
            hermes_runtime=hermes_runtime,
        )
    )

    assert result.status == "ready"
    assert result.hermes_subcall_count == 0
    assert result.atomic_execution is None
    assert hermes_runtime.calls == []


@pytest.mark.asyncio
async def test_dry_run_records_outer_scaffold_guarding_and_termination(
    tmp_path: Path,
) -> None:
    """Dry-run still uses the Ouroboros scaffold for guardrails and stop state."""
    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            dry_run=True,
        )
    )

    scaffold = result.outer_scaffold_state
    assert scaffold is not None
    assert result.termination_reason == RLMTerminationReason.DRY_RUN_READY
    assert scaffold.run_state == RLMRunLifecycleState.COMPLETED
    assert scaffold.termination_reason == RLMTerminationReason.DRY_RUN_READY
    assert scaffold.is_terminal is True
    assert scaffold.has_converged is False
    assert scaffold.ac_tree.max_depth == 5
    assert scaffold.ac_tree.root_id == "rlm_ac_root"
    assert scaffold.nodes["rlm_node_root"].state == RLMNodeLifecycleState.QUEUED
    assert scaffold.work_queue == ["rlm_node_root"]
    assert [
        transition.to_state for transition in scaffold.transitions if transition.subject == "run"
    ] == [
        "initialized",
        "guarding",
        "scheduling",
        "completed",
    ]


@pytest.mark.asyncio
async def test_outer_scaffold_schedules_chunk_recursion_and_parent_termination(
    tmp_path: Path,
) -> None:
    """Ouroboros owns chunk scheduling, AC/RLM state, and final stop reason."""
    target = tmp_path / "large.py"
    target.write_text("VALUE_1 = 1\nVALUE_2 = 2\nVALUE_3 = 3\n", encoding="utf-8")
    hermes_runtime = _FakeHermesRuntime()

    result = await run_rlm_loop(
        RLMRunConfig(
            target="large.py",
            cwd=tmp_path,
            chunk_line_limit=1,
            hermes_runtime=hermes_runtime,
        )
    )

    scaffold = result.outer_scaffold_state
    assert scaffold is not None
    assert scaffold.run_state == RLMRunLifecycleState.COMPLETED
    assert scaffold.termination_reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED
    assert scaffold.is_terminal is True
    assert scaffold.has_converged is True
    assert result.termination_reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED
    assert scaffold.work_queue == []
    assert scaffold.iteration == result.hermes_subcall_count + 1
    assert scaffold.generated_rlm_tree_depth == 1
    assert scaffold.to_dict()["generated_rlm_tree_depth"] == 1

    root_node = scaffold.nodes["rlm_node_root"]
    assert root_node.state == RLMNodeLifecycleState.SYNTHESIS_COMPLETE
    assert root_node.child_node_ids == (
        "rlm_node_atomic_chunk_001",
        "rlm_node_atomic_chunk_002",
        "rlm_node_atomic_chunk_003",
    )
    assert root_node.terminal_reason == RLMTerminationReason.PARENT_SYNTHESIS_COMPLETED

    child_nodes = [scaffold.nodes[f"rlm_node_atomic_chunk_{index:03d}"] for index in range(1, 4)]
    assert [node.state for node in child_nodes] == [
        RLMNodeLifecycleState.ATOMIC_COMPLETE,
        RLMNodeLifecycleState.ATOMIC_COMPLETE,
        RLMNodeLifecycleState.ATOMIC_COMPLETE,
    ]
    assert [node.parent_node_id for node in child_nodes] == ["rlm_node_root"] * 3
    assert [node.parent_call_id for node in child_nodes] == [
        "rlm_call_atomic_synthesis",
        "rlm_call_atomic_synthesis",
        "rlm_call_atomic_synthesis",
    ]

    ac_tree = scaffold.ac_tree
    root_ac = ac_tree.get_node("rlm_ac_root")
    assert root_ac is not None
    assert root_ac.status == ACStatus.COMPLETED
    assert root_ac.children_ids == (
        "rlm_ac_atomic_chunk_001",
        "rlm_ac_atomic_chunk_002",
        "rlm_ac_atomic_chunk_003",
    )
    child_ac_statuses: list[ACStatus] = []
    for index in range(1, 4):
        child_ac = ac_tree.get_node(f"rlm_ac_atomic_chunk_{index:03d}")
        assert child_ac is not None
        assert child_ac.originating_subcall_trace_id == "rlm_trace_rlm_call_atomic_synthesis"
        assert (
            child_ac.metadata["originating_subcall_trace_id"]
            == "rlm_trace_rlm_call_atomic_synthesis"
        )
        child_ac_statuses.append(child_ac.status)
    assert child_ac_statuses == [ACStatus.COMPLETED, ACStatus.COMPLETED, ACStatus.COMPLETED]

    prompt_envelopes = [json.loads(str(call["prompt"])) for call in hermes_runtime.calls]
    assert [prompt["outer_scaffold"]["owner"] for prompt in prompt_envelopes] == [
        "ouroboros",
        "ouroboros",
        "ouroboros",
        "ouroboros",
    ]
    assert prompt_envelopes[0]["outer_scaffold"]["active_node_id"] == ("rlm_node_atomic_chunk_001")
    assert prompt_envelopes[0]["outer_scaffold"]["run_state"] == "running_node"
    assert prompt_envelopes[-1]["outer_scaffold"]["active_node_id"] == "rlm_node_root"
    assert prompt_envelopes[-1]["outer_scaffold"]["run_state"] == "synthesizing"
    assert (
        "max_iterations_reached" in prompt_envelopes[-1]["outer_scaffold"]["termination_conditions"]
    )


def test_outer_scaffold_applies_max_iteration_termination(tmp_path: Path) -> None:
    """The outer scheduler stops before another node can recurse forever."""
    config = RLMRunConfig(
        target="src",
        cwd=tmp_path,
        max_iterations=1,
    )
    scaffold = RLMOuterScaffoldState.initialize(config)
    scaffold.enter_guarding()
    scaffold.complete_guarding()

    scaffold.select_node("rlm_node_root")

    with pytest.raises(ValueError, match="max iterations reached"):
        scaffold.select_node("rlm_node_root")

    assert scaffold.run_state == RLMRunLifecycleState.FAILED
    assert scaffold.termination_reason == RLMTerminationReason.MAX_ITERATIONS_REACHED


def test_rlm_guide_benchmark_citations_resolve_to_precise_repository_spans() -> None:
    """Benchmark guide claims cite existing, bounded repository spans."""
    import re

    repo_root = Path(__file__).resolve().parents[3]
    guide_path = repo_root / "docs/guides/recursive-language-model.md"
    content = guide_path.read_text(encoding="utf-8")
    start_marker = "### Benchmark Claim Grounding Inventory"
    end_marker = "\n## Trace Requirements"

    assert start_marker in content
    assert end_marker in content
    benchmark_section = content.split(start_marker, 1)[1].split(end_marker, 1)[0]
    citations = re.findall(
        r"([A-Za-z0-9_./-]+\.py):([0-9]+)-([0-9]+)",
        benchmark_section,
    )

    assert citations
    for relative_path, start_text, end_text in citations:
        source_path = repo_root / relative_path
        assert source_path.is_file(), relative_path

        start_line = int(start_text)
        end_line = int(end_text)
        line_count = len(
            source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        )

        assert 1 <= start_line <= end_line <= line_count, relative_path
        assert end_line - start_line <= 180, f"{relative_path}:{start_line}-{end_line}"
