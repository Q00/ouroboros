"""Unanswered interview questions are kept in ``Seed.open_questions``, never in worker input.

The split follows ledger-entry provenance (``LedgerEntry.question`` on an entry
that does not bind), never the text of a line.
"""

from __future__ import annotations

import re

import yaml

from ouroboros.auto.ledger import (
    REPO_FACT_KEY_INFIX,
    REQUESTER_KEY_SUFFIX,
    REQUIRED_SECTIONS,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import (
    ledger_assumptions,
    ledger_open_questions,
    partial_seed_from_evidence,
    synthesize_seed_from_ledger,
    with_ledger_carryover,
)
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.core.seed import Seed
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import render_seed_contract_for_execution
from ouroboros.mcp.tools.seed_handoff import project_worker_safe_seed
from ouroboros.orchestrator.runner import build_system_prompt

GOAL = "Fix HPacker alignment so top and bottom are not reversed."
NOT_FOUND_QUESTION = (
    "Should the fix be limited to HPacker's top/bottom behavior, leaving VPacker unchanged?"
)
UNANSWERED_QUESTION = "Should the legacy alias keep working?"
REQUESTER_QUESTION = "Which release should carry the fix?"
REPO_QUESTION = "Where is the alignment computed?"
NEUTRAL_DEFAULT = "The packing direction for nested packers is unspecified"
# A value that looks like an open-question line but was not produced by an
# interviewer question: it stays an assumption, because routing reads
# provenance, not text.
LOOKALIKE_VALUE = "Open question: this text has no interview provenance"


def _entry(key: str, value: str, **kwargs: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "source": LedgerSource.ASSUMPTION,
        "confidence": 0.5,
        "status": LedgerStatus.DEFAULTED,
    }
    fields.update(kwargs)
    return LedgerEntry(key=key, value=value, **fields)  # type: ignore[arg-type]


def _ledger() -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(GOAL)
    for section in REQUIRED_SECTIONS:
        if section != "goal":
            ledger.add_entry(
                section,
                _entry(f"{section}.safe_default_finalization", f"safe default for {section}"),
            )
    # Investigator not-found entry (``investigator.not_found_answer``).
    ledger.add_entry(
        "constraints",
        _entry(
            "constraints.scope.investigated.1",
            "The repository code does not settle this interview question.",
            question=NOT_FOUND_QUESTION,
        ),
    )
    # Draft kept because the requester gave no answer.
    ledger.add_entry(
        "acceptance_criteria",
        _entry(
            "acceptance_criteria.alias",
            "Keep the legacy alias",
            question=UNANSWERED_QUESTION,
            requester_unanswered=True,
        ),
    )
    # The requester's real answer.
    ledger.add_entry(
        "constraints",
        _entry(
            f"constraints.release{REQUESTER_KEY_SUFFIX}",
            "Ship it in the next patch release",
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.83,
            status=LedgerStatus.CONFIRMED,
            question=REQUESTER_QUESTION,
        ),
    )
    # A checked repository fact.
    ledger.add_entry(
        "runtime_context",
        _entry(
            f"runtime_context.alignment{REPO_FACT_KEY_INFIX}2",
            "offsets.py computes the alignment",
            source=LedgerSource.REPO_FACT,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
            question=REPO_QUESTION,
            evidence=["offsets.py:10-12: def _get_aligned_offsets"],
        ),
    )
    # Neutral defaults no interviewer question produced.
    ledger.add_entry("inputs", _entry("inputs.direction", NEUTRAL_DEFAULT))
    ledger.add_entry("outputs", _entry("outputs.lookalike", LOOKALIKE_VALUE))
    return ledger


def test_open_questions_are_routed_by_entry_provenance() -> None:
    ledger = _ledger()

    assert ledger_open_questions(ledger) == (NOT_FOUND_QUESTION, UNANSWERED_QUESTION)
    assumptions = ledger_assumptions(ledger)
    assert f"Inputs: {NEUTRAL_DEFAULT}" in assumptions
    assert f"Outputs: {LOOKALIKE_VALUE}" in assumptions
    for question in (NOT_FOUND_QUESTION, UNANSWERED_QUESTION, REQUESTER_QUESTION, REPO_QUESTION):
        assert not any(question in line for line in assumptions)


def test_requester_answers_and_repo_facts_are_never_open_questions() -> None:
    seed = synthesize_seed_from_ledger(_ledger())

    assert [item.question for item in seed.requester_answers] == [REQUESTER_QUESTION]
    assert [item.question for item in seed.repo_facts] == [REPO_QUESTION]
    assert REQUESTER_QUESTION not in seed.open_questions
    assert REPO_QUESTION not in seed.open_questions


def test_every_seed_builder_carries_the_open_questions() -> None:
    ledger = _ledger()
    expected = (NOT_FOUND_QUESTION, UNANSWERED_QUESTION)

    assert synthesize_seed_from_ledger(ledger).open_questions == expected
    assert partial_seed_from_evidence(ledger, reason="test").open_questions == expected
    generated = Seed.from_dict(
        {
            "goal": GOAL,
            "ontology_schema": {"name": "Fix", "description": "d"},
            "metadata": {"ambiguity_score": 0.1},
        }
    )
    assert with_ledger_carryover(generated, ledger).open_questions == expected


def test_worker_prompts_exclude_open_questions() -> None:
    seed = synthesize_seed_from_ledger(_ledger())
    assert seed.open_questions

    contract_prompt = render_seed_contract_for_execution(SeedContract.from_seed(seed))
    system_prompt = build_system_prompt(seed, resolved_context_pack_fragment="")
    worker_yaml = project_worker_safe_seed(
        yaml.safe_dump(seed.to_dict(), sort_keys=False)
    ).seed_content

    for rendered in (contract_prompt, system_prompt, worker_yaml):
        for question in seed.open_questions:
            assert question not in rendered
        # Known-source inputs still reach the worker.
        assert REQUESTER_QUESTION in rendered
        assert REPO_QUESTION in rendered
    assert "open_questions" not in yaml.safe_load(worker_yaml)


def test_seed_round_trip_keeps_open_questions_and_loads_older_seeds() -> None:
    seed = synthesize_seed_from_ledger(_ledger())

    data = seed.to_dict()
    assert data["open_questions"] == [NOT_FOUND_QUESTION, UNANSWERED_QUESTION]
    restored = Seed.from_dict(yaml.safe_load(yaml.safe_dump(data)))
    assert restored.open_questions == seed.open_questions

    older = dict(data)
    older.pop("open_questions")
    loaded = Seed.from_dict(older)
    assert loaded.open_questions == ()
    assert "open_questions" not in loaded.to_dict()


def test_auto_result_reports_open_questions() -> None:
    from ouroboros.cli.commands.auto import _print_result
    from ouroboros.cli.formatters import console
    from ouroboros.mcp.tools.auto_handler import _format_result, _result_meta

    questions = (NOT_FOUND_QUESTION, "Is [bold]markup[/bold] kept verbatim?")
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_x",
        phase="complete",
        open_questions=questions,
    )

    assert _result_meta(result)["open_questions"] == list(questions)
    text = _format_result(result)
    with console.capture() as capture:
        _print_result(result, show_ledger=False)
    # Collapse the console's line wrapping before matching.
    cli_output = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capture.get()).split())
    for question in questions:
        assert question in text
        assert question in cli_output


def test_pipeline_result_reads_open_questions_from_the_seed(tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.auto.state import AutoPipelineState

    class _Unused:
        progress_callback = None

    ledger = _ledger()
    state = AutoPipelineState(goal=GOAL, cwd=str(tmp_path))
    pipeline = AutoPipeline(_Unused(), None)  # type: ignore[arg-type]
    generated = Seed.from_dict(
        {
            "goal": GOAL,
            "acceptance_criteria": ["The packer aligns top and bottom as documented."],
            "ontology_schema": {"name": "Fix", "description": "d"},
            "metadata": {"ambiguity_score": 0.1},
        }
    )

    # The real recording path: ledger carryover, acceptance normalization, artifact.
    pipeline._record_generated_seed(state, ledger, generated)
    result = pipeline._result(state, ledger)

    assert result.open_questions == (NOT_FOUND_QUESTION, UNANSWERED_QUESTION)
