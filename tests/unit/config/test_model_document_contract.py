"""Guard source-derived config-reference identities without certifying config consumers."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final

import pytest

from ouroboros.config._model_defaults import DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL
from tests.unit.config.model_document_contract import model_default_errors

ROOT: Final = Path(__file__).resolve().parents[3]
TEST: Final = Path(__file__).relative_to(ROOT)


@pytest.fixture
def reference() -> str:
    return (ROOT / "docs/config-reference.md").read_text(encoding="utf-8")


def test_reference_matches_source_model_defaults(reference: str) -> None:
    # Given the checked-in reference and the actual imported source constants.
    # When the finite example and consensus-table contract is evaluated.
    errors = model_default_errors(reference)
    # Then every documented model identity agrees with its own provider's source default.
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    ("identity", "replacement", "occurrence", "location"),
    [
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 0, "claude_code.model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 1, "anthropic.model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 2, "anthropic.model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 3, "clarification.default_model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 4, "resilience.wonder_model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 5, "resilience.reflect_model"),
        (DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL, 6, "evaluation.semantic_model"),
        (DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL, 0, "litellm.model"),
        (DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL, 1, "consensus.models table"),
        (DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL, 2, "consensus.advocate_model table"),
        (DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL, 3, "consensus.models"),
        (DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL, 4, "consensus.advocate_model"),
    ],
)
def test_each_model_identity_rejects_the_other_provider(
    reference: str, identity: str, replacement: str, occurrence: int, location: str
) -> None:
    # Given one direct or provider-form occurrence changed independently of all others.
    pieces = reference.split(identity)
    assert len(pieces) == (8 if identity == DEFAULT_OPUS_MODEL else 6)
    changed = identity.join(pieces[: occurrence + 1])
    changed += replacement + identity.join(pieces[occurrence + 1 :])
    # When the document is checked against the actual source identities.
    errors = model_default_errors(changed)
    # Then precisely the corrupted location fails, despite correct identifiers elsewhere.
    assert len(errors) == 1 and "MODEL_DEFAULT:" in errors[0] and location in errors[0], errors


@pytest.mark.parametrize(
    ("old", "new", "diagnostic"),
    [
        ("  wonder_model:", "  retired_wonder_model:", "resilience.wonder_model"),
        (
            f"  wonder_model: {DEFAULT_OPUS_MODEL}",
            f"  wonder_model: {DEFAULT_OPUS_MODEL}\n  wonder_model: {DEFAULT_OPUS_MODEL}",
            "resilience.wonder_model",
        ),
        (
            f"  wonder_model: {DEFAULT_OPUS_MODEL}",
            f"  wonder_model: [{DEFAULT_OPUS_MODEL}]",
            "resilience.wonder_model",
        ),
        ("  wonder_model:", "  wonder_model: [unterminated #", "MODEL_DOCUMENT:"),
        ("### Full Config Skeleton", "### Renamed Skeleton", "MODEL_DOCUMENT:"),
        (
            "### Full Config Skeleton",
            "### Full Config Skeleton\n\n### Full Config Skeleton",
            "MODEL_DOCUMENT:",
        ),
        (
            "### Full Config Skeleton",
            "### Full Config Skeleton\n```yaml\n{}\n```",
            "MODEL_DOCUMENT:",
        ),
        ("| `models` |", "| `retired_models` |", "consensus.models"),
        ("| `advocate_model` |", "| `retired_advocate_model` |", "consensus.advocate_model"),
        (
            "on `litellm` it keeps",
            "on `litellm` (`wrong-model`); on `litellm` it keeps",
            "consensus.models",
        ),
        (
            f'(`litellm`: `"{DEFAULT_CONSENSUS_OPUS_MODEL}"`)',
            f'(`litellm`: `"{DEFAULT_CONSENSUS_OPUS_MODEL}"`) (`litellm`: `"wrong-model"`)',
            "consensus.advocate_model",
        ),
        (
            f"    - {DEFAULT_CONSENSUS_OPUS_MODEL}",
            f"    - {DEFAULT_CONSENSUS_OPUS_MODEL}\n    - {DEFAULT_CONSENSUS_OPUS_MODEL}",
            "consensus.models",
        ),
        (
            f"    - {DEFAULT_CONSENSUS_OPUS_MODEL}",
            f"    - {DEFAULT_CONSENSUS_OPUS_MODEL}\n    - null",
            "consensus.models",
        ),
    ],
)
def test_missing_ambiguous_or_malformed_examples_fail(
    reference: str, old: str, new: str, diagnostic: str
) -> None:
    # Given a target whose structure no longer unambiguously represents its model default.
    assert old in reference
    changed = reference.replace(old, new)
    # When the finite contract parses the modified document.
    errors = model_default_errors(changed)
    # Then another intact occurrence cannot hide the missing or invalid target.
    assert errors and any(diagnostic in error for error in errors), errors


def test_duplicate_consensus_table_row_fails(reference: str) -> None:
    # Given a duplicated provider-fallback row with two individually valid identities.
    row = next(line for line in reference.splitlines() if line.startswith("| `advocate_model` |"))
    # When both rows remain under the consensus heading.
    errors = model_default_errors(reference.replace(row, row + "\n" + row))
    # Then the table is ambiguous instead of silently selecting the first row.
    assert any("MODEL_DOCUMENT: consensus.advocate_model" in error for error in errors)


def test_quoted_yaml_and_unrelated_provider_text_are_allowed(reference: str) -> None:
    # Given equivalent scalar quoting, foreign provider changes, and unrelated historical text.
    lines = [
        line.replace(DEFAULT_OPUS_MODEL, f'"{DEFAULT_OPUS_MODEL}"').replace(
            DEFAULT_CONSENSUS_OPUS_MODEL, f'"{DEFAULT_CONSENSUS_OPUS_MODEL}"'
        )
        if not line.startswith("|")
        else line
        for line in reference.splitlines()
    ]
    changed = "\n".join(lines).replace("model: openai/gpt-5.4", "model: foreign-model")
    changed += "\n### Unrelated example\nHistorical identifier: obsolete-opus\n"
    changed += "```yaml\nsemantic_model: obsolete-opus\n```\n"
    # When only named model-default relationships are checked.
    errors = model_default_errors(changed)
    # Then formatting and unrelated configuration do not create drift.
    assert not errors, errors


@pytest.mark.parametrize("constant", ["DEFAULT_OPUS_MODEL", "DEFAULT_CONSENSUS_OPUS_MODEL"])
@pytest.mark.parametrize("update_document", [False, True])
def test_pytest_contract_follows_changed_source_identity(
    tmp_path: Path, constant: str, update_document: bool
) -> None:
    # Given a disposable checkout with a genuinely changed source constant.
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in (
        "tests/__init__.py",
        "tests/unit/__init__.py",
        "tests/unit/config/__init__.py",
        "tests/unit/config/model_document_contract.py",
        str(TEST),
        "docs/config-reference.md",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    source = tmp_path / "src/ouroboros/config/_model_defaults.py"
    old = DEFAULT_OPUS_MODEL if constant == "DEFAULT_OPUS_MODEL" else DEFAULT_CONSENSUS_OPUS_MODEL
    replacement = (
        "new-direct-identity" if constant == "DEFAULT_OPUS_MODEL" else "new-provider-identity"
    )
    assignment = f'{constant} = "{old}"'
    assert assignment in source.read_text()
    source.write_text(source.read_text().replace(assignment, f'{constant} = "{replacement}"'))
    if update_document:
        document = tmp_path / "docs/config-reference.md"
        document.write_text(document.read_text().replace(old, replacement))
    # When a fresh public pytest process imports that checkout and runs the real-doc contract.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{TEST}::test_reference_matches_source_model_defaults",
            "-q",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    (tmp_path / "pytest.log").write_text(completed.stdout + completed.stderr)
    # Then stale documentation fails and a coordinated source/document change passes.
    assert completed.returncode == (0 if update_document else 1), (
        completed.stdout + completed.stderr
    )
    assert ("1 passed" if update_document else "MODEL_DEFAULT:") in completed.stdout
