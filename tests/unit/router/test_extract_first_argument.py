from __future__ import annotations

import pytest

from ouroboros.router import extract_first_argument


@pytest.mark.parametrize(
    ("remainder", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param("", None, id="empty"),
        pytest.param("   \t  ", None, id="whitespace"),
        pytest.param("seed.yaml", "seed.yaml", id="single-plain-token"),
        pytest.param(
            "seed.yaml --max-iterations 2",
            "seed.yaml --max-iterations 2",
            id="multi-token-joined",
        ),
        pytest.param(
            "add dark mode to settings",
            "add dark mode to settings",
            id="natural-language-unquoted",
        ),
        pytest.param(
            '"seed file.yaml" --strict',
            "seed file.yaml --strict",
            id="double-quoted-joined",
        ),
        pytest.param("seed.yaml\n", "seed.yaml", id="trailing-newline-normalized"),
        pytest.param(
            '"seed file.yaml"\r\n',
            "seed file.yaml",
            id="quoted-trailing-crlf-normalized",
        ),
        pytest.param(
            "'seed file.yaml' --strict",
            "seed file.yaml --strict",
            id="single-quoted-joined",
        ),
        pytest.param(
            r"seed\ file.yaml --strict",
            "seed file.yaml --strict",
            id="escaped-space-joined",
        ),
        pytest.param(
            '"add dark mode to settings"',
            "add dark mode to settings",
            id="fully-quoted-phrase",
        ),
        pytest.param(
            "goal: test\nconstraints:\n  - keep it simple\nacceptance_criteria:\n  - works",
            "goal: test\nconstraints:\n  - keep it simple\nacceptance_criteria:\n  - works",
            id="multiline-inline-content-preserved",
        ),
    ],
)
def test_extract_first_argument_returns_full_argument_payload(
    remainder: str | None,
    expected: str | None,
) -> None:
    assert extract_first_argument(remainder) == expected


def test_extract_first_argument_falls_back_to_whitespace_split_for_invalid_shell_syntax() -> None:
    assert (
        extract_first_argument('"unterminated seed path.yaml --strict')
        == '"unterminated seed path.yaml --strict'
    )
