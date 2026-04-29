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
            r"C:\temp\seed.yaml --strict",
            r"C:\temp\seed.yaml --strict",
            id="windows-drive-path-backslashes-preserved",
        ),
        pytest.param(
            r'C:\temp\seed.yaml "two words"',
            r"C:\temp\seed.yaml two words",
            id="windows-drive-path-backslashes-preserved-and-quotes-normalized",
        ),
        pytest.param(
            r"\\server\share\seed.yaml --strict",
            r"\\server\share\seed.yaml --strict",
            id="windows-unc-path-backslashes-preserved",
        ),
        pytest.param(
            r'\\server\share\seed.yaml "two words"',
            r"\\server\share\seed.yaml two words",
            id="windows-unc-path-backslashes-preserved-and-quotes-normalized",
        ),
        pytest.param(
            'C:/temp/seed.yaml "two words"',
            "C:/temp/seed.yaml two words",
            id="windows-forward-slash-path-still-normalized",
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
