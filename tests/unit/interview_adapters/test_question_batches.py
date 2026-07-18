from __future__ import annotations

from pydantic import ValidationError
import pytest

from ouroboros.interview_adapters import (
    PlainTextAnswerCollector,
    QuestionAnswer,
    QuestionBatch,
    QuestionBatchOption,
    QuestionBatchQuestion,
    QuestionHostCapabilities,
    QuestionRenderMode,
    parse_native_question_answers,
    parse_plain_text_question_answers,
    render_question_batch,
)


def _question(
    question_id: str,
    question: str,
    *,
    options: tuple[tuple[str, str], ...] = (),
    multiple: bool = False,
    recommended_answer: str = "Use the smallest safe default",
) -> QuestionBatchQuestion:
    return QuestionBatchQuestion(
        question_id=question_id,
        question=question,
        options=tuple(
            QuestionBatchOption(option_id=option_id, label=label) for option_id, label in options
        ),
        multiple=multiple,
        recommended_answer=recommended_answer,
        escape_hatch="Write a different answer in your own words.",
        source_label="from user goal",
    )


@pytest.fixture
def batch() -> QuestionBatch:
    return QuestionBatch(
        batch_id="opening-decisions",
        questions=(
            _question(
                "audience",
                "Who should use this first?",
                options=(("new_users", "New users"), ("maintainers", "Maintainers")),
                recommended_answer="new_users",
            ),
            _question(
                "platforms",
                "Which platforms are in scope?",
                options=(
                    ("web", "Web"),
                    ("desktop", "Desktop"),
                    ("mobile", "Mobile"),
                ),
                multiple=True,
                recommended_answer="web",
            ),
            _question(
                "success",
                "What would make the first release successful?",
                recommended_answer="A user completes the main task without help.",
            ),
        ),
    )


def test_same_payload_round_trips_through_native_and_plain_text(
    batch: QuestionBatch,
) -> None:
    native = render_question_batch(
        batch,
        QuestionHostCapabilities(native_question_batches=True, max_questions_per_batch=4),
    )[0]
    text = render_question_batch(
        batch,
        QuestionHostCapabilities(native_question_batches=False, max_questions_per_batch=4),
    )[0]

    assert native.mode is QuestionRenderMode.NATIVE
    assert native.text is None
    assert text.mode is QuestionRenderMode.PLAIN_TEXT
    assert text.text is not None
    assert native.payload == text.payload
    assert native.payload.questions == batch.questions

    native_answers = parse_native_question_answers(
        native.payload,
        {
            "audience": "new_users",
            "platforms": ["web", "mobile"],
            "success": "A user completes the main task in under a minute.",
        },
    )
    text_answers = parse_plain_text_question_answers(
        text.payload,
        "1: a / 2: a,c / 3: A user completes the main task in under a minute.",
    )

    assert native_answers == text_answers
    assert native_answers.answers == (
        QuestionAnswer(question_id="audience", selected_option_ids=("new_users",)),
        QuestionAnswer(question_id="platforms", selected_option_ids=("web", "mobile")),
        QuestionAnswer(
            question_id="success",
            free_text="A user completes the main task in under a minute.",
        ),
    )


def test_text_renderer_numbers_questions_and_includes_complete_contract(
    batch: QuestionBatch,
) -> None:
    text = render_question_batch(batch, QuestionHostCapabilities())[0].text

    assert text is not None
    assert "1. Who should use this first? [from user goal]" in text
    assert "   a) New users" in text
    assert "   Recommended: a) New users" in text
    assert "   Other: Write a different answer in your own words." in text
    assert "Reply in this format: 1: a / 2: a,b / 3: <free text>" in text


