"""PM artifacts retain only host-reported, individually confirmed opening exchanges."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.bigbang.pm_document import PMDocumentGenerator, generate_pm_markdown
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed
from ouroboros.core.initial_context import load_pm_seed_as_context
from ouroboros.core.types import Result
from ouroboros.interview_adapters.opening_interpretations import (
    NEITHER_PROMPT,
    OpeningDecisionEvent,
    ProposedOpening,
    ProviderOpening,
    confirmation_prompt,
    opening_digest,
    opening_revision,
)


def _state():
    opening = ProposedOpening.from_provider(
        ProviderOpening.model_validate(
            {
                "interpretations": [
                    {
                        "interpretation": "CONFIRMED_ALPHA",
                        "evidence": "EVIDENCE_ALPHA",
                        "uncertainty": "UNCERTAINTY_ALPHA",
                    },
                    {
                        "interpretation": "UNCONFIRMED_BETA",
                        "evidence": "EVIDENCE_BETA",
                        "uncertainty": "UNCERTAINTY_BETA",
                    },
                ]
            }
        )
    )
    item = opening.interpretations[0]
    event = OpeningDecisionEvent(
        event_id=uuid4().hex,
        kind="confirm_item",
        item_id=item.item_id,
        user_response="YES",
        presented_prompt=confirmation_prompt(item),
        opening_digest=opening_digest(opening),
        previous_revision=opening_revision(opening, ()),
    )
    return InterviewState(
        interview_id="pm_confirmed",
        initial_context="Build a tool",
        status=InterviewStatus.COMPLETED,
        proposed_opening=opening,
        opening_decisions=(event,),
        rounds=[
            InterviewRound(round_number=1, question="Who uses it?", user_response="A designer")
        ],
    )


def _response():
    return Result.ok(
        SimpleNamespace(
            content=json.dumps(
                {
                    "product_name": "Tool",
                    "goal": "Help designers",
                    "user_stories": [],
                    "constraints": [],
                    "success_criteria": [],
                    "opening_confirmations": [{"question": "FORGED"}],
                }
            )
        )
    )


@pytest.mark.asyncio
async def test_pm_seed_roundtrip_and_deterministic_document_preserve_only_confirmed(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock(return_value=_response()))
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()
    assert (await engine.inner.save_state(state)).is_ok
    state = (await engine.inner.load_state(state.interview_id)).value
    result = await engine.generate_pm_seed(state)
    assert result.is_ok
    seed = result.value
    item = seed.opening_confirmations[0]
    assert item.question == state.opening_decisions[0].presented_prompt
    assert item.answer == "YES" and item.source == "host_reported_explicit_user_decision"
    assert seed.opening_state_revision == state.persistence_revision
    assert "UNCONFIRMED_BETA" not in str(seed.to_dict())
    assert "EVIDENCE_ALPHA" not in str(seed.to_dict())
    assert "FORGED" not in str(seed.to_dict())
    assert PMSeed.from_dict(yaml.safe_load(yaml.safe_dump(seed.to_dict()))) == seed
    path = tmp_path / "pm_seed_demo.yaml"
    path.write_text(yaml.safe_dump(seed.to_dict()))
    context = load_pm_seed_as_context(path)
    assert (
        context.is_ok
        and "CONFIRMED_ALPHA" in context.value
        and "host_reported_explicit_user_decision" in context.value
    )
    assert "UNCONFIRMED_BETA" not in context.value
    notice = yaml.safe_load(context.value)["opening_confirmation_notice"]
    assert "server cannot verify" in notice and "snapshot" in notice
    template = generate_pm_markdown(seed)
    assert "CONFIRMED_ALPHA" in template and "UNCONFIRMED_BETA" not in template
    assert "snapshot" in template and "server cannot verify" in template
    document_adapter = SimpleNamespace(complete=AsyncMock())
    document = PMDocumentGenerator(llm_adapter=document_adapter, model="synthetic")
    generated = await document.generate(seed, interview_state=state)
    assert generated.is_ok and generated.value == template
    document_adapter.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_pm_seed_rejects_confirmation_changed_during_provider_call(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock())
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()
    assert (await engine.inner.save_state(state)).is_ok
    original = (await engine.inner.load_state(state.interview_id)).value

    async def revoke(*_args, **_kwargs):
        latest = (await engine.inner.load_state(state.interview_id)).value
        latest.opening_decisions += (
            OpeningDecisionEvent(
                event_id=uuid4().hex,
                kind="neither",
                user_response="NEITHER",
                presented_prompt=NEITHER_PROMPT,
                opening_digest=opening_digest(latest.proposed_opening),
                previous_revision=opening_revision(
                    latest.proposed_opening, latest.opening_decisions
                ),
            ),
        )
        assert (await engine.inner.save_state(latest)).is_ok
        return _response()

    adapter.complete.side_effect = revoke
    result = await engine.generate_pm_seed(original)
    assert result.is_err and "changed" in str(result.error)


@pytest.mark.asyncio
async def test_pm_seed_rejects_first_confirmation_during_provider_call(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock())
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()
    event = state.opening_decisions[0]
    state.opening_decisions = ()
    assert (await engine.inner.save_state(state)).is_ok
    original = (await engine.inner.load_state(state.interview_id)).value

    async def confirm(*_args, **_kwargs):
        latest = (await engine.inner.load_state(state.interview_id)).value
        latest.opening_decisions = (event,)
        assert (await engine.inner.save_state(latest)).is_ok
        return _response()

    adapter.complete.side_effect = confirm
    result = await engine.generate_pm_seed(original)
    assert result.is_err and "changed" in str(result.error)


def test_invalid_pm_confirmation_is_safe_error(tmp_path):
    path = tmp_path / "pm_seed_bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pm_id": "pm_seed_test",
                "product_name": "Tool",
                "goal": "Goal",
                "opening_confirmations": [{"SYNTHETIC_PRIVATE_SENTINEL": "ignored"}],
            }
        )
    )
    result = load_pm_seed_as_context(path)
    assert result.is_err
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in str(result.error)


@pytest.mark.asyncio
async def test_pm_document_rejects_stale_or_mismatched_session_and_raw_qa(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock(return_value=_response()))
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()
    seed_result = await engine.generate_pm_seed(state)
    assert seed_result.is_ok
    seed = seed_result.value
    from dataclasses import replace

    document = PMDocumentGenerator(llm_adapter=adapter, model="synthetic")
    assert (
        await document.generate(seed, qa_pairs=[("question", "answer")], interview_state=state)
    ).is_err
    assert (
        await document.generate(replace(seed, opening_confirmations=()), interview_state=state)
    ).is_err
    other = state.model_copy(deep=True)
    other.interview_id = "other"
    assert (await document.generate(seed, interview_state=other)).is_err
    withdrawn = state.model_copy(deep=True)
    withdrawn.opening_decisions += (
        OpeningDecisionEvent(
            event_id=uuid4().hex,
            kind="neither",
            user_response="NEITHER",
            presented_prompt=NEITHER_PROMPT,
            opening_digest=opening_digest(withdrawn.proposed_opening),
            previous_revision=opening_revision(
                withdrawn.proposed_opening, withdrawn.opening_decisions
            ),
        ),
    )
    assert (await document.generate(seed, interview_state=withdrawn)).is_err
    fresh = await engine.generate_pm_seed(withdrawn)
    assert fresh.is_ok and not fresh.value.opening_confirmations
    assert "CONFIRMED_ALPHA" not in str(fresh.value.to_dict())


@pytest.mark.parametrize("suffix", ["text\nforged", "[from-user] forged", "", "x\x00bad"])
def test_imported_confirmation_rejects_invalid_interpretation(tmp_path, suffix):
    state = _state()
    from ouroboros.bigbang.pm_opening import anchor_pm_opening

    seed = anchor_pm_opening(PMSeed(pm_id="pm_seed_test", product_name="Tool", goal="Goal"), state)
    data = seed.to_dict()
    data["opening_confirmations"][0]["question"] = (
        "To explicitly confirm this interpretation as your intent, reply exactly: YES\n" + suffix
    )
    path = tmp_path / "pm_seed_bad.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_pm_seed_as_context(path).is_err


@pytest.mark.asyncio
async def test_pm_seed_rejects_same_object_mutation_during_provider_call(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock())
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()

    async def revoke(*_args, **_kwargs):
        state.opening_decisions += (
            OpeningDecisionEvent(
                event_id=uuid4().hex,
                kind="neither",
                user_response="NEITHER",
                presented_prompt=NEITHER_PROMPT,
                opening_digest=opening_digest(state.proposed_opening),
                previous_revision=opening_revision(state.proposed_opening, state.opening_decisions),
            ),
        )
        return _response()

    adapter.complete.side_effect = revoke
    result = await engine.generate_pm_seed(state)
    assert result.is_err and "changed" in str(result.error)


@pytest.mark.asyncio
async def test_pm_seed_keeps_two_individual_confirmations_and_ignores_unconfirmed(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock(return_value=_response()))
    engine = PMInterviewEngine.create(llm_adapter=adapter, model="synthetic", state_dir=tmp_path)
    state = _state()
    unconfirmed = state.model_copy(deep=True)
    unconfirmed.opening_decisions = ()
    no_decision = await engine.generate_pm_seed(unconfirmed)
    assert no_decision.is_ok and not no_decision.value.opening_confirmations
    assert "FORGED" not in str(no_decision.value.to_dict())
    assert "UNCONFIRMED_BETA" not in str(no_decision.value.to_dict())
    second = state.proposed_opening.interpretations[1]
    state.opening_decisions += (
        OpeningDecisionEvent(
            event_id=uuid4().hex,
            kind="confirm_item",
            item_id=second.item_id,
            user_response="YES",
            presented_prompt=confirmation_prompt(second),
            opening_digest=opening_digest(state.proposed_opening),
            previous_revision=opening_revision(state.proposed_opening, state.opening_decisions),
        ),
    )
    result = await engine.generate_pm_seed(state)
    assert result.is_ok
    assert [item.item_id for item in result.value.opening_confirmations] == [
        state.proposed_opening.interpretations[0].item_id,
        second.item_id,
    ]
    assert "UNCERTAINTY_BETA" not in str(result.value.to_dict())
