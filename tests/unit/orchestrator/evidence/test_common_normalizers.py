"""Regression tests for the evidence whitespace normalizers.

`_normalize_command` and `_normalize_exact_command` are two names for one
behaviour. The second exists so that command-proof call sites read as
explicitly case-sensitive (see `69bc1ad7a`). They used to carry separate copies
of the body under an unenforced contract that they must never diverge; a
whitespace fix applied to only one would have made the proof path and the audit
path disagree about what "the same command" means.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.evidence.common import (
    _normalize_command,
    _normalize_exact_command,
    _normalized_evidence_text,
)

COMMANDS = [
    pytest.param("uv run pytest", id="already-normal"),
    pytest.param("uv   run    pytest", id="runs-of-spaces"),
    pytest.param("uv\trun\tpytest", id="tabs"),
    pytest.param("uv run \\\n  pytest -q", id="line-continuation"),
    pytest.param("  uv run pytest  ", id="surrounding-whitespace"),
    pytest.param("UV Run PyTest", id="mixed-case"),
    pytest.param("./Scripts/Check-Auto-Boundary.py", id="mixed-case-path"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("echo '  spaced  literal  '", id="quoted-inner-spaces"),
    pytest.param("printf '한글 인자'", id="non-ascii"),
]


@pytest.mark.parametrize("command", COMMANDS)
def test_both_names_agree(command: str) -> None:
    """The alias must never drift from the implementation it documents."""
    assert _normalize_exact_command(command) == _normalize_command(command)


@pytest.mark.parametrize("command", COMMANDS)
def test_case_is_preserved(command: str) -> None:
    """Command proof is case-sensitive; neither normalizer may fold case."""
    assert _normalize_command(command) == " ".join(command.split())
    assert _normalize_exact_command(command) == " ".join(command.split())


def test_mixed_case_survives_normalization() -> None:
    """The property the `exact` name advertises, pinned explicitly."""
    assert _normalize_exact_command("UV Run PyTest") == "UV Run PyTest"


def test_evidence_text_normalizer_still_folds_case() -> None:
    """Guard the contrast: the text normalizer is the one that lowercases.

    If this ever stops being true, the docstring on `_normalize_command`
    explaining why case-preservation is worth mentioning becomes stale.
    """
    assert _normalized_evidence_text("UV Run PyTest") == "uv run pytest"


def test_normalization_is_idempotent() -> None:
    for command in ("uv   run\tpytest", "  Mixed   Case  "):
        once = _normalize_command(command)
        assert _normalize_command(once) == once
