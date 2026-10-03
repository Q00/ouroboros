"""The evidence turn's list order, prompt, reply parser and relevance check."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ouroboros.config.models import EvidenceCallOrderingWeights, ExecutionConfig
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_ledger import RecordedCall, build_call_ledger
from ouroboros.orchestrator.evidence.call_ordering import (
    call_features,
    changed_files_from_observations,
    order_calls,
)
from ouroboros.orchestrator.evidence.cited_evidence import Citations
from ouroboros.orchestrator.evidence.evidence_turn import (
    REPLY_NOT_NUMBERS,
    REPLY_UNPARSEABLE,
    TURN_RAN_A_TOOL,
    EvidenceTurnSettings,
    ReplayPermission,
    build_cited_evidence,
    parse_citations,
    render_evidence_turn_prompt,
)
from ouroboros.orchestrator.evidence.relevance_veto import (
    NOT_RELEVANT,
    RELEVANT,
    UNAVAILABLE,
    UNDECIDED,
    judge_relevance,
)
from tests.unit.orchestrator.evidence.test_cited_evidence import codex_call, transcript

FIELDS = ("tests_passed", "commands_run")


def _ledger(messages: tuple[AgentMessage, ...], workspace: Path) -> tuple[RecordedCall, ...]:
    return build_call_ledger(messages, task_cwd=str(workspace), is_error_is_exit_verdict=False)


def _messages() -> tuple[AgentMessage, ...]:
    return transcript(
        codex_call("ls -la", "item_1", 0),
        codex_call("pytest -q pkg/mod.py", "item_2", 1),
        codex_call("git diff --check", "item_3", 0),
        codex_call("pytest -q pkg/mod.py", "item_4", 0),
        codex_call("pytest -q other", "item_5", 0),
    )


class TestOrdering:
    def test_order_reads_structural_features_only(self, tmp_path: Path) -> None:
        messages = _messages()
        ledger = _ledger(messages, tmp_path)
        ordered = order_calls(
            ledger, messages=messages, task_cwd=str(tmp_path), weights=EvidenceCallOrderingWeights()
        )
        # The latest passing run of a test of the changed file comes first;
        # the listing and lint commands (no verification) come last.
        assert ordered[0].number == 4
        assert [call.number for call in ordered][-2:] == [1, 3]
        # Equal scores keep transcript order: the failed earlier run of the
        # changed file's test (2) ties with the passing run of another test (5).
        assert [call.number for call in ordered][1:3] == [2, 5]
        features = call_features(
            ledger[3],
            ledger=ledger,
            messages=messages,
            changed_files=changed_files_from_observations(messages),
            task_cwd=str(tmp_path),
        )
        assert features.verification and features.exercises_patch
        assert features.latest_of_command and features.passed and features.after_last_edit

    def test_order_is_deterministic(self, tmp_path: Path) -> None:
        messages = _messages()
        ledger = _ledger(messages, tmp_path)
        weights = EvidenceCallOrderingWeights()
        first = order_calls(ledger, messages=messages, task_cwd=str(tmp_path), weights=weights)
        second = order_calls(
            tuple(reversed(ledger)), messages=messages, task_cwd=str(tmp_path), weights=weights
        )
        assert first == second

    def test_zero_weights_keep_transcript_order(self, tmp_path: Path) -> None:
        messages = _messages()
        zero = EvidenceCallOrderingWeights(
            verification=0,
            exercises_patch=0,
            latest_of_command=0,
            passed=0,
            after_last_edit=0,
            replayable=0,
        )
        ordered = order_calls(
            _ledger(messages, tmp_path), messages=messages, task_cwd=str(tmp_path), weights=zero
        )
        assert [call.number for call in ordered] == [1, 2, 3, 4, 5]

    def test_weights_live_in_one_config_object_with_defaults(self) -> None:
        config = ExecutionConfig()
        assert config.evidence_relevance_veto is True
        assert config.evidence_call_ordering == EvidenceCallOrderingWeights()
        tuned = ExecutionConfig(evidence_call_ordering={"verification": 9.5})
        assert tuned.evidence_call_ordering.verification == 9.5


class TestPromptAndReply:
    def test_prompt_lists_numbers_results_and_commands(self, tmp_path: Path) -> None:
        messages = _messages()
        ledger = _ledger(messages, tmp_path)
        prompt = render_evidence_turn_prompt(ac_content="AC text", ordered=ledger, fields=FIELDS)
        assert prompt.startswith("[EVIDENCE TURN")
        assert "[2] exit 1 :: /bin/bash -lc 'pytest -q pkg/mod.py'" in prompt
        assert '{"tests_passed": [<numbers>], "commands_run": [<numbers>]}' in prompt
        assert "—" not in prompt

    def test_numbers_are_parsed(self) -> None:
        citations, error = parse_citations(
            '```json\n{"tests_passed": [4], "commands_run": [4, 3]}\n```', FIELDS
        )
        assert error is None
        assert citations == Citations(tests_passed=(4,), commands_run=(4, 3))

    def test_a_missing_field_cites_nothing(self) -> None:
        citations, _error = parse_citations('{"commands_run": [1]}', FIELDS)
        assert citations == Citations(tests_passed=(), commands_run=(1,))

    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            ('{"tests_passed": ["pytest -q pkg"]}', REPLY_NOT_NUMBERS),
            ('{"tests_passed": [true]}', REPLY_NOT_NUMBERS),
            ('{"tests_passed": 4}', REPLY_NOT_NUMBERS),
            ("I ran call 4.", REPLY_UNPARSEABLE),
        ],
    )
    def test_anything_but_numbers_is_unusable(self, reply: str, reason: str) -> None:
        assert parse_citations(reply, FIELDS) == (None, reason)


class _Judge:
    def __init__(self, answers: Mapping[int, str] | Exception) -> None:
        self.answers = answers
        self.seen: list[int] = []

    async def __call__(self, *, criterion: str, calls: Sequence[RecordedCall]) -> Mapping[int, str]:
        del criterion
        self.seen = [call.number for call in calls]
        if isinstance(self.answers, Exception):
            raise self.answers
        return self.answers


class TestRelevanceCheck:
    @pytest.mark.asyncio
    async def test_only_an_explicit_not_relevant_vetoes(self, tmp_path: Path) -> None:
        ledger = _ledger(_messages(), tmp_path)
        decisions = await judge_relevance(
            _Judge({4: NOT_RELEVANT, 5: RELEVANT}),
            criterion="AC",
            calls=(ledger[3], ledger[4], ledger[2]),
        )
        assert [(d.number, d.relevant, d.status) for d in decisions] == [
            (4, False, NOT_RELEVANT),
            (5, True, RELEVANT),
            (3, None, UNDECIDED),
        ]

    @pytest.mark.asyncio
    async def test_a_failing_check_withholds_nothing(self, tmp_path: Path) -> None:
        ledger = _ledger(_messages(), tmp_path)
        decisions = await judge_relevance(
            _Judge(RuntimeError("provider down")), criterion="AC", calls=(ledger[3],)
        )
        assert [(d.relevant, d.status) for d in decisions] == [(None, UNAVAILABLE)]

    @pytest.mark.asyncio
    async def test_the_check_sees_only_cited_passing_calls_and_is_recorded(
        self, tmp_path: Path
    ) -> None:
        judge = _Judge({4: RELEVANT})
        cited = await build_cited_evidence(
            reply='{"tests_passed": [2, 4, 99], "commands_run": [4]}',
            reply_messages=(AgentMessage(type="result", content="{}"),),
            primary_messages=_messages(),
            ac_content="AC",
            fields=FIELDS,
            task_cwd=str(tmp_path),
            runtime_backend="codex_cli",
            settings=EvidenceTurnSettings(),
            judge=judge,
            replay=ReplayPermission(allowed=False),
        )
        assert judge.seen == [4]
        assert [(d.number, d.status) for d in cited.relevance] == [(4, RELEVANT)]
        assert cited.is_error_is_exit_verdict is False

    @pytest.mark.asyncio
    async def test_the_check_is_skipped_when_switched_off(self, tmp_path: Path) -> None:
        judge = _Judge({4: NOT_RELEVANT})
        cited = await build_cited_evidence(
            reply='{"tests_passed": [4], "commands_run": [4]}',
            reply_messages=(),
            primary_messages=_messages(),
            ac_content="AC",
            fields=FIELDS,
            task_cwd=str(tmp_path),
            runtime_backend="claude",
            settings=EvidenceTurnSettings(relevance_veto=False),
            judge=judge,
            replay=ReplayPermission(allowed=False),
        )
        assert judge.seen == []
        assert cited.relevance == ()
        assert cited.is_error_is_exit_verdict is True

    @pytest.mark.asyncio
    async def test_a_reply_from_a_turn_that_ran_a_tool_is_unusable(self, tmp_path: Path) -> None:
        cited = await build_cited_evidence(
            reply='{"tests_passed": [4], "commands_run": [4]}',
            reply_messages=codex_call("pytest -q pkg/mod.py", "item_9", 0),
            primary_messages=_messages(),
            ac_content="AC",
            fields=FIELDS,
            task_cwd=str(tmp_path),
            runtime_backend="codex_cli",
            settings=EvidenceTurnSettings(),
            judge=None,
            replay=ReplayPermission(allowed=False),
        )
        assert cited.citations is None
        assert cited.reply_error == TURN_RAN_A_TOOL
