from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswerSource
from ouroboros.auto.driver_answerer import (
    DriverAutoAnswerer,
    classify_driver_answer_text_risk,
    classify_interview_answer_risk,
)
from ouroboros.auto.ledger import LedgerSource, SeedDraftLedger
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


def test_classifies_actual_driver_answer_text_risk() -> None:
    assert (
        classify_driver_answer_text_risk(
            "Use API key sk-1234567890abcdef1234567890abcdef for the service."
        )
        == "actual answer contains secret or credential"
    )
    assert (
        classify_driver_answer_text_risk("Delete the production database to reset customer state.")
        == "actual answer recommends destructive production action"
    )
    assert classify_driver_answer_text_risk("Use a placeholder secret reference.") is None


def test_answer_text_risk_detects_plain_bearer_and_jwt_tokens() -> None:
    assert (
        classify_driver_answer_text_risk(
            "Set Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        )
        == "actual answer contains secret or credential"
    )
    assert (
        classify_driver_answer_text_risk(
            "Use token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        == "actual answer contains secret or credential"
    )


def test_answer_text_risk_allows_benign_customer_facing_content_edits() -> None:
    assert classify_driver_answer_text_risk("Remove the customer-facing banner copy.") is None
    assert classify_driver_answer_text_risk("Delete the customer help text from the FAQ.") is None
    assert classify_driver_answer_text_risk("The password is generated during setup.") is None


def test_answer_text_risk_does_not_let_placeholder_words_bypass_secrets() -> None:
    assert (
        classify_driver_answer_text_risk(
            "Use this placeholder password: supersecretproductionpassword123."
        )
        == "actual answer contains secret or credential"
    )
    assert (
        classify_driver_answer_text_risk(
            "Example token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcm9kIn0.abc123456789xyz"
        )
        == "actual answer contains secret or credential"
    )


def test_answer_text_risk_detects_env_var_and_dsn_secret_shapes() -> None:
    assert (
        classify_driver_answer_text_risk("Set OPENAI_API_KEY=fakekeyvalue1234567890 in env.")
        == "actual answer contains secret or credential"
    )
    assert (
        classify_driver_answer_text_risk("Use AWS_SECRET_ACCESS_KEY=fakesecretvalue1234567890.")
        == "actual answer contains secret or credential"
    )
    assert (
        classify_driver_answer_text_risk(
            "Configure DATABASE_URL=postgres://demo:fakedbpassword1234@db.example/app."
        )
        == "actual answer contains secret or credential"
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
    assert answer.metadata.risk == "destructive or financial/production choice"
    assert answer.metadata.confidence == answer.confidence
    assert answer.metadata.provenance == (
        "driver:codex",
        "brake:off",
        "scaffold_source:conservative_default",
    )
    assert adapter.prompts


@pytest.mark.asyncio
async def test_driver_answerer_preserves_scaffold_ledger_values() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    adapter = FakeAdapter("Use Typer and verify with pytest.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.ledger_updates
    assert all(entry.value != answer.text for _section, entry in answer.ledger_updates)
    assert {entry.source.value for _section, entry in answer.ledger_updates} == {"inference"}
    assert any("driver:codex" in entry.evidence for _section, entry in answer.ledger_updates)
    assert any("Driver answer was:" in entry.rationale for _section, entry in answer.ledger_updates)


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
    assert captured["allowed_tools"] == []


@pytest.mark.asyncio
async def test_hermes_driver_does_not_request_unsupported_tool_envelope(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto import driver_answerer as module

    captured: dict[str, object] = {}
    adapter = FakeAdapter("Use the checked-out project conventions.")

    def fake_create_llm_adapter(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return adapter

    monkeypatch.setattr(module, "create_llm_adapter", fake_create_llm_adapter)
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    answerer = DriverAutoAnswerer(backend="hermes", brake=AutoBrakeMode.OFF, cwd=tmp_path)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.source == AutoAnswerSource.DRIVER
    assert captured["allowed_tools"] is None


@pytest.mark.asyncio
async def test_driver_answerer_risky_brake_off_records_active_risk() -> None:
    from ouroboros.auto.ledger import LedgerStatus

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
    assert answer.metadata.risk == "destructive or financial/production choice"
    assert answer.metadata.confidence == 1.0
    assert answer.metadata.provenance == (
        "driver:codex",
        "brake:on",
        "scaffold_source:conservative_default",
    )
    assert adapter.prompts == []


@pytest.mark.asyncio
async def test_driver_answerer_brake_on_gates_risky_actual_answer() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    adapter = FakeAdapter("Use password: supersecretproductionpassword123.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.ON, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.source == AutoAnswerSource.BLOCKER
    assert answer.blocker is not None
    assert "selected-driver response requires approval" in answer.blocker.reason
    assert answer.metadata.risk == "actual answer contains secret or credential"
    assert answer.metadata.provenance == (
        "driver:codex",
        "brake:on",
        "scaffold_source:existing_convention",
        "answer_risk:actual answer contains secret or credential",
    )
    assert adapter.prompts


@pytest.mark.asyncio
async def test_driver_answerer_brake_off_records_risky_actual_answer() -> None:
    ledger = SeedDraftLedger.from_goal("Build a CLI")
    adapter = FakeAdapter("Run DROP DATABASE against production before starting.")
    answerer = DriverAutoAnswerer(backend="codex", brake=AutoBrakeMode.OFF, adapter=adapter)

    answer = await answerer.answer("Which runtime and framework should be used?", ledger)

    assert answer.source == AutoAnswerSource.DRIVER
    assert answer.blocker is None
    assert answer.metadata.risk == "actual answer recommends destructive production action"
    assert (
        "answer_risk:actual answer recommends destructive production action"
        in answer.metadata.provenance
    )
    risks = [
        entry
        for _section, entry in answer.ledger_updates
        if entry.key.startswith("risk.auto_driver")
    ]
    assert risks
    assert risks[0].source == LedgerSource.ASSUMPTION
    assert "answer_risk:actual answer recommends destructive production action" in risks[0].evidence
