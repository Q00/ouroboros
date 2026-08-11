"""Keep the documented OUROBOROS_INSTALL_REF one-liners actually working.

PR #2072's review caught a real bug: `VAR=x curl ... | bash` only scopes VAR
to the `curl` process, not the `bash` process that actually reads it in
scripts/install.sh. The fix moves the assignment to the right-hand side of
the pipe (`curl ... | VAR=x bash`). This file guards both the documented
command shape and the underlying shell semantics so the mistake can't come
back silently.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DOC_SURFACES = {
    "README.md": "readme",
    "README.ko.md": "readme-ko",
    "README.zh-CN.md": "readme-zh",
    "docs/getting-started.md": "docs-getting-started",
}

ONE_LINER_RE = re.compile(
    r"curl -fsSL https://raw\.githubusercontent\.com/Q00/ouroboros/main/scripts/install\.sh \| "
    r"OUROBOROS_INSTALL_REF=(?P<token>[A-Za-z0-9._-]{1,32}) bash"
)


@pytest.mark.parametrize("doc_path,expected_token", sorted(DOC_SURFACES.items()))
def test_install_ref_one_liner_puts_assignment_on_bash_side(
    doc_path: str, expected_token: str
) -> None:
    text = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    match = ONE_LINER_RE.search(text)
    assert match, f"{doc_path} is missing the expected OUROBOROS_INSTALL_REF one-liner shape"
    assert match.group("token") == expected_token


@pytest.mark.parametrize("doc_path", sorted(DOC_SURFACES))
def test_install_ref_one_liner_is_not_the_broken_lhs_shape(doc_path: str) -> None:
    text = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    broken = re.search(r"OUROBOROS_INSTALL_REF=[A-Za-z0-9._-]{1,32} curl -fsSL", text)
    assert broken is None, (
        f"{doc_path} has the env assignment on the curl side of the pipe -- it never reaches bash"
    )


def test_env_assignment_on_pipe_rhs_actually_reaches_bash() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'printf "echo \\$OUROBOROS_INSTALL_REF" | OUROBOROS_INSTALL_REF=probe-token bash',
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "probe-token"


def test_env_assignment_on_pipe_lhs_does_not_reach_bash() -> None:
    """Negative sanity check: proves the bug this suite guards against is real."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'OUROBOROS_INSTALL_REF=probe-token printf "echo \\$OUROBOROS_INSTALL_REF" | bash',
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == ""
