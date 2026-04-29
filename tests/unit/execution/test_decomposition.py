"""Unit tests for ouroboros.execution.decomposition module.

Tests cover:
- DecompositionResult model
- DecompositionError class
- JSON extraction from responses
- Child validation (count, cycles, empty)
- Context compression
- decompose_ac() function
- Max depth enforcement
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result


class TestDecompositionResult:
    """Tests for DecompositionResult dataclass."""

    def test_result_creation(self):
        """DecompositionResult should store all fields."""
        from ouroboros.execution.decomposition import DecompositionResult

        result = DecompositionResult(
            parent_ac_id="ac_parent123",
            child_acs=("Child AC 1", "Child AC 2"),
            child_ac_ids=("ac_child1", "ac_child2"),
            reasoning="Split by functionality",
            events=[],
        )

        assert result.parent_ac_id == "ac_parent123"
        assert len(result.child_acs) == 2
        assert len(result.child_ac_ids) == 2
        assert result.reasoning == "Split by functionality"

    def test_result_is_frozen(self):
        """DecompositionResult should be immutable."""
        from ouroboros.execution.decomposition import DecompositionResult

        result = DecompositionResult(
            parent_ac_id="ac_test",
            child_acs=("A", "B"),
            child_ac_ids=("ac_a", "ac_b"),
            reasoning="Test",
        )

        with pytest.raises((AttributeError, TypeError)):
            result.parent_ac_id = "modified"


class TestDecompositionError:
    """Tests for DecompositionError class."""

    def test_error_creation(self):
        """DecompositionError should store context."""
        from ouroboros.execution.decomposition import DecompositionError

        error = DecompositionError(
            message="Max depth reached",
            ac_id="ac_test",
            depth=5,
            error_type="max_depth_reached",
        )

        assert "Max depth reached" in str(error)
        assert error.ac_id == "ac_test"
        assert error.depth == 5
        assert error.error_type == "max_depth_reached"

    def test_error_with_details(self):
        """DecompositionError should store additional details."""
        from ouroboros.execution.decomposition import DecompositionError

        error = DecompositionError(
            message="Parse failed",
            ac_id="ac_test",
            depth=2,
            error_type="parse_failure",
            details={"response_preview": "invalid json"},
        )

        assert error.details == {"response_preview": "invalid json"}


class TestJsonExtraction:
    """Tests for JSON extraction from LLM responses."""

    def test_extract_direct_json(self):
        """Should extract direct JSON response."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = '{"children": ["A", "B"], "reasoning": "Test"}'

        result = _extract_json_from_response(response)

        assert result is not None
        assert result["children"] == ["A", "B"]

    def test_extract_json_from_markdown(self):
        """Should extract JSON from markdown code block."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = """Here's the decomposition:
```json
{"children": ["Task A", "Task B", "Task C"], "reasoning": "Split by domain"}
```
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert len(result["children"]) == 3

    def test_extract_json_with_children_array(self):
        """Should find JSON with children array pattern."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = """I'll decompose this into:
{"children": ["Setup database", "Create API"], "reasoning": "Backend split"}
Additional notes here.
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert "children" in result

    def test_extract_invalid_returns_none(self):
        """Should return None for invalid JSON."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = "No JSON here, just text about decomposition."

        result = _extract_json_from_response(response)

        assert result is None


class TestValidateChildren:
    """Tests for _validate_children() function."""

    def test_valid_children(self):
        """Should accept valid children list."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Child 1", "Child 2", "Child 3"],
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_ok

    def test_too_few_children(self):
        """Should reject less than MIN_CHILDREN."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Only one"],
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "minimum" in str(result.error).lower()
        assert result.error.error_type == "insufficient_children"

    def test_too_many_children(self):
        """Should reject more than MAX_CHILDREN."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["A", "B", "C", "D", "E", "F"],  # 6 children
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "maximum" in str(result.error).lower()
        assert result.error.error_type == "too_many_children"

    def test_cyclic_decomposition(self):
        """Should reject child identical to parent."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Parent AC", "Different child"],  # First is same as parent
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "cyclic" in str(result.error).lower()
        assert result.error.error_type == "cyclic_decomposition"

    def test_cyclic_case_insensitive(self):
        """Should detect cycles case-insensitively."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["  PARENT AC  ", "Different child"],
            parent_content="parent ac",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert result.error.error_type == "cyclic_decomposition"

    def test_empty_child(self):
        """Should reject empty child content."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Valid child", "   ", "Another valid"],
            parent_content="Parent",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "empty" in str(result.error).lower()
        assert result.error.error_type == "empty_child"


class TestContextCompression:
    """Tests for _compress_context() function."""

    def test_no_compression_at_shallow_depth(self):
        """Should not compress at depth < 3."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "A" * 1000  # 1000 chars

        result = _compress_context(insights, depth=2)

        assert result == insights  # Not compressed

    def test_compression_at_depth_3(self):
        """Should compress at depth >= 3."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "A" * 1000

        result = _compress_context(insights, depth=3)

        assert len(result) < 1000
        assert "compressed for depth" in result

    def test_short_content_not_compressed(self):
        """Should not compress content under 500 chars."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "Short insights"

        result = _compress_context(insights, depth=5)

        assert result == insights


