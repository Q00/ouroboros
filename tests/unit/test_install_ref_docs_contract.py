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

# Each surface may carry more than one one-liner (#2122 added a hero copy above
# the demo table). Every occurrence is checked, not just the first: a `search`
# that stops at the first match is how the hero copy landed without this
# contract noticing it at all.
DOC_SURFACES = {
    "README.md": ("readme", "readme-hero"),
    "README.ko.md": ("readme-ko", "readme-hero-ko"),
    "README.zh-CN.md": ("readme-zh", "readme-hero-zh"),
    "docs/getting-started.md": ("docs-getting-started",),
}

ONE_LINER_RE = re.compile(
    r"curl -fsSL https://raw\.githubusercontent\.com/Q00/ouroboros/main/scripts/install\.sh \| "
    r"OUROBOROS_INSTALL_REF=(?P<token>[A-Za-z0-9._-]{1,32}) bash"
)


@pytest.mark.parametrize("doc_path,expected_tokens", sorted(DOC_SURFACES.items()))
def test_install_ref_one_liners_put_assignment_on_bash_side(
    doc_path: str, expected_tokens: tuple[str, ...]
) -> None:
    text = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    tokens = tuple(match.group("token") for match in ONE_LINER_RE.finditer(text))
    assert tokens, f"{doc_path} is missing the expected OUROBOROS_INSTALL_REF one-liner shape"
    assert sorted(tokens) == sorted(expected_tokens), (
        f"{doc_path} one-liner attribution tokens drifted: {tokens!r}"
    )


@pytest.mark.parametrize("doc_path", sorted(DOC_SURFACES))
def test_every_documented_install_command_uses_the_guarded_shape(doc_path: str) -> None:
    """No install one-liner may exist outside the shape this file guards.

    The token check above only sees commands that already match
    ``ONE_LINER_RE``. A command written some other way would be invisible to it
    — which is exactly the failure mode this file exists to prevent — so count
    the raw mentions of the install script and require them all to be accounted
    for.
    """
    text = (REPO_ROOT / doc_path).read_text(encoding="utf-8")
    piped_to_bash = len(re.findall(r"scripts/install\.sh[^\n]*\|[^\n]*bash", text))
    guarded = len(ONE_LINER_RE.findall(text))
    assert piped_to_bash == guarded, (
        f"{doc_path} pipes install.sh to bash {piped_to_bash} time(s) but only "
        f"{guarded} match the guarded OUROBOROS_INSTALL_REF shape"
    )


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
