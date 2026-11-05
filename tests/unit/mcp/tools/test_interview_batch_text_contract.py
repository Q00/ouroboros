import pytest

from ouroboros.mcp.tools.interview_batch import parse_batch_text


def test_numeric_choices_and_unquoted_free_text():
    assert parse_batch_text("1: 2 / 2: 1,3 / 3: a small garden", ["one", "two", "three"]) == [
        {"question_id": "one", "selected_choices": [2]},
        {"question_id": "two", "selected_choices": [1, 3]},
        {"question_id": "three", "answer": "a small garden"},
    ]


def test_quoted_numeric_and_quoted_separator_remain_verbatim_answers():
    assert parse_batch_text('1: "1" / 2: "fast / 3: safe"', ["one", "two", "three"]) == [
        {"question_id": "one", "answer": "1"},
        {"question_id": "two", "answer": "fast / 3: safe"},
    ]


def test_unquoted_path_ending_in_slash_is_free_text():
    assert parse_batch_text("1: use /srv/example/", ["one"]) == [
        {"question_id": "one", "answer": "use /srv/example/"}
    ]


def test_quoted_label_with_slash_is_literal_free_text():
    assert parse_batch_text('1: "5 /"', ["one"]) == [{"question_id": "one", "answer": "5 /"}]


@pytest.mark.parametrize(
    "value",
    [
        "1: 1,1",
        "1: 5",
        "1: a,2",
        "1: a,",
        "1: ",
        "1: a /",
        "1: 5 /",
        "1: 5 / 2: a",
        "1: 9,1 /",
        "1: a, /",
        "1: z /",
        "1: a / 6: b",
    ],
)
def test_ambiguous_or_invalid_selections_are_not_silently_recorded(value):
    with pytest.raises(ValueError):
        parse_batch_text(value, ["one", "two", "three"])