def test_text_parser_handles_numbers_letters_commas_and_free_input(
    batch: QuestionBatch,
) -> None:
    page = render_question_batch(batch, QuestionHostCapabilities())[0].payload

    answers = parse_plain_text_question_answers(
        page,
        "1: 2\n2: A,c\n3: Keep / export available for audit",
    )

    assert answers.answers == (
        QuestionAnswer(question_id="audience", selected_option_ids=("maintainers",)),
        QuestionAnswer(question_id="platforms", selected_option_ids=("web", "mobile")),
        QuestionAnswer(question_id="success", free_text="Keep / export available for audit"),
    )


def test_text_parser_preserves_free_input_escape_on_choice_question(
    batch: QuestionBatch,
) -> None:
    page = render_question_batch(batch, QuestionHostCapabilities())[0].payload
    answers = parse_plain_text_question_answers(
        page, "1: Teachers in small schools/2: b/3: Fewer support requests"
    )

    assert answers.answers[0] == QuestionAnswer(
        question_id="audience", free_text="Teachers in small schools"
    )


def test_text_collector_reprompts_only_up_to_limit(batch: QuestionBatch) -> None:
    page = render_question_batch(batch, QuestionHostCapabilities())[0].payload
    collector = PlainTextAnswerCollector(page, max_reprompts=2)

    first = collector.submit("a, c")
    second = collector.submit("1: a")
    third = collector.submit("still not numbered")
    fourth = collector.submit("1: a / 2: a,c / 3: Too late")

    assert first.reprompt is not None and first.remaining_reprompts == 1
    assert second.reprompt is not None and second.remaining_reprompts == 0
    assert third.exhausted and third.reprompt is None
    assert fourth.exhausted and fourth.reprompt is None
    assert collector.failed_attempts == 3


def test_text_collector_accepts_a_valid_retry(batch: QuestionBatch) -> None:
    page = render_question_batch(batch, QuestionHostCapabilities())[0].payload
    collector = PlainTextAnswerCollector(page, max_reprompts=2)

    assert not collector.submit("not numbered").accepted
    accepted = collector.submit("1: a / 2: a,c / 3: Ship a useful first workflow")

    assert accepted.accepted and accepted.failed_attempts == 1
    with pytest.raises(RuntimeError, match="already been answered"):
        collector.submit("1: b / 2: b / 3: Replace it")


@pytest.mark.parametrize("native", [True, False])
def test_batch_size_follows_host_capacity_without_changing_questions(native: bool) -> None:
    batch = QuestionBatch(
        batch_id="capacity-check",
        questions=tuple(_question(f"q{index}", f"Question {index}?") for index in range(1, 10)),
    )

    pages = render_question_batch(
        batch,
        QuestionHostCapabilities(native_question_batches=native, max_questions_per_batch=4),
    )

    assert [len(page.payload.questions) for page in pages] == [4, 4, 1]
    assert [page.payload.question_offset for page in pages] == [0, 4, 8]
    assert all(page.payload.page_count == 3 for page in pages)
    assert (
        tuple(question for page in pages for question in page.payload.questions) == batch.questions
    )


def test_contract_validates_structure_without_runtime_identity() -> None:
    question = _question("audience", "Who should use this first?")
    with pytest.raises(ValidationError, match="source_label"):
        QuestionBatchQuestion.model_validate({**question.model_dump(), "source_label": " "})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        QuestionHostCapabilities.model_validate(
            {
                "native_question_batches": True,
                "max_questions_per_batch": 4,
                "runtime_name": "some-host",
            }
        )


def test_text_parser_rejects_incomplete_duplicate_and_unknown_numbers(
    batch: QuestionBatch,
) -> None:
    page = render_question_batch(batch, QuestionHostCapabilities())[0].payload
    with pytest.raises(ValueError, match="missing answers"):
        parse_plain_text_question_answers(page, "1: a / 2: a,c")
    with pytest.raises(ValueError, match="more than once"):
        parse_plain_text_question_answers(page, "1: a / 1: b / 3: done")
    with pytest.raises(ValueError, match="unexpected question numbers"):
        parse_plain_text_question_answers(page, "1: a / 2: a,c / 3: done / 4: extra")
