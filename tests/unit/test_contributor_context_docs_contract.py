"""Guard structural stale-context regressions in contributor documentation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTRIBUTOR_CONTEXT = (
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "docs" / "README.md",
    REPO_ROOT / "docs" / "contributing" / "architecture-overview.md",
    REPO_ROOT / "docs" / "contributing" / "ci-gates.md",
    REPO_ROOT / "docs" / "contributing" / "developing.md",
    REPO_ROOT / "docs" / "contributing" / "key-patterns.md",
    REPO_ROOT / "docs" / "contributing" / "review-conventions.md",
    REPO_ROOT / "docs" / "contributing" / "testing-guide.md",
)
STALE_ROOT_CONTEXT = (
    REPO_ROOT / "Code-Review-Claude.md",
    REPO_ROOT / "Code-Review-Codex.md",
    REPO_ROOT / "project-context.md",
)
PARITY_COMMAND_CONSUMERS = (
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "contributing" / "ci-gates.md",
)
PROFILE_GUIDANCE_CONSUMERS = (
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "docs" / "contributing" / "ci-gates.md",
)
REMOVED_PACKAGE_MARKERS = (
    "routing/",
    "secondary/",
    "PAL Router",
    "Secondary Loop",
    "The Six Phases",
    "Phase 1: PAL Router",
    "Phase 5: Secondary",
)
SOURCE_PACKAGE = re.compile(r"src/ouroboros/(?P<package>[a-z][a-z0-9_]*)/")
SOURCE_FILE = re.compile(r"`(?P<path>src/ouroboros/[a-z0-9_./-]+\.py)`")


def test_contributor_context_does_not_restore_retired_packages() -> None:
    violations: list[str] = []

    for path in CONTRIBUTOR_CONTEXT:
        text = path.read_text(encoding="utf-8")
        for marker in REMOVED_PACKAGE_MARKERS:
            if marker in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {marker!r}")

    assert not violations, "Retired architecture returned to contributor context:\n" + "\n".join(
        violations
    )


def test_documented_source_packages_exist() -> None:
    missing: list[str] = []

    for path in CONTRIBUTOR_CONTEXT:
        text = path.read_text(encoding="utf-8")
        for package in sorted(set(SOURCE_PACKAGE.findall(text))):
            source_path = REPO_ROOT / "src" / "ouroboros" / package
            if not source_path.is_dir():
                missing.append(
                    f"{path.relative_to(REPO_ROOT)} -> {source_path.relative_to(REPO_ROOT)}"
                )

    assert not missing, "Contributor docs reference missing source packages:\n" + "\n".join(missing)


def test_documented_source_files_exist() -> None:
    missing: list[str] = []

    for path in CONTRIBUTOR_CONTEXT:
        text = path.read_text(encoding="utf-8")
        for source_file in sorted(set(SOURCE_FILE.findall(text))):
            if not (REPO_ROOT / source_file).is_file():
                missing.append(f"{path.relative_to(REPO_ROOT)} -> {source_file}")

    assert not missing, "Contributor docs reference missing source files:\n" + "\n".join(missing)


def test_obsolete_root_policy_artifacts_are_absent() -> None:
    present = [str(path.relative_to(REPO_ROOT)) for path in STALE_ROOT_CONTEXT if path.exists()]

    assert not present, f"Obsolete root context still looks authoritative: {present}"


def test_pytest_session_clears_inherited_codex_home() -> None:
    assert "CODEX_HOME" not in os.environ


def test_pr_parity_commands_have_one_executable_owner() -> None:
    stale_command = "uv run ruff format src/ tests/ && uv run ruff check src/ tests/ --fix"
    duplicates = [
        str(path.relative_to(REPO_ROOT))
        for path in PARITY_COMMAND_CONSUMERS
        if stale_command in path.read_text(encoding="utf-8")
    ]

    assert not duplicates, f"Mutating PR-parity command is duplicated in: {duplicates}"


def test_testing_guide_uses_a_supported_dependency_profile() -> None:
    guide = (REPO_ROOT / "docs" / "contributing" / "testing-guide.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = pyproject["dependency-groups"]
    conflicts = pyproject["tool"]["uv"]["conflicts"]

    assert "uv sync --python 3.12 --dev --group mcp-test --group litellm-test" in guide
    assert {"mcp-test", "litellm-test"} <= set(groups)
    assert not any(
        {item.get("group") for item in conflict if "group" in item} == {"mcp-test", "litellm-test"}
        for conflict in conflicts
    )


def test_invalid_all_extras_profile_is_not_documented_for_contributors() -> None:
    violations = [
        str(path.relative_to(REPO_ROOT))
        for path in PROFILE_GUIDANCE_CONSUMERS
        if "uv sync --python 3.13 --all-extras" in path.read_text(encoding="utf-8")
    ]

    assert not violations, f"Unsupported all-extras contributor profile remains in: {violations}"


def test_testing_guide_names_the_real_isolation_and_ci_boundaries() -> None:
    guide = (REPO_ROOT / "docs" / "contributing" / "testing-guide.md").read_text(encoding="utf-8")

    assert "tests/conftest.py" in guide
    assert "$HOME" in guide
    assert ".github/workflows/test.yml" in guide
    assert "-n 4 --dist worksteal" in guide
    assert "MCP tests require network and external servers" not in guide
