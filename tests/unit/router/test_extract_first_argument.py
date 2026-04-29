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
        pytest.param(
            r"C:\temp\seed.yaml --strict",
            r"C:\temp\seed.yaml --strict",
            id="windows-drive-path-preserved",
        ),
        pytest.param(
            r"\\server\share\seed.yaml --strict",
            r"\\server\share\seed.yaml --strict",
            id="windows-unc-path-preserved",
        ),
        pytest.param(
            "  C:\\temp\\seed.yaml --strict",
            "  C:\\temp\\seed.yaml --strict",
            id="windows-drive-path-with-incidental-leading-whitespace-preserved",
        ),
        pytest.param(
            r'"C:\Program Files\app\seed.yaml" --strict',
            r"C:\Program Files\app\seed.yaml --strict",
            id="quoted-drive-path-with-spaces-preserved",
        ),
        pytest.param(
            r"'C:\Program Files\app\seed.yaml' --strict",
            r"C:\Program Files\app\seed.yaml --strict",
            id="single-quoted-drive-path-with-spaces-preserved",
        ),
        pytest.param(
            r'"\\server\share\dir name\seed.yaml" --strict',
            r"\\server\share\dir name\seed.yaml --strict",
            id="quoted-unc-path-with-spaces-preserved",
        ),
        pytest.param(
            r"'\\server\share\dir name\seed.yaml' --strict",
            r"\\server\share\dir name\seed.yaml --strict",
            id="single-quoted-unc-path-with-spaces-preserved",
        ),
        pytest.param(
            r'"C:\Program Files\app\seed.yaml"',
            r"C:\Program Files\app\seed.yaml",
            id="quoted-drive-path-without-tail",
        ),
        pytest.param(
            r'"\\server\share\dir name\seed.yaml" "two words"',
            r"\\server\share\dir name\seed.yaml two words",
            id="quoted-unc-path-with-quoted-tail-shell-normalized",
        ),
        pytest.param(
            r'"C:\Program Files\app\seed.yaml" --label "two words"',
            r"C:\Program Files\app\seed.yaml --label two words",
            id="quoted-drive-path-with-quoted-tail-shell-normalized",
        ),
        pytest.param(
            r'"C:\temp\seed.yaml" --label one --label two',
            r"C:\temp\seed.yaml --label one --label two",
            id="quoted-drive-path-with-bare-tail-rejoined",
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


def test_extract_first_argument_does_not_split_drive_path_on_embedded_quote() -> None:
    """A leading quote followed mid-path by another quote must not produce a
    silently-corrupted ``C:\\Pro`` token. The helper bails out when the closing
    quote is not separated from any tail by whitespace, so the contrived input
    falls through to the existing shlex/fallback path; whatever shlex returns,
    it must not be the truncated drive-letter prefix.
    """
    corrupted_prefix = r"C:\Pro"
    result = extract_first_argument(r'"C:\Pro"gram Files\app\seed.yaml"')
    assert result != corrupted_prefix
    assert result is not None and "Files" in result
