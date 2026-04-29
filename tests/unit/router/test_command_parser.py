"""Tests for stateless Ouroboros command parsing."""

from dataclasses import FrozenInstanceError

import pytest

from ouroboros.router import ParsedOooCommand, parse_ooo_command
from ouroboros.router.types import ParsedOooCommand as TypesParsedOooCommand


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (
            "ooo run",
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo run",
                remainder=None,
            ),
        ),
        (
            "ooo run seed.yaml",
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo run",
                remainder="seed.yaml",
            ),
        ),
        (
            '  OOO interview "Build a REST API"',
            ParsedOooCommand(
                skill_name="interview",
                command_prefix="ooo interview",
                remainder='"Build a REST API"',
            ),
        ),
        (
            "ooo pm-interview --draft",
            ParsedOooCommand(
                skill_name="pm-interview",
                command_prefix="ooo pm-interview",
                remainder="--draft",
            ),
        ),
        (
            "ooo run_2 seed.yaml",
            ParsedOooCommand(
                skill_name="run_2",
                command_prefix="ooo run_2",
                remainder="seed.yaml",
            ),
        ),
        (
            "/ouroboros:run seed.yaml",
            ParsedOooCommand(
                skill_name="run",
                command_prefix="/ouroboros:run",
                remainder="seed.yaml",
            ),
        ),
        (
            "  /OUROBOROS:status",
            ParsedOooCommand(
                skill_name="status",
                command_prefix="/ouroboros:status",
                remainder=None,
            ),
        ),
    ],
)
def test_parse_ooo_command_accepts_exact_ooo_and_slash_prefixes(
    prompt: str,
    expected: ParsedOooCommand,
) -> None:
    assert parse_ooo_command(prompt) == expected


def test_parse_ooo_command_normalizes_prefix_and_preserves_argument_text() -> None:
    parsed = parse_ooo_command(" \tOoO   Run\t--seed  alpha  beta  ")

    assert parsed == ParsedOooCommand(
        skill_name="run",
        command_prefix="ooo run",
        remainder="--seed  alpha  beta  ",
    )


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        pytest.param(
            "\nooo run seed.yaml",
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo run",
                remainder="seed.yaml",
            ),
            id="leading-newline-ooo-prefix",
        ),
        pytest.param(
            "\r\n\tOOO\tEvaluate\tartifact.md",
            ParsedOooCommand(
                skill_name="evaluate",
                command_prefix="ooo evaluate",
                remainder="artifact.md",
            ),
            id="tab-separated-ooo-command",
        ),
        pytest.param(
            " \f/OUROBOROS:Status\torch_123",
            ParsedOooCommand(
                skill_name="status",
                command_prefix="/ouroboros:status",
                remainder="orch_123",
            ),
            id="form-feed-leading-slash-prefix",
        ),
        pytest.param(
            "ooo run    ",
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo run",
                remainder="",
            ),
            id="trailing-separator-whitespace-no-argument",
        ),
    ],
)
def test_parse_ooo_command_handles_raw_command_whitespace_variants(
    prompt: str,
    expected: ParsedOooCommand,
) -> None:
    assert parse_ooo_command(prompt) == expected


def test_parse_ooo_command_normalizes_slash_prefix_and_skill_case() -> None:
    parsed = parse_ooo_command(" \t/OUROBOROS:Run   Seed.yaml")

    assert parsed == ParsedOooCommand(
        skill_name="run",
        command_prefix="/ouroboros:run",
        remainder="Seed.yaml",
    )


def test_parse_ooo_command_accepts_multiline_slash_payload() -> None:
    payload = "goal: test\nconstraints:\n  - keep it simple\nacceptance_criteria:\n  - works"

    assert parse_ooo_command(f"/ouroboros:run\n{payload}") == ParsedOooCommand(
        skill_name="run",
        command_prefix="/ouroboros:run",
        remainder=payload,
    )


def test_parse_ooo_command_preserves_multiline_payload_leading_whitespace() -> None:
    payload = "  goal: test\n  constraints:\n    - keep it simple"

    assert parse_ooo_command(f"/ouroboros:run\n{payload}") == ParsedOooCommand(
        skill_name="run",
        command_prefix="/ouroboros:run",
        remainder=payload,
    )


def test_parsed_ooo_command_type_is_immutable_normalized_command_data() -> None:
    parsed = TypesParsedOooCommand(
        skill_name="run",
        command_prefix="ooo run",
        remainder="seed.yaml",
    )

    assert ParsedOooCommand is TypesParsedOooCommand
    assert parsed.skill_name == "run"
    assert parsed.command_prefix == "ooo run"
    assert parsed.remainder == "seed.yaml"
    assert parsed.remaining_text == "seed.yaml"

    with pytest.raises(FrozenInstanceError):
        parsed.skill_name = "status"  # type: ignore[misc]


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "ooo",
        "ooo    ",
        "ooo:run seed.yaml",
        "ooorun seed.yaml",
        "ouroboros:run seed.yaml",
        "please ooo run seed.yaml",
        "note /ouroboros:run seed.yaml",
        "@ouroboros ooo run seed.yaml",
        "`ooo run seed.yaml`",
        "> ooo run seed.yaml",
        "/ouroboros:",
        "/ouroboros:r?",
    ],
)
def test_parse_ooo_command_rejects_non_runtime_intercept_forms(prompt: str) -> None:
    assert parse_ooo_command(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "ooo /run seed.yaml",
        "ooo -run seed.yaml",
        "ooo run!",
        "ooo run:seed.yaml",
        "/ouroboros: run seed.yaml",
        "/ouroboros:-run seed.yaml",
        "/ouroboros:run!",
    ],
)
def test_parse_ooo_command_rejects_malformed_command_prefixes(prompt: str) -> None:
    assert parse_ooo_command(prompt) is None


def test_parse_ooo_command_has_no_runtime_side_effects(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    parsed = parse_ooo_command("ooo run seed.yaml")

    assert parsed == ParsedOooCommand(
        skill_name="run",
        command_prefix="ooo run",
        remainder="seed.yaml",
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert caplog.records == []
