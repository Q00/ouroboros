"""Guard source-derived config-reference identities without certifying config consumers."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Final, assert_never

import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ouroboros.config._model_defaults import DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL
from tests.unit.config.model_document_contract import model_default_errors

ROOT: Final = Path(__file__).resolve().parents[3]
TEST: Final = Path(__file__).relative_to(ROOT)
DIRECT_TARGETS: Final = (
    ("### Portable Task Profiles", "llm_profiles.deep.providers.claude_code.model"),
    ("## `economics`", "economics.tiers.frontier.models.anthropic.model"),
    ("### Full Config Skeleton", "economics.tiers.frontier.models.anthropic.model"),
    ("### Full Config Skeleton", "clarification.default_model"),
    ("### Full Config Skeleton", "resilience.wonder_model"),
    ("### Full Config Skeleton", "resilience.reflect_model"),
    ("### Full Config Skeleton", "evaluation.semantic_model"),
)
PROVIDER_TARGETS: Final = (
    ("### Portable Task Profiles", "llm_profiles.deep.providers.litellm.model"),
    ("## `consensus`", "models"),
    ("## `consensus`", "advocate_model"),
    ("### Full Config Skeleton", "consensus.models.1"),
    ("### Full Config Skeleton", "consensus.advocate_model"),
)


def _replace_target(markdown: str, section: str, path: str, old: str, new: str) -> str:
    """Mutate one named claim using YAML source marks, independent of other occurrences."""
    heading = re.search(rf"(?m)^{re.escape(section)}$", markdown)
    assert heading is not None
    following = markdown[heading.end() :]
    boundary = re.search(r"(?m)^#{2,3} ", following)
    body = following[: boundary.start()] if boundary else following
    if section == "## `consensus`":
        row = next(line for line in body.splitlines() if line.startswith(f"| `{path}` |"))
        assert row.count(old) == 1
        changed = body.replace(row, row.replace(old, new))
    else:
        block = re.search(r"(?ms)^```yaml\n(.*?)^```", body)
        assert block is not None
        node = yaml.compose(block[1], Loader=yaml.SafeLoader)
        for segment in path.split("."):
            match node:
                case SequenceNode(value=items) if segment.isdecimal():
                    candidates = [items[int(segment)]]
                case SequenceNode(value=items):
                    candidates = [
                        child
                        for child in items
                        if isinstance(child, MappingNode)
                        and any(
                            key.value == "provider" and value.value == segment
                            for key, value in child.value
                        )
                    ]
                case MappingNode(value=entries):
                    candidates = [value for key, value in entries if key.value == segment]
                case Node() | None:
                    pytest.fail(f"Missing mutation target: {section} {path} at {segment}")
                case unreachable:
                    assert_never(unreachable)
            assert len(candidates) == 1, (section, path, segment)
            node = candidates[0]
        assert isinstance(node, ScalarNode) and node.value == old
        start, end = block.start(1) + node.start_mark.index, block.start(1) + node.end_mark.index
        changed = body[:start] + new + body[end:]
    return markdown[: heading.end()] + changed + following[len(body) :]


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
    ("section", "path", "identity", "replacement"),
    [
        (section, path, DEFAULT_OPUS_MODEL, DEFAULT_CONSENSUS_OPUS_MODEL)
        for section, path in DIRECT_TARGETS
    ]
    + [
        (section, path, DEFAULT_CONSENSUS_OPUS_MODEL, DEFAULT_OPUS_MODEL)
        for section, path in PROVIDER_TARGETS
    ],
)
@pytest.mark.parametrize("unrelated_example", [False, True])
def test_each_model_identity_rejects_the_other_provider(
    reference: str,
    section: str,
    path: str,
    identity: str,
    replacement: str,
    unrelated_example: bool,
) -> None:
    # Given named targets, optionally surrounded by an unrelated example with the same identities.
    document = reference
    if unrelated_example:
        document += "\n### Unrelated example\n```yaml\n"
        document += f"direct: {DEFAULT_OPUS_MODEL}\nprovider: {DEFAULT_CONSENSUS_OPUS_MODEL}\n```\n"
    changed = _replace_target(document, section, path, identity, replacement)
    # When the document is checked against the actual source identities.
    errors = model_default_errors(changed)
    # Then precisely the corrupted location fails, despite correct identifiers elsewhere.
    location = path.removesuffix(".1")
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