class TestDecomposeAc:
    """Tests for decompose_ac() async function."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter that returns valid decomposition."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(
                content='{"children": ["Child AC 1", "Child AC 2", "Child AC 3"], "reasoning": "Split by functionality"}'
            )
        )
        return adapter

    @pytest.fixture
    def failing_llm_adapter(self):
        """Create a mock LLM adapter that fails."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.err(
            ProviderError("LLM timeout", provider="openrouter")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_successful_decomposition(self, mock_llm_adapter):
        """decompose_ac() should return children on success."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            discover_insights="User needs login and registration",
        )

        assert result.is_ok
        assert len(result.value.child_acs) == 3
        assert len(result.value.child_ac_ids) == 3
        assert all(id.startswith("ac_") for id in result.value.child_ac_ids)
        assert result.value.reasoning == "Split by functionality"

    @pytest.mark.asyncio
    async def test_decomposition_emits_event(self, mock_llm_adapter):
        """decompose_ac() should emit decomposition event."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_ok
        assert len(result.value.events) == 1
        assert result.value.events[0].type == "ac.decomposition.completed"

    @pytest.mark.asyncio
    async def test_hermes_subcall_occurs_during_decomposition(self, mock_llm_adapter):
        """RLM decomposition should ask Hermes for guidance before generating children."""
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult

        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message="Split auth into storage, endpoints, and validation.",
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            discover_insights="User needs login and registration",
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_parent_call",
        )

        assert result.is_ok
        hermes_runtime.execute_task_to_result.assert_awaited_once()

        hermes_kwargs = hermes_runtime.execute_task_to_result.await_args.kwargs
        assert hermes_kwargs["tools"] == []
        assert "Do not invoke Ouroboros" in hermes_kwargs["system_prompt"]
        assert "Implement user authentication" in hermes_kwargs["prompt"]

        assert result.value.hermes_subcall is not None
        assert result.value.hermes_subcall.parent_call_id == "rlm_parent_call"
        assert result.value.hermes_subcall.subcall_id is not None
        assert result.value.hermes_subcall.subcall_id.startswith("rlm_subcall_")
        assert f"subcall_id: {result.value.hermes_subcall.subcall_id}" in hermes_kwargs["prompt"]
        assert result.value.hermes_subcall.depth == 0
        assert result.value.hermes_subcall.structured_result is not None
        assert result.value.hermes_subcall.structured_result.verdict == "partial"
        assert result.value.hermes_subcall.structured_result.control.requires_retry is True

        llm_messages = mock_llm_adapter.complete.call_args.args[0]
        user_message = llm_messages[1].content
        assert "Hermes decomposition sub-call guidance" in user_message
        assert "Hermes normalized structured result" in user_message
        assert "Split auth into storage, endpoints, and validation." in user_message

    @pytest.mark.asyncio
    async def test_hermes_decomposition_subcall_captures_structured_contract(
        self,
        mock_llm_adapter,
    ):
        """Valid Hermes decomposition JSON should be parsed into the RLM contract."""
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_auth",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.88,
            result={"summary": "Split auth into API and persistence work."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Auth API",
                        statement="Implement login and registration endpoints.",
                        success_criteria=("Endpoints are verifiable",),
                        rationale="API behavior is a distinct boundary.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Auth persistence",
                        statement="Persist user credentials securely.",
                        success_criteria=("Credentials are stored safely",),
                        rationale="Storage can be validated independently.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=structured_output.to_json(),
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_node_auth",
        )

        assert result.is_ok
        assert result.value.child_acs == (
            "Implement login and registration endpoints.",
            "Persist user credentials securely.",
        )
        assert result.value.dependencies == ((), (0,))
        assert len(result.value.child_ac_nodes) == 2
        assert [node.id for node in result.value.child_ac_nodes] == list(result.value.child_ac_ids)
        assert [node.content for node in result.value.child_ac_nodes] == list(
            result.value.child_acs
        )
        assert [node.parent_id for node in result.value.child_ac_nodes] == [
            "ac_parent",
            "ac_parent",
        ]
        assert [node.depth for node in result.value.child_ac_nodes] == [1, 1]
        assert [node.status.value for node in result.value.child_ac_nodes] == [
            "pending",
            "pending",
        ]
        assert result.value.child_ac_nodes[0].metadata["title"] == "Auth API"
        assert result.value.child_ac_nodes[0].metadata["success_criteria"] == [
            "Endpoints are verifiable"
        ]
        assert result.value.child_ac_nodes[1].metadata["depends_on"] == [0]
        assert result.value.child_ac_nodes[1].metadata["source"] == ("rlm.hermes.decomposition")
        mock_llm_adapter.complete.assert_not_called()
        assert result.value.hermes_subcall is not None
        assert result.value.hermes_subcall.rlm_node_id == "rlm_node_auth"
        assert result.value.hermes_subcall.structured_result == structured_output
        assert result.value.hermes_subcall.structured_result.artifact.proposed_child_acs[
            1
        ].depends_on == (0,)

        subquestion_results = result.value.hermes_subquestion_results
        assert len(subquestion_results) == 2
        assert [item["child_ac_id"] for item in subquestion_results] == list(
            result.value.child_ac_ids
        )
        assert subquestion_results[0]["subquestion"]["title"] == "Auth API"
        assert subquestion_results[1]["subquestion"]["depends_on"] == [0]
        assert subquestion_results[0]["rlm_node_id"] == "rlm_node_auth"
        assert subquestion_results[0]["verdict"] == "decomposed"
        hermes_call = subquestion_results[0]["hermes_call"]
        assert hermes_call["schema_version"] == "rlm.trace.v1"
        assert "Implement user authentication" in hermes_call["prompt"]
        assert hermes_call["completion"] == structured_output.to_json()
        assert hermes_call["parent_call_id"] == "rlm_node_auth"
        assert hermes_call["subcall_id"].startswith("rlm_subcall_")
        assert hermes_call["trace_id"] == "rlm_trace_rlm_node_auth"
        assert hermes_call["depth"] == 0
        assert hermes_call["mode"] == "decompose_ac"
        assert result.value.child_ac_ids != ("Auth API", "Auth persistence")
        assert {node.originating_subcall_trace_id for node in result.value.child_ac_nodes} == {
            "rlm_trace_rlm_node_auth"
        }
        assert {
            node.metadata["originating_subcall_trace_id"] for node in result.value.child_ac_nodes
        } == {"rlm_trace_rlm_node_auth"}

        event = result.value.events[0]
        assert event.data["child_ac_ids"] == list(result.value.child_ac_ids)
        assert event.data["child_ac_nodes"] == [
            {
                "id": node.id,
                "content": node.content,
                "depth": node.depth,
                "parent_id": node.parent_id,
                "status": node.status.value,
                "is_atomic": node.is_atomic,
                "children_ids": list(node.children_ids),
                "execution_id": node.execution_id,
                "originating_subcall_trace_id": node.originating_subcall_trace_id,
                "metadata": node.metadata,
            }
            for node in result.value.child_ac_nodes
        ]
        assert event.data["hermes_subquestion_results"] == list(subquestion_results)
        persisted_payload = event.to_db_dict()["payload"]
        assert persisted_payload["child_ac_ids"] == list(result.value.child_ac_ids)
        assert persisted_payload["child_ac_nodes"][0]["id"] == result.value.child_ac_ids[0]
        assert (
            persisted_payload["child_ac_nodes"][0]["originating_subcall_trace_id"]
            == "rlm_trace_rlm_node_auth"
        )
        assert (
            persisted_payload["hermes_subquestion_results"][0]["child_ac_id"]
            == (result.value.child_ac_ids[0])
        )

    @pytest.mark.asyncio
    async def test_generated_hermes_child_ac_nodes_persist_and_reload_trace_backref(
        self,
        mock_llm_adapter,
        tmp_path,
    ):
        """Generated Hermes child AC nodes should reload with sub-call trace provenance."""
        from ouroboros.core.ac_tree import ACNode, ACStatus, ACTree
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.persistence.event_store import EventStore
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_auth",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.91,
            result={"summary": "Split auth into a persisted child AC."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Auth API",
                        statement="Implement login and registration endpoints.",
                        success_criteria=("Endpoints are verifiable",),
                        rationale="API behavior is a distinct boundary.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Auth persistence",
                        statement="Persist user credentials securely.",
                        success_criteria=("Credentials are stored safely",),
                        rationale="Storage is independently verifiable.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=structured_output.to_json(),
                    messages=(),
                )
            )
        )
        ac_tree = ACTree()
        ac_tree.add_node(
            ACNode(
                id="ac_parent",
                content="Implement user authentication",
                depth=0,
            )
        )

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_call_parent",
            call_id="rlm_call_decompose_auth",
            rlm_node_id="rlm_node_auth",
            ac_tree=ac_tree,
        )

        assert result.is_ok
        assert len(result.value.persisted_child_ac_nodes) == 2
        expected_trace_id = "rlm_trace_rlm_call_decompose_auth"

        db_path = tmp_path / "rlm_trace_backref.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        await store.append(result.value.events[0])
        await store.close()

        reloaded_store = EventStore(f"sqlite+aiosqlite:///{db_path}", read_only=True)
        await reloaded_store.initialize(create_schema=False)
        replayed = await reloaded_store.replay("ac_decomposition", "ac_parent")
        await reloaded_store.close()

        assert len(replayed) == 1
        replayed_payload = replayed[0].data
        child_payloads = replayed_payload["child_ac_nodes"]
        assert len(child_payloads) == 2
        for child_payload in child_payloads:
            assert child_payload["originating_subcall_trace_id"] == expected_trace_id
            assert child_payload["metadata"]["originating_subcall_trace_id"] == expected_trace_id

        reloaded_tree = ACTree.from_dict(
            {
                "root_id": "ac_parent",
                "max_depth": 5,
                "nodes": {
                    "ac_parent": {
                        "id": "ac_parent",
                        "content": "Implement user authentication",
                        "depth": 0,
                        "parent_id": None,
                        "status": ACStatus.DECOMPOSED.value,
                        "is_atomic": False,
                        "children_ids": replayed_payload["child_ac_ids"],
                        "execution_id": None,
                        "originating_subcall_trace_id": None,
                        "metadata": {},
                    },
                    **{child_payload["id"]: child_payload for child_payload in child_payloads},
                },
            }
        )

        for child_payload in child_payloads:
            reloaded_child = reloaded_tree.get_node(child_payload["id"])
            assert reloaded_child is not None
            assert reloaded_child.originating_subcall_trace_id == expected_trace_id
            assert reloaded_child.metadata["originating_subcall_trace_id"] == expected_trace_id

    @pytest.mark.asyncio
    async def test_repeated_hermes_decomposition_output_does_not_duplicate_ac_tree_nodes(
        self,
        mock_llm_adapter,
    ):
        """Repeated Hermes decomposition JSON should not create duplicate AC nodes."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        tree = ACTree()
        tree.add_node(ACNode(id="ac_parent", content="Parent AC", depth=0))
        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_duplicate_guard",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.9,
            result={"summary": "Split into repeatable Hermes child nodes."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="First repeatable child",
                        statement="Implement the first repeatable Hermes child.",
                        success_criteria=("First repeatable child is committed once",),
                        rationale="The child identity is stable across retries.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Second repeatable child",
                        statement="Implement the second repeatable Hermes child.",
                        success_criteria=("Second repeatable child is committed once",),
                        rationale="The child identity is stable across retries.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        repeated_output = structured_output.to_json()
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            side_effect=[
                Result.ok(TaskResult(success=True, final_message=repeated_output, messages=())),
                Result.ok(TaskResult(success=True, final_message=repeated_output, messages=())),
            ]
        )

        first = await decompose_ac(
            ac_content="Implement repeatable decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            rlm_node_id="rlm_node_duplicate_guard",
            ac_tree=tree,
        )
        second = await decompose_ac(
            ac_content="Implement repeatable decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            rlm_node_id="rlm_node_duplicate_guard",
            ac_tree=tree,
        )

        assert first.is_ok
        assert second.is_ok
        assert first.value.child_ac_ids == second.value.child_ac_ids
        assert first.value.persisted_child_ac_nodes == first.value.child_ac_nodes
        assert second.value.persisted_child_ac_nodes == ()
        assert "child_ac_nodes" not in second.value.events[0].data
        assert tree.nodes["ac_parent"].children_ids == first.value.child_ac_ids
        assert len(tree.nodes) == 1 + len(first.value.child_ac_ids)
        assert set(tree.nodes) == {"ac_parent", *first.value.child_ac_ids}
        assert len(tree.get_children("ac_parent")) == len(first.value.child_ac_ids)
        hermes_runtime.execute_task_to_result.assert_awaited()
        assert hermes_runtime.execute_task_to_result.await_count == 2
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_hermes_subquestion_results_materialize_ac_tree_children(
        self,
        mock_llm_adapter,
    ):
        """Accepted Hermes sub-question records should reconstruct AC tree children."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
            RLMHermesEvidenceReference,
            RLMHermesResidualGap,
        )

        tree = ACTree()
        tree.add_node(ACNode(id="ac_parent", content="Parent AC", depth=1))
        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_subquestions",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.86,
            result={"summary": "Split parent AC from Hermes sub-question records."},
            evidence_references=(
                RLMHermesEvidenceReference(
                    chunk_id="src/ouroboros/rlm/contracts.py:1-80",
                    source_path="src/ouroboros/rlm/contracts.py",
                    start_line=1,
                    end_line=80,
                    claim="The RLM contract defines proposed child AC payloads.",
                ),
            ),
            residual_gaps=(
                RLMHermesResidualGap(
                    gap="No runtime execution was needed for this decomposition.",
                    impact="The test only validates AC tree materialization.",
                    suggested_next_step="Execute generated atomic children separately.",
                ),
            ),
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Normalize child payloads",
                        statement="Normalize Hermes child payloads into canonical AC nodes.",
                        success_criteria=("Canonical child AC nodes are materialized",),
                        rationale="Ouroboros owns AC tree mutation after Hermes replies.",
                        estimated_chunk_needs=("src/ouroboros/execution/decomposition.py",),
                    ),
                    RLMHermesACSubQuestion(
                        title="Preserve dependency metadata",
                        statement="Preserve Hermes sibling dependencies on AC nodes.",
                        success_criteria=("Dependency metadata can be replayed",),
                        rationale="Trace replay needs deterministic sibling ordering.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=structured_output.to_json(),
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Split parent AC from Hermes output",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=1,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_call_parent",
            call_id="rlm_call_decompose_parent",
            rlm_node_id="rlm_node_subquestions",
            ac_tree=tree,
        )

        assert result.is_ok
        decomposition = result.value
        assert mock_llm_adapter.complete.await_count == 0
        assert decomposition.child_acs == (
            "Normalize Hermes child payloads into canonical AC nodes.",
            "Preserve Hermes sibling dependencies on AC nodes.",
        )
        assert decomposition.dependencies == ((), (0,))
        assert decomposition.persisted_child_ac_nodes == decomposition.child_ac_nodes
        assert tree.nodes["ac_parent"].children_ids == decomposition.child_ac_ids

        subquestion_results = decomposition.hermes_subquestion_results
        assert [item["child_ac_id"] for item in subquestion_results] == list(
            decomposition.child_ac_ids
        )
        assert [item["subquestion"]["statement"] for item in subquestion_results] == list(
            decomposition.child_acs
        )

        for item in subquestion_results:
            child_node = tree.nodes[item["child_ac_id"]]
            subquestion = item["subquestion"]

            assert child_node.content == subquestion["statement"]
            assert child_node.parent_id == item["parent_ac_id"] == "ac_parent"
            assert child_node.depth == 2
            assert child_node.metadata["source"] == "rlm.hermes.decomposition"
            assert child_node.metadata["title"] == subquestion["title"]
            assert child_node.metadata["statement"] == subquestion["statement"]
            assert child_node.metadata["depends_on"] == subquestion["depends_on"]
            assert child_node.metadata["rlm_node_id"] == item["rlm_node_id"]
            assert child_node.originating_subcall_trace_id == "rlm_trace_rlm_call_decompose_parent"
            assert (
                child_node.metadata["originating_subcall_trace_id"]
                == "rlm_trace_rlm_call_decompose_parent"
            )

        assert subquestion_results[0]["evidence_references"][0]["chunk_id"] == (
            "src/ouroboros/rlm/contracts.py:1-80"
        )
        assert subquestion_results[0]["residual_gaps"][0]["gap"].startswith("No runtime execution")
        assert subquestion_results[0]["hermes_call"]["call_id"] == "rlm_call_decompose_parent"
        assert subquestion_results[0]["hermes_call"]["parent_call_id"] == "rlm_call_parent"
        assert subquestion_results[0]["hermes_call"]["subcall_id"].startswith("rlm_subcall_")
        assert (
            subquestion_results[0]["hermes_call"]["trace_id"]
            == "rlm_trace_rlm_call_decompose_parent"
        )

        event_payload = decomposition.events[0].to_db_dict()["payload"]
        assert event_payload["child_ac_ids"] == list(decomposition.child_ac_ids)
        assert event_payload["hermes_subquestion_results"] == list(subquestion_results)
        assert [node["id"] for node in event_payload["child_ac_nodes"]] == list(
            decomposition.child_ac_ids
        )
        assert {
            node["originating_subcall_trace_id"] for node in event_payload["child_ac_nodes"]
        } == {"rlm_trace_rlm_call_decompose_parent"}

    @pytest.mark.asyncio
    async def test_hermes_decomposition_child_call_context_links_to_current_call(
        self,
        mock_llm_adapter,
    ):
        """Accepted child ACs carry a nested Hermes call context for future recursion."""
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_current",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.8,
            result={"summary": "Split into nested child calls."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Nested first",
                        statement="Implement the first nested child AC.",
                        success_criteria=("First nested child passes",),
                        rationale="It can recurse independently.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Nested second",
                        statement="Implement the second nested child AC.",
                        success_criteria=("Second nested child passes",),
                        rationale="It can recurse independently.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=structured_output.to_json(),
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Implement nested recursive decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=2,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_call_parent",
            call_id="rlm_call_current",
            rlm_node_id="rlm_node_current",
        )

        assert result.is_ok
        assert result.value.hermes_subcall is not None
        assert result.value.hermes_subcall.call_id == "rlm_call_current"
        assert result.value.hermes_subcall.parent_call_id == "rlm_call_parent"
        assert result.value.hermes_subcall.subcall_id is not None
        assert result.value.hermes_subcall.subcall_id.startswith("rlm_subcall_")
        assert result.value.hermes_subcall.depth == 2
        assert result.value.hermes_subcall.to_trace_record().call_id == "rlm_call_current"
        assert (
            result.value.hermes_subcall.to_trace_record().trace_id == "rlm_trace_rlm_call_current"
        )
        assert (
            result.value.hermes_subcall.to_trace_record().subcall_id
            == result.value.hermes_subcall.subcall_id
        )

        prompt = hermes_runtime.execute_task_to_result.await_args.kwargs["prompt"]
        assert f"subcall_id: {result.value.hermes_subcall.subcall_id}" in prompt
        assert "call_id: rlm_call_current" in prompt
        assert "parent_call_id: rlm_call_parent" in prompt

        child_metadata = [node.metadata for node in result.value.child_ac_nodes]
        assert {node.originating_subcall_trace_id for node in result.value.child_ac_nodes} == {
            "rlm_trace_rlm_call_current"
        }
        assert [metadata["rlm_parent_call_id"] for metadata in child_metadata] == [
            "rlm_call_current",
            "rlm_call_current",
        ]
        assert [metadata["originating_subcall_trace_id"] for metadata in child_metadata] == [
            "rlm_trace_rlm_call_current",
            "rlm_trace_rlm_call_current",
        ]
        assert [metadata["rlm_call_depth"] for metadata in child_metadata] == [3, 3]
        assert [metadata["rlm_parent_subcall_id"] for metadata in child_metadata] == [
            result.value.hermes_subcall.subcall_id,
            result.value.hermes_subcall.subcall_id,
        ]
        assert [
            metadata["rlm_child_call_context"]["parent_call_id"] for metadata in child_metadata
        ] == [
            "rlm_call_current",
            "rlm_call_current",
        ]
        assert [
            metadata["rlm_child_call_context"]["parent_subcall_id"] for metadata in child_metadata
        ] == [
            result.value.hermes_subcall.subcall_id,
            result.value.hermes_subcall.subcall_id,
        ]
        assert [metadata["rlm_child_call_context"]["depth"] for metadata in child_metadata] == [
            3,
            3,
        ]

    @pytest.mark.asyncio
    async def test_hermes_decomposition_subcall_normalizes_markdown_json(
        self,
        mock_llm_adapter,
    ):
        """Hermes markdown-wrapped JSON should still normalize into the RLM schema."""
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_markdown",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.75,
            result={"summary": "Split the work from markdown JSON."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="First child",
                        statement="Implement the first child AC.",
                        success_criteria=("First child is verifiable",),
                        rationale="It is independently testable.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Second child",
                        statement="Implement the second child AC.",
                        success_criteria=("Second child is verifiable",),
                        rationale="It is independently testable.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=f"```json\n{structured_output.to_json()}\n```",
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Implement a two-part feature",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            rlm_node_id="rlm_node_markdown",
            parent_call_id="rlm_call_parent",
        )

        assert result.is_ok
        assert result.value.child_acs == (
            "Implement the first child AC.",
            "Implement the second child AC.",
        )
        assert result.value.dependencies == ((), (0,))
        assert result.value.hermes_subcall is not None
        assert result.value.hermes_subcall.parent_call_id == "rlm_call_parent"
        assert result.value.hermes_subcall.structured_result == structured_output
        mock_llm_adapter.complete.assert_not_called()

    def test_structured_hermes_decomposition_creates_stable_child_ac_nodes(self):
        """Accepted normalized Hermes children become stable materialized AC nodes."""
        from ouroboros.execution.decomposition import (
            CanonicalChildACNodeInput,
            HermesDecompositionSubcall,
            _create_decomposition_result,
        )
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_stable",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.9,
            result={"summary": "Split stable children."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="First stable child",
                        statement="Implement the first stable child.",
                        success_criteria=("First child passes",),
                        rationale="It is a separate boundary.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Second stable child",
                        statement="Implement the second stable child.",
                        success_criteria=("Second child passes",),
                        rationale="It depends on the first child.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_subcall = HermesDecompositionSubcall(
            completion=structured_output.to_json(),
            structured_result=structured_output,
            rlm_node_id="rlm_node_stable",
            ac_node_id="ac_parent",
        )

        first = _create_decomposition_result(
            ac_content="Implement stable decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=2,
            children=[
                "Implement the first stable child.",
                "Implement the second stable child.",
            ],
            dependencies=[(), (0,)],
            reasoning="Split stable children.",
            hermes_subcall=hermes_subcall,
        )
        second = _create_decomposition_result(
            ac_content="Implement stable decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=2,
            children=[
                "Implement the first stable child.",
                "Implement the second stable child.",
            ],
            dependencies=[(), (0,)],
            reasoning="Split stable children.",
            hermes_subcall=hermes_subcall,
        )

        assert first.is_ok
        assert second.is_ok
        assert first.value.child_ac_ids == second.value.child_ac_ids
        assert len(first.value.child_ac_nodes) == len(structured_output.artifact.proposed_child_acs)
        assert all(
            isinstance(node_input, CanonicalChildACNodeInput)
            for node_input in first.value.child_ac_node_inputs
        )
        assert [node_input.to_ac_node() for node_input in first.value.child_ac_node_inputs] == list(
            first.value.child_ac_nodes
        )
        assert [node.id for node in first.value.child_ac_nodes] == list(first.value.child_ac_ids)
        assert first.value.child_ac_nodes[0].depth == 3
        assert first.value.child_ac_nodes[0].parent_id == "ac_parent"
        assert first.value.child_ac_nodes[0].children_ids == ()
        assert first.value.child_ac_nodes[0].metadata["success_criteria"] == ["First child passes"]
        assert first.value.child_ac_nodes[1].metadata["depends_on"] == [0]

    def test_hermes_child_payloads_normalize_to_canonical_ac_node_inputs(self):
        """Hermes child payload fields should become canonical AC node inputs."""
        from ouroboros.core.ac_tree import ACStatus
        from ouroboros.execution.decomposition import (
            HermesDecompositionSubcall,
            _normalize_child_ac_node_inputs,
        )
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_inputs",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.82,
            result={"summary": "Canonicalize Hermes children."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Normalize schema",
                        statement="Normalize the first Hermes child payload.",
                        success_criteria=("Canonical node fields are present",),
                        rationale="The AC tree requires stable node inputs.",
                        estimated_chunk_needs=("contracts.py",),
                    ),
                    RLMHermesACSubQuestion(
                        title="Preserve ordering",
                        statement="Normalize the second Hermes child payload.",
                        success_criteria=("Dependencies remain prior-sibling indices",),
                        rationale="Parent replay depends on sibling order.",
                        depends_on=(0,),
                    ),
                ),
            ),
        )
        hermes_subcall = HermesDecompositionSubcall(
            structured_result=structured_output,
            rlm_node_id="rlm_node_inputs",
            ac_node_id="ac_parent",
            call_id="rlm_call_inputs",
            subcall_id="rlm_subcall_inputs",
        )

        node_inputs = _normalize_child_ac_node_inputs(
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=1,
            child_acs=("ignored first fallback", "ignored second fallback"),
            child_ac_ids=("ac_child_1", "ac_child_2"),
            dependencies=((), ()),
            hermes_subcall=hermes_subcall,
        )

        assert [node_input.id for node_input in node_inputs] == ["ac_child_1", "ac_child_2"]
        assert [node_input.content for node_input in node_inputs] == [
            "Normalize the first Hermes child payload.",
            "Normalize the second Hermes child payload.",
        ]
        assert [node_input.depth for node_input in node_inputs] == [2, 2]
        assert [node_input.parent_id for node_input in node_inputs] == [
            "ac_parent",
            "ac_parent",
        ]
        assert all(node_input.status == ACStatus.PENDING for node_input in node_inputs)
        assert all(node_input.children_ids == () for node_input in node_inputs)
        assert node_inputs[0].metadata["source"] == "rlm.hermes.decomposition"
        assert node_inputs[0].metadata["stable_identity"].startswith("child_ac_identity:")
        assert node_inputs[0].metadata["stable_identity_schema"] == (
            "ouroboros.child_ac_identity.v1"
        )
        assert node_inputs[0].metadata["success_criteria"] == ["Canonical node fields are present"]
        assert node_inputs[0].metadata["estimated_chunk_needs"] == ["contracts.py"]
        assert node_inputs[0].originating_subcall_trace_id == "rlm_trace_rlm_call_inputs"
        assert (
            node_inputs[0].metadata["originating_subcall_trace_id"] == "rlm_trace_rlm_call_inputs"
        )
        assert node_inputs[1].metadata["depends_on"] == [0]
        assert node_inputs[1].metadata["rlm_node_id"] == "rlm_node_inputs"
        assert (
            node_inputs[1].to_ac_node().originating_subcall_trace_id == "rlm_trace_rlm_call_inputs"
        )
        assert node_inputs[1].to_ac_node().content == ("Normalize the second Hermes child payload.")

    def test_rejected_hermes_child_payloads_do_not_tag_legacy_child_nodes(self):
        """Only accepted Hermes child payloads should become Hermes-sourced nodes."""
        from ouroboros.execution.decomposition import (
            HermesDecompositionSubcall,
            _create_decomposition_result,
        )
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_partial",
            ac_node_id="ac_parent",
            verdict="partial",
            confidence=0.4,
            result={"summary": "Hermes was not accepted."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="Rejected first",
                        statement="Rejected first Hermes child.",
                        success_criteria=("Not accepted",),
                        rationale="Partial output cannot mutate the tree.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Rejected second",
                        statement="Rejected second Hermes child.",
                        success_criteria=("Not accepted",),
                        rationale="Partial output cannot mutate the tree.",
                    ),
                ),
            ),
        )
        hermes_subcall = HermesDecompositionSubcall(
            structured_result=structured_output,
            rlm_node_id="rlm_node_partial",
            ac_node_id="ac_parent",
        )

        result = _create_decomposition_result(
            ac_content="Implement fallback decomposition",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            children=["Fallback first child.", "Fallback second child."],
            dependencies=[(), (0,)],
            reasoning="Fallback LLM split.",
            hermes_subcall=hermes_subcall,
        )

        assert result.is_ok
        assert result.value.hermes_subquestion_results == ()
        assert [node.content for node in result.value.child_ac_nodes] == [
            "Fallback first child.",
            "Fallback second child.",
        ]
        assert all(
            node.metadata["source"] == "legacy.decomposition"
            for node in result.value.child_ac_nodes
        )
        assert all("rlm_node_id" not in node.metadata for node in result.value.child_ac_nodes)
        assert all(
            node.originating_subcall_trace_id is None for node in result.value.child_ac_nodes
        )
        assert all(
            "originating_subcall_trace_id" not in node.metadata
            for node in result.value.child_ac_nodes
        )

    def test_canonical_child_ac_node_inputs_reject_forward_dependencies(self):
        """Canonical AC node inputs should preserve prior-sibling dependency rules."""
        from ouroboros.execution.decomposition import _normalize_child_ac_node_inputs

        with pytest.raises(ValueError, match="prior sibling"):
            _normalize_child_ac_node_inputs(
                ac_id="ac_parent",
                execution_id="exec_123",
                depth=0,
                child_acs=("First child.", "Second child."),
                child_ac_ids=("ac_child_1", "ac_child_2"),
                dependencies=((1,), ()),
                hermes_subcall=None,
            )

    def test_generated_child_ac_nodes_link_to_parent_without_disturbing_siblings(self):
        """Generated child AC nodes should append to their parent in an ACTree."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import _create_decomposition_result

        tree = ACTree()
        tree.add_node(
            ACNode(
                id="ac_parent",
                content="Parent AC",
                depth=0,
                children_ids=("ac_existing",),
            )
        )

        result = _create_decomposition_result(
            ac_content="Implement parent work",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            children=["Implement first child.", "Implement second child."],
            dependencies=[(), (0,)],
            reasoning="Split by child boundary.",
        )

        assert result.is_ok
        for child_node in result.value.child_ac_nodes:
            tree.add_node(child_node)

        assert [node.parent_id for node in result.value.child_ac_nodes] == [
            "ac_parent",
            "ac_parent",
        ]
        assert tree.nodes["ac_parent"].children_ids == (
            "ac_existing",
            *result.value.child_ac_ids,
        )

    def test_duplicate_generated_child_ac_nodes_are_skipped_by_stable_identity(self):
        """Duplicate materialized children should not create duplicate tree nodes."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import _create_decomposition_result

        tree = ACTree()
        tree.add_node(ACNode(id="ac_parent", content="Parent AC", depth=0))

        result = _create_decomposition_result(
            ac_content="Implement parent work",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            children=[
                "Implement shared child.",
                "  implement   SHARED child. ",
            ],
            dependencies=[(), ()],
            reasoning="Repeated child should be ignored by the tree.",
        )

        assert result.is_ok
        assert (
            result.value.child_ac_nodes[0].metadata["stable_identity"]
            == (result.value.child_ac_nodes[1].metadata["stable_identity"])
        )

        for child_node in result.value.child_ac_nodes:
            tree.add_node(child_node)

        assert result.value.child_ac_ids[1] not in tree.nodes
        assert tree.nodes["ac_parent"].children_ids == (result.value.child_ac_ids[0],)

    def test_persist_hermes_child_ac_nodes_filters_wrong_parent_legacy_and_duplicates(self):
        """Only new Hermes-derived children should be committed to the AC tree."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import (
            HERMES_CHILD_AC_SOURCE,
            persist_hermes_child_ac_nodes,
        )

        tree = ACTree()
        tree.add_node(ACNode(id="ac_parent", content="Parent AC", depth=0))
        tree.add_node(
            ACNode(
                id="ac_existing",
                content="Existing Hermes child.",
                depth=1,
                parent_id="ac_parent",
                metadata={
                    "source": HERMES_CHILD_AC_SOURCE,
                    "stable_identity": "child_ac_identity:existing",
                },
            )
        )
        duplicate = ACNode(
            id="ac_duplicate",
            content="Duplicate Hermes child.",
            depth=1,
            parent_id="ac_parent",
            metadata={
                "source": HERMES_CHILD_AC_SOURCE,
                "stable_identity": "child_ac_identity:existing",
            },
        )
        new_child = ACNode(
            id="ac_new",
            content="New Hermes child.",
            depth=1,
            parent_id="ac_parent",
            metadata={
                "source": HERMES_CHILD_AC_SOURCE,
                "stable_identity": "child_ac_identity:new",
            },
        )
        legacy_child = ACNode(
            id="ac_legacy",
            content="Legacy fallback child.",
            depth=1,
            parent_id="ac_parent",
            metadata={"source": "legacy.decomposition"},
        )
        wrong_parent = ACNode(
            id="ac_wrong_parent",
            content="Wrong parent child.",
            depth=1,
            parent_id="ac_other_parent",
            metadata={
                "source": HERMES_CHILD_AC_SOURCE,
                "stable_identity": "child_ac_identity:wrong_parent",
            },
        )

        persisted = persist_hermes_child_ac_nodes(
            tree,
            parent_ac_id="ac_parent",
            child_ac_nodes=(duplicate, new_child, legacy_child, wrong_parent),
        )

        assert persisted == (new_child,)
        assert tree.nodes["ac_parent"].children_ids == ("ac_existing", "ac_new")
        assert "ac_new" in tree.nodes
        assert "ac_duplicate" not in tree.nodes
        assert "ac_legacy" not in tree.nodes
        assert "ac_wrong_parent" not in tree.nodes

    def test_structured_hermes_decomposition_persists_only_new_children_to_ac_tree(self):
        """Integrated decomposition commit should add only new Hermes child nodes."""
        from ouroboros.core.ac_tree import ACNode, ACTree
        from ouroboros.execution.decomposition import (
            HermesDecompositionSubcall,
            _create_decomposition_result,
        )
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        tree = ACTree()
        tree.add_node(
            ACNode(
                id="ac_parent",
                content="Parent AC",
                depth=0,
                children_ids=("ac_existing",),
            )
        )
        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_tree",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.91,
            result={"summary": "Persist Hermes child nodes."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="First persisted child",
                        statement="Persist the first Hermes child.",
                        success_criteria=("First child is committed",),
                        rationale="It is a new child node.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Second persisted child",
                        statement="Persist the second Hermes child.",
                        success_criteria=("Second child is committed",),
                        rationale="It is another new child node.",
                    ),
                ),
            ),
        )
        hermes_subcall = HermesDecompositionSubcall(
            completion=structured_output.to_json(),
            structured_result=structured_output,
            rlm_node_id="rlm_node_tree",
            ac_node_id="ac_parent",
        )

        first = _create_decomposition_result(
            ac_content="Persist Hermes children",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            children=[
                "Persist the first Hermes child.",
                "Persist the second Hermes child.",
            ],
            dependencies=[(), ()],
            reasoning="Persist Hermes child nodes.",
            hermes_subcall=hermes_subcall,
            ac_tree=tree,
        )
        second = _create_decomposition_result(
            ac_content="Persist Hermes children",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            children=[
                "Persist the first Hermes child.",
                "Persist the second Hermes child.",
            ],
            dependencies=[(), ()],
            reasoning="Persist Hermes child nodes.",
            hermes_subcall=hermes_subcall,
            ac_tree=tree,
        )

        assert first.is_ok
        assert second.is_ok
        assert first.value.persisted_child_ac_nodes == first.value.child_ac_nodes
        assert second.value.child_ac_ids == first.value.child_ac_ids
        assert second.value.persisted_child_ac_nodes == ()
        assert "child_ac_nodes" not in second.value.events[0].data
        assert tree.nodes["ac_parent"].children_ids == (
            "ac_existing",
            *first.value.child_ac_ids,
        )
        assert set(first.value.child_ac_ids).issubset(tree.nodes)

    @pytest.mark.asyncio
    async def test_hermes_decomposition_contract_mismatch_stops_pipeline(
        self,
        mock_llm_adapter,
    ):
        """Hermes output for the wrong RLM node must not mutate AC decomposition."""
        from ouroboros.execution.decomposition import decompose_ac
        from ouroboros.orchestrator.adapter import TaskResult
        from ouroboros.rlm import (
            RLMHermesACDecompositionArtifact,
            RLMHermesACDecompositionResult,
            RLMHermesACSubQuestion,
        )

        structured_output = RLMHermesACDecompositionResult(
            rlm_node_id="wrong_rlm_node",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.75,
            result={"summary": "Wrong node output."},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=False,
                proposed_child_acs=(
                    RLMHermesACSubQuestion(
                        title="First child",
                        statement="Implement the first child AC.",
                        success_criteria=("First child is verifiable",),
                        rationale="It is independently testable.",
                    ),
                    RLMHermesACSubQuestion(
                        title="Second child",
                        statement="Implement the second child AC.",
                        success_criteria=("Second child is verifiable",),
                        rationale="It is independently testable.",
                    ),
                ),
            ),
        )
        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.ok(
                TaskResult(
                    success=True,
                    final_message=structured_output.to_json(),
                    messages=(),
                )
            )
        )

        result = await decompose_ac(
            ac_content="Implement a two-part feature",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            rlm_node_id="expected_rlm_node",
        )

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert result.error.provider == "hermes"
        assert "rlm_node_id mismatch" in result.error.details["contract_error"]
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_hermes_subcall_failure_stops_rlm_decomposition(self, mock_llm_adapter):
        """RLM decomposition should not generate children without required Hermes guidance."""
        from ouroboros.execution.decomposition import decompose_ac

        hermes_runtime = MagicMock()
        hermes_runtime.execute_task_to_result = AsyncMock(
            return_value=Result.err(ProviderError("Hermes unavailable", provider="hermes"))
        )

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            hermes_runtime=hermes_runtime,
            parent_call_id="rlm_parent_call",
        )

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert result.error.provider == "hermes"
        hermes_runtime.execute_task_to_result.assert_awaited_once()
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_depth_rejection(self, mock_llm_adapter):
        """decompose_ac() should reject at max depth."""
        from ouroboros.execution.decomposition import MAX_DEPTH, decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=MAX_DEPTH,  # At max depth
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_err
        assert "max depth" in str(result.error).lower()
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self, failing_llm_adapter):
        """decompose_ac() should return error on LLM failure."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=failing_llm_adapter,
        )

        assert result.is_err
        assert isinstance(result.error, ProviderError)

    @pytest.mark.asyncio
    async def test_parse_failure_returns_error(self):
        """decompose_ac() should return error on parse failure."""
        from ouroboros.execution.decomposition import decompose_ac

        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(MagicMock(content="Not valid JSON response"))

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=adapter,
        )

        assert result.is_err
        assert result.error.error_type == "parse_failure"

    @pytest.mark.asyncio
    async def test_validation_failure_returns_error(self):
        """decompose_ac() should return error on validation failure."""
        from ouroboros.execution.decomposition import decompose_ac

        adapter = AsyncMock()
        # Only 1 child - should fail validation
        adapter.complete.return_value = Result.ok(
            MagicMock(content='{"children": ["Only one"], "reasoning": "Test"}')
        )

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=adapter,
        )

        assert result.is_err
        assert result.error.error_type == "insufficient_children"

    @pytest.mark.asyncio
    async def test_context_compression_at_depth(self, mock_llm_adapter):
        """decompose_ac() should compress context at depth >= 3."""
        from ouroboros.execution.decomposition import decompose_ac

        long_insights = "A" * 1000

        await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=3,
            llm_adapter=mock_llm_adapter,
            discover_insights=long_insights,
        )

        # Check that LLM was called with compressed insights
        call_args = mock_llm_adapter.complete.call_args
        messages = call_args[0][0]
        user_message = messages[1].content
        assert "[compressed for depth]" in user_message

    @pytest.mark.asyncio
    async def test_child_ids_are_unique(self, mock_llm_adapter):
        """decompose_ac() should generate unique child IDs."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_ok
        # All IDs should be unique
        child_ids = result.value.child_ac_ids
        assert len(child_ids) == len(set(child_ids))


class TestDecompositionConstants:
    """Tests for module constants."""

    def test_min_children_is_2(self):
        """MIN_CHILDREN should be 2."""
        from ouroboros.execution.decomposition import MIN_CHILDREN

        assert MIN_CHILDREN == 2

    def test_max_children_is_5(self):
        """MAX_CHILDREN should be 5."""
        from ouroboros.execution.decomposition import MAX_CHILDREN

        assert MAX_CHILDREN == 5

    def test_max_depth_is_5(self):
        """MAX_DEPTH should be 5."""
        from ouroboros.execution.decomposition import MAX_DEPTH

        assert MAX_DEPTH == 5

    def test_compression_depth_is_3(self):
        """COMPRESSION_DEPTH should be 3."""
        from ouroboros.execution.decomposition import COMPRESSION_DEPTH

        assert COMPRESSION_DEPTH == 3


class TestDecompositionPrompts:
    """Tests for decomposition prompts."""

    def test_system_prompt_exists(self):
        """DECOMPOSITION_SYSTEM_PROMPT should be defined."""
        from ouroboros.execution.decomposition import DECOMPOSITION_SYSTEM_PROMPT

        assert "MECE" in DECOMPOSITION_SYSTEM_PROMPT
        assert "2-5" in DECOMPOSITION_SYSTEM_PROMPT

    def test_user_template_has_placeholders(self):
        """DECOMPOSITION_USER_TEMPLATE should have required placeholders."""
        from ouroboros.execution.decomposition import DECOMPOSITION_USER_TEMPLATE

        assert "{ac_content}" in DECOMPOSITION_USER_TEMPLATE
        assert "{discover_insights}" in DECOMPOSITION_USER_TEMPLATE
        assert "{depth}" in DECOMPOSITION_USER_TEMPLATE
        assert "{max_depth}" in DECOMPOSITION_USER_TEMPLATE


class TestDecomposeAcWithRealHermes:
    """End-to-end integration test proving decompose_ac(hermes_runtime=...) is live.

    Second of two empirically-verified Hermes integration paths in Ouroboros
    (the first is rlm/loop.py:RLMOuterScaffoldLoop driven by ``ooo rlm``).
    Skipped by default because it spawns real Hermes via HermesCliRuntime.
    Enable with OUROBOROS_HERMES_LIVE=1.
    """

    @pytest.mark.asyncio
    async def test_decompose_ac_with_real_hermes(self) -> None:
        import os
        from pathlib import Path

        if os.environ.get("OUROBOROS_HERMES_LIVE") != "1":
            pytest.skip("Set OUROBOROS_HERMES_LIVE=1 to run live Hermes integration")

        from ouroboros.execution.decomposition import (
            MAX_CHILDREN,
            MIN_CHILDREN,
            decompose_ac,
        )
        from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime
        from ouroboros.providers.base import LLMAdapter

        class _NoopLLMAdapter(LLMAdapter):
            async def complete(self, *args, **kwargs):
                raise AssertionError(
                    "LLM should not be reached when Hermes returns structured output",
                )

        hermes = HermesCliRuntime(cwd=str(Path.cwd()))
        result = await decompose_ac(
            ac_content=(
                "Implement a CLI command that outputs the current time in ISO 8601 format."
            ),
            ac_id="ac_live_smoke_001",
            execution_id="exec_live_smoke",
            depth=0,
            llm_adapter=_NoopLLMAdapter(),
            discover_insights=(
                "The CLI uses typer. Time should respect the user's local timezone."
            ),
            hermes_runtime=hermes,
            parent_call_id=None,
            call_id="rlm_call_live_smoke",
            rlm_node_id="rlm_node_live_smoke",
        )

        assert result.is_ok, f"decompose_ac failed: {result.error}"
        decomposition = result.value
        assert MIN_CHILDREN <= len(decomposition.child_acs) <= MAX_CHILDREN
        assert decomposition.hermes_subcall is not None
        assert decomposition.hermes_subcall.completion.strip()
