from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.evidence.call_ledger import CallResult, build_call_ledger
from ouroboros.orchestrator.evidence.evidence_turn import render_evidence_turn_prompt
from ouroboros.orchestrator.evidence.relevance_veto import LLMRelevanceJudge
from tests.unit.orchestrator.evidence.test_cited_evidence import FABRICATION, codex_call, verify


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "API_TOKEN=sentinel_private_value pytest -q",
        'PASSWORD="sentinel_private_value" pytest -q',
        "PASSWORD='sentinel_private_value with spaces' pytest -q",
        "curl -H 'Authorization: Bearer sentinel_private_value' https://example.test",
        "curl --user alice:sentinel_private_value https://example.test",
        'curl --password "sentinel_private_value with spaces" https://example.test',
        "curl --password=sentinel_private_value https://example.test",
        "curl -ualice:sentinel_private_value https://example.test",
        "curl --proxy-user alice:sentinel_private_value https://example.test",
    ],
)
async def test_model_prompts_do_not_export_command_secrets(tmp_path: Path, command: str) -> None:
    messages = codex_call(command, "call-1", 0)
    ledger = build_call_ledger(messages, task_cwd=str(tmp_path), is_error_is_exit_verdict=False)
    prompt = render_evidence_turn_prompt(
        ac_content="Run the test", ordered=ledger, fields=("tests_passed",)
    )
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content='{"1":"relevant"}')))
    )
    await LLMRelevanceJudge(adapter=adapter)(criterion="Run the test", calls=ledger)
    sent = adapter.complete.call_args.args[0][0].content
    assert "sentinel_private_value" not in prompt
    assert "sentinel_private_value" not in sent
    assert "sentinel_private_value" in ledger[0].command


def test_mapper_failure_cannot_become_a_passing_citation(tmp_path: Path) -> None:
    runtime = CodexCliRuntime(cli_path="codex", cwd=str(tmp_path))
    item = {"id": "item_1", "type": "command_execution", "command": "pytest -q"}
    messages = runtime._convert_event({"type": "item.started", "item": item}, current_handle=None)
    messages += runtime._convert_event(
        {"type": "item.completed", "item": {**item, "exit_code": 0, "status": "failed"}},
        current_handle=None,
    )
    ledger = build_call_ledger(messages, task_cwd=str(tmp_path), is_error_is_exit_verdict=False)
    assert ledger[0].result is CallResult.FAILED
    verdict = verify(tmp_path, tuple(messages), tests_passed=(1,))
    assert verdict.failure_class == FABRICATION
