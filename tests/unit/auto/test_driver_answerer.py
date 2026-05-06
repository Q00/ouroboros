from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswerSource
from ouroboros.auto.driver_answerer import DriverAutoAnswerer, classify_interview_answer_risk
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.state import AutoBrakeMode
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, UsageInfo


class FakeAdapter:
    def __init__(self, content: str = "Use the existing project conventions.") -> None:
        self.content = content
        self.prompts: list[str] = []

    async def complete(self, messages, config):  # noqa: ANN001
        self.prompts.append(messages[-1].content)
        return Result.ok(
            CompletionResponse(
                content=self.content,
                model="fake",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def test_classifies_blocker_questions_as_risky() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=FakeAdapter())
    scaffold = answerer.baseline.answer("Which production credentials should we use?", ledger)

    assert classify_interview_answer_risk("Which production credentials should we use?", scaffold)


@pytest.mark.asyncio
async def test_driver_answerer_brake_off_answers_risky_question() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    adapter = FakeAdapter(
        "Assumption: use a placeholder secret reference, never a real credential."
    )
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which production credentials should we use?", ledger)

    assert answer.source == AutoAnswerSource.DRIVER
    assert answer.blocker is None
    assert "driver=codex" in answer.text
    assert "brake=off" in answer.text
    assert "risk=" in answer.text
    assert adapter.prompts


@pytest.mark.asyncio
async def test_driver_answerer_ledger_updates_mirror_driver_answer() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    adapter = FakeAdapter("Use Typer and verify with pytest.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.ledger_updates
    assert all(entry.value == answer.text for _section, entry in answer.ledger_updates)
    assert {entry.source.value for _section, entry in answer.ledger_updates} == {"inference"}
    assert any("driver:codex" in entry.evidence for _section, entry in answer.ledger_updates)


@pytest.mark.asyncio
async def test_driver_answerer_constructs_adapter_with_session_cwd(monkeypatch, tmp_path) -> None:
    from ouroboros.auto import driver_answerer as module

    captured: dict[str, object] = {}
    adapter = FakeAdapter("Use the checked-out project conventions.")

    def fake_create_llm_adapter(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return adapter

    monkeypatch.setattr(module, "create_llm_adapter", fake_create_llm_adapter)
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, cwd=tmp_path)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.source == AutoAnswerSource.DRIVER
    assert captured["cwd"] == tmp_path


@pytest.mark.asyncio
async def test_driver_answerer_brake_on_gates_risky_question() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    adapter = FakeAdapter("This should not be called")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.ON, adapter=adapter)

    answer = await answerer.answer("Which production credentials should we use?", ledger)

    assert answer.blocker is not None
    assert "requires approval" in answer.blocker.reason
    assert adapter.prompts == []
