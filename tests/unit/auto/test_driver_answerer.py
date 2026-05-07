from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswerContext, AutoAnswerSource
from ouroboros.auto.driver_answerer import (
    DriverAutoAnswerer,
    _ledger_updates_for,
    classify_interview_answer_risk,
)
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
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


class RaisingAdapter:
    async def complete(self, messages, config):  # noqa: ANN001, ARG002
        raise RuntimeError("backend crashed")


class ErrorAdapter:
    async def complete(self, messages, config):  # noqa: ANN001, ARG002
        from ouroboros.core.errors import ProviderError

        return Result.err(
            ProviderError(
                "Hermes CLI not found at hermes. Install Hermes or configure OUROBOROS_HERMES_CLI_PATH.",
                provider="hermes_cli",
            )
        )


def test_classifies_blocker_questions_as_risky() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=FakeAdapter())
    scaffold = answerer.baseline.answer("Which production credentials should we use?", ledger)

    assert classify_interview_answer_risk("Which production credentials should we use?", scaffold)


def test_classifies_routine_non_goal_gap_as_not_risky() -> None:
    assert (
        classify_interview_answer_risk("What non-goals should explicitly remain out of scope?")
        is None
    )


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
async def test_driver_answerer_records_driver_text_for_unsupported_scaffold_values() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    adapter = FakeAdapter("Use Typer and verify with pytest.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.ledger_updates
    assert {entry.source for _section, entry in answer.ledger_updates}
    assert any("driver:codex" in entry.evidence for _section, entry in answer.ledger_updates)
    assert any(entry.value == answer.text for _section, entry in answer.ledger_updates)
    assert any("Scaffold was:" in entry.rationale for _section, entry in answer.ledger_updates)


@pytest.mark.asyncio
async def test_driver_answerer_preserves_scaffold_provenance() -> None:
    from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
    from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus

    scaffold = AutoAnswer(
        text="Assume no external services.",
        source=AutoAnswerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        ledger_updates=[
            (
                "non_goals",
                LedgerEntry(
                    key="non_goals.auto_mvp",
                    value="External services are out of scope.",
                    source=LedgerSource.NON_GOAL,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                ),
            )
        ],
    )

    updates = _ledger_updates_for(
        scaffold,
        driver_text="[driver=codex] Keep the MVP local.",
        risk=None,
        backend="codex",
    )

    assert updates[0][1].source == LedgerSource.NON_GOAL


@pytest.mark.asyncio
async def test_driver_answerer_preserves_confirmed_scaffold_status() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python package managed by uv"},
        evidence={"runtime_context": ["pyproject.toml"]},
    )
    adapter = FakeAdapter("Use the existing Python/uv runtime.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger, context)

    runtime_updates = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert runtime_updates
    assert any(entry.status is LedgerStatus.CONFIRMED for entry in runtime_updates)


@pytest.mark.asyncio
async def test_driver_answerer_keeps_unsupported_scaffold_contract_open() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python package managed by uv"},
        evidence={"runtime_context": ["pyproject.toml"]},
    )
    adapter = FakeAdapter("Use Rust and Cargo for the implementation.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger, context)

    runtime_updates = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert runtime_updates
    assert all(entry.status is LedgerStatus.WEAK for entry in runtime_updates)
    assert any("Use Rust and Cargo" in entry.value for entry in runtime_updates)
    assert any(
        "Scaffold was: Python package managed by uv" in entry.rationale for entry in runtime_updates
    )


@pytest.mark.asyncio
async def test_driver_answerer_rejects_partial_overlap_runtime_contradiction() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    context = AutoAnswerContext(
        repo_facts={"runtime_context": "Python package managed by uv"},
        evidence={"runtime_context": ["pyproject.toml"]},
    )
    adapter = FakeAdapter("Use Python with Poetry for dependency management.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger, context)

    runtime_updates = [
        entry for section, entry in answer.ledger_updates if section == "runtime_context"
    ]
    assert runtime_updates
    assert all(entry.status is LedgerStatus.WEAK for entry in runtime_updates)
    assert any("Poetry" in entry.value for entry in runtime_updates)
    assert all("uv" not in entry.value for entry in runtime_updates)


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
async def test_driver_answerer_adapter_creation_exception_becomes_blocker(monkeypatch) -> None:
    from ouroboros.auto import driver_answerer as module

    def fake_create_llm_adapter(**kwargs):  # noqa: ANN003, ANN202, ARG001
        raise FileNotFoundError("codex missing")

    monkeypatch.setattr(module, "create_llm_adapter", fake_create_llm_adapter)
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF)

    answer = await answerer.answer("Which runtime should be used?", ledger)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "selected driver codex failed to answer" in answer.blocker.reason
    assert "FileNotFoundError" in answer.blocker.reason


@pytest.mark.asyncio
async def test_driver_answerer_complete_exception_becomes_blocker() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    answerer = DriverAutoAnswerer(
        backend="codex",
        brake=AutoBrakeMode.OFF,
        adapter=RaisingAdapter(),  # type: ignore[arg-type]
    )

    answer = await answerer.answer("Which runtime should be used?", ledger)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "selected driver codex failed to answer" in answer.blocker.reason
    assert "RuntimeError" in answer.blocker.reason


@pytest.mark.asyncio
async def test_driver_answerer_missing_hermes_becomes_recoverable_blocker() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    answerer = DriverAutoAnswerer(
        backend="hermes",
        brake=AutoBrakeMode.OFF,
        adapter=ErrorAdapter(),  # type: ignore[arg-type]
    )

    answer = await answerer.answer("Which runtime should be used?", ledger)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "selected driver hermes failed to answer" in answer.blocker.reason
    assert "Install Hermes" in answer.blocker.reason


@pytest.mark.asyncio
async def test_driver_answerer_risky_brake_off_records_active_risk() -> None:
    from ouroboros.auto.ledger import LedgerSource, LedgerStatus

    ledger = SeedDraftLedger.from_goal("Deploy a service")
    adapter = FakeAdapter("Use a placeholder secret reference, never a real credential.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which production credentials should we use?", ledger)

    risks = [
        entry
        for _section, entry in answer.ledger_updates
        if entry.key.startswith("risk.auto_driver")
    ]
    assert risks
    assert risks[0].source == LedgerSource.ASSUMPTION
    assert risks[0].status == LedgerStatus.INFERRED


@pytest.mark.asyncio
async def test_driver_answerer_brake_on_gates_risky_question() -> None:
    ledger = SeedDraftLedger.from_goal("Deploy a service")
    adapter = FakeAdapter("This should not be called")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.ON, adapter=adapter)

    answer = await answerer.answer("Which production credentials should we use?", ledger)

    assert answer.blocker is not None
    assert "requires approval" in answer.blocker.reason
    assert adapter.prompts == []
