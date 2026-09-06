"""Unit tests for CodexCliRuntime."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.cli_version_attestation import (
    read_cli_executable_resolution_chain_identity,
)
import ouroboros.orchestrator.codex_cli_runtime as codex_cli_runtime_module
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.skill_tool_mapping import SkillToolMapping
from ouroboros.router import Resolved, ResolveRequest
from ouroboros.router.dispatch import SkillDispatchRouter as SharedSkillDispatchRouter

_EXPECTED_CODEX_PATH = str(Path("/usr/local/bin/codex"))
_EXPECTED_PROJECT_CWD = str(Path("/tmp/project").resolve())


def _test_cli_path(name: str = "codex") -> str:
    return str(Path(os.environ["OUROBOROS_TEST_CLI_DIR"]) / name)


def test_capabilities_report_prompt_only_tool_restrictions_as_translated() -> None:
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    assert runtime.capabilities.system_prompt_support is ParamSupport.TRANSLATED
    assert runtime.capabilities.tool_restriction_support is ParamSupport.TRANSLATED


def test_capabilities_enable_after_turn_synapse_delivery() -> None:
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    assert runtime.capabilities.targeted_resume is True
    assert runtime.capabilities.session_signals.after_turn_delivery is True
    assert runtime.capabilities.session_signals.checkpoint_redirect is False


def test_codex_config_fingerprint_ignores_automatic_project_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_key = str(project_dir.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=project_key)
    original = runtime._codex_config_fingerprint

    config_path.write_text(
        f'model = "gpt-test"\n\n[projects.{json.dumps(project_key)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() == original


def test_codex_config_fingerprint_ignores_main_root_trust_for_worktree_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex records first-use trust against the git MAIN repo root.

    A task running in a linked worktree must not have that benign entry
    treated as authority-bearing drift (observed live: every AC after the
    first failed instantly with "configuration changed").
    """
    main_repo = tmp_path / "main-repo"
    (main_repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {main_repo / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8"
    )
    main_key = str(main_repo.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=str(worktree.resolve(strict=False)))
    original = runtime._codex_config_fingerprint

    config_path.write_text(
        f'model = "gpt-test"\n\n[projects.{json.dumps(main_key)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() == original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 0


def test_codex_config_fingerprint_exempts_first_use_trust_when_config_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex may CREATE config.toml during initialization with only the trust entry.

    The absent-file baseline and a file holding nothing but the exempt
    first-use trust entry both mean "no non-exempt global settings" and must
    fingerprint identically; a non-exempt entry in that new file still drifts.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_key = str(project_dir.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=project_key)
    original = runtime._codex_config_fingerprint

    config_path = codex_home / "config.toml"
    config_path.write_text(
        f'[projects.{json.dumps(project_key)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() == original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 0

    config_path.write_text(
        f'model = "gpt-other"\n\n[projects.{json.dumps(project_key)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    assert runtime._fingerprint_codex_config_files() != original


def test_codex_config_fingerprint_ignores_first_use_trust_for_other_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent session's first-use trust entry is not this runtime's drift.

    Observed live: two ``ooo run`` sessions (or one run plus the user running
    ``codex`` in another terminal) each record ``projects.<own-root>.trust_level``
    on first use; treating the other's entry as drift failed every AC after
    the first, instantly, in both sessions.
    """
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()
    project_key = str(project_dir.resolve(strict=False))
    other_project_key = str(other_dir.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=project_key)
    original = runtime._codex_config_fingerprint

    config_path.write_text(
        f'model = "gpt-test"\n\n[projects.{json.dumps(other_project_key)}]\n'
        'trust_level = "trusted"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() == original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 0


def test_codex_config_fingerprint_tracks_non_trusted_level_for_other_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the automatic ``trusted`` entry is exempt; anything else is drift."""
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()
    project_key = str(project_dir.resolve(strict=False))
    other_project_key = str(other_dir.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=project_key)

    config_path.write_text(
        f'model = "gpt-test"\n\n[projects.{json.dumps(other_project_key)}]\n'
        'trust_level = "untrusted"\n',
        encoding="utf-8",
    )

    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


@pytest.mark.parametrize(
    "updated_trust_level",
    [
        None,
        "untrusted",
    ],
)
def test_codex_config_fingerprint_tracks_existing_current_project_trust_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updated_trust_level: str | None,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_key = str(project_dir.resolve(strict=False))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        f'model = "gpt-test"\n\n[projects.{json.dumps(project_key)}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd=project_key)

    updated_project_config = (
        ""
        if updated_trust_level is None
        else f"[projects.{json.dumps(project_key)}]\ntrust_level = {json.dumps(updated_trust_level)}\n"
    )
    config_path.write_text(
        f'model = "gpt-test"\n\n{updated_project_config}',
        encoding="utf-8",
    )

    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


def test_codex_config_fingerprint_still_detects_project_runtime_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    config_path.write_text(
        (
            'model = "gpt-test"\n\n[projects."/tmp/project"]\n'
            'trust_level = "trusted"\nmodel = "different-model"\n'
        ),
        encoding="utf-8",
    )

    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


def test_codex_profile_v2_fingerprint_ignores_comment_only_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile-v2 TOML fingerprints should reflect semantics, not formatting."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    profile_path = codex_home / "qa.config.toml"
    profile_path.write_text(
        '# comment\nmodel = "gpt-test"\nreasoning_effort = "high"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime._fingerprint_codex_config_files()
    profile_path.write_text(
        'reasoning_effort = "high"\n\n# another comment\nmodel = "gpt-test"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() == original


def test_codex_config_fingerprint_tracks_active_rules_and_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable Codex identity must include active instruction assets."""
    codex_home = tmp_path / "codex-home"
    rules_dir = codex_home / "rules"
    skill_dir = codex_home / "skills" / "ouroboros-welcome"
    rules_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (rules_dir / "ouroboros.md").write_text("rule before\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("---\nname: welcome\n---\nBefore\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime.execution_identity_contract()["codex_config_fingerprint"]
    (rules_dir / "ouroboros.md").write_text("rule after\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() != original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


def test_codex_config_fingerprint_ignores_only_app_managed_system_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop refreshes of .system must not hide changes to user skills."""
    codex_home = tmp_path / "codex-home"
    system_skill = codex_home / "skills" / ".system" / "bundled" / "SKILL.md"
    user_skill = codex_home / "skills" / "my-skill" / "SKILL.md"
    system_skill.parent.mkdir(parents=True)
    user_skill.parent.mkdir(parents=True)
    system_skill.write_text("system before\n", encoding="utf-8")
    user_skill.write_text("user before\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime.execution_identity_contract()["codex_config_fingerprint"]
    system_skill.write_text("system during refresh\n", encoding="utf-8")
    (system_skill.parent / "NEW.md").write_text("new bundled file\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() == original

    user_skill.write_text("user after\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() != original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks require Windows privileges")
def test_codex_config_fingerprint_ignores_system_skills_through_root_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logical skills root keeps its ownership boundary through a symlink."""
    codex_home = tmp_path / "codex-home"
    skill_target = tmp_path / "skill-target"
    system_skill = skill_target / ".system" / "bundled" / "SKILL.md"
    user_skill = skill_target / "my-skill" / "SKILL.md"
    system_skill.parent.mkdir(parents=True)
    user_skill.parent.mkdir(parents=True)
    system_skill.write_text("system before\n", encoding="utf-8")
    user_skill.write_text("user before\n", encoding="utf-8")
    codex_home.mkdir()
    (codex_home / "skills").symlink_to(skill_target, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime.execution_identity_contract()["codex_config_fingerprint"]
    system_skill.write_text("system during refresh\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() == original

    user_skill.write_text("user after\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() != original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


def test_codex_config_fingerprint_tracks_instruction_asset_symlink_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instruction identity must change when active rules/skills topology changes."""
    codex_home = tmp_path / "codex-home"
    rules_dir = codex_home / "rules"
    rules_dir.mkdir(parents=True)
    target = tmp_path / "rule-target.md"
    target.write_text("external contents are not followed\n", encoding="utf-8")
    (rules_dir / "ouroboros.md").symlink_to(target)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime.execution_identity_contract()["codex_config_fingerprint"]
    replacement_target = tmp_path / "replacement-target.md"
    (rules_dir / "ouroboros.md").unlink()
    (rules_dir / "ouroboros.md").symlink_to(replacement_target)

    assert runtime._fingerprint_codex_config_files() != original


def test_codex_config_fingerprint_tracks_instruction_symlink_target_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instruction symlinks must bind the current target contents, not only pathnames."""
    codex_home = tmp_path / "codex-home"
    rules_dir = codex_home / "rules"
    rules_dir.mkdir(parents=True)
    target = tmp_path / "rule-target.md"
    target.write_text("rule before\n", encoding="utf-8")
    (rules_dir / "ouroboros.md").symlink_to(target)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    original = runtime.execution_identity_contract()["codex_config_fingerprint"]
    target.write_text("rule after\n", encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() != original
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1
    # Re-baselined: the same state is not drift twice.
    runtime._reconcile_codex_config_files()
    assert runtime._drift.epoch == 1


def test_build_command_rejects_in_place_codex_cli_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable Codex identity must fail closed if the selected executable changes."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)

    runtime = CodexCliRuntime(cli_path=cli_path, cwd="/tmp/project", model="gpt-5")
    assert runtime._build_command("/tmp/last-message")

    cli_path.write_text("#!/bin/sh\necho codex 2.0\n", encoding="utf-8")
    cli_path.chmod(0o755)

    with pytest.raises(RuntimeError, match="Codex CLI executable changed"):
        runtime._verify_cli_executable_identity_unchanged()


def test_build_command_rejects_cli_content_drift_before_version_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced executable must never be launched for its version string."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cli_path = tmp_path / "codex"
    side_effect = tmp_path / "replacement-ran"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)

    runtime = CodexCliRuntime(cli_path=cli_path, cwd="/tmp/project", model="gpt-5")

    cli_path.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(side_effect))}\necho codex 2.0\n",
        encoding="utf-8",
    )
    cli_path.chmod(0o755)

    with pytest.raises(RuntimeError, match="Codex CLI executable changed"):
        runtime._verify_cli_executable_identity_unchanged()
    assert not side_effect.exists()


def test_version_attestation_distinguishes_timeout_from_execution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(str(cli_path), timeout=2)

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", timeout)
    timed_out = runtime._cli_executable_version_attestation()
    assert timed_out.state is codex_cli_runtime_module._CliExecutableVersionState.TIMED_OUT
    assert timed_out.identity is None

    def execution_failure(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("deterministic probe failure")

    monkeypatch.setattr(
        codex_cli_runtime_module.subprocess,
        "run",
        execution_failure,
    )
    failed = runtime._cli_executable_version_attestation()
    assert failed.state is codex_cli_runtime_module._CliExecutableVersionState.EXECUTION_FAILED
    assert failed.identity is None


def test_missing_attestations_never_compare_as_positive_identity_evidence() -> None:
    state = codex_cli_runtime_module._CliExecutableVersionState
    attestation = codex_cli_runtime_module._CliExecutableVersionAttestation

    assert (
        CodexCliRuntime._compare_cli_executable_version_attestations(
            attestation(state.TIMED_OUT),
            attestation(state.TIMED_OUT),
        )
        is state.TIMED_OUT
    )
    assert (
        CodexCliRuntime._compare_cli_executable_version_attestations(
            attestation(state.VERIFIED, "baseline", (1, 1), "baseline"),
            attestation(state.INDETERMINATE),
        )
        is state.INDETERMINATE
    )
    # Even an internally malformed "verified" value cannot turn None == None
    # into positive identity evidence.
    assert (
        CodexCliRuntime._compare_cli_executable_version_attestations(
            attestation(state.VERIFIED),
            attestation(state.VERIFIED),
        )
        is state.EXECUTION_FAILED
    )


def test_resolution_chain_symlink_loop_fails_closed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second.name)
    second.symlink_to(first.name)

    assert read_cli_executable_resolution_chain_identity(str(first)) is None


def test_resolution_chain_broken_symlink_fails_closed(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.symlink_to("missing")

    assert read_cli_executable_resolution_chain_identity(str(broken)) is None


def test_initialization_timeout_blocks_without_claiming_executable_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    calls = 0

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(str(cli_path), timeout=2)

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", timeout)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    with pytest.raises(RuntimeError, match="timed out during runtime initialization") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "executable changed" not in str(excinfo.value)
    # A missing baseline cannot be repaired by comparing it to another missing
    # probe: command construction fails before performing a second probe.
    assert calls == 1


def test_initialization_execution_failure_has_distinct_fail_closed_error(tmp_path: Path) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    with pytest.raises(RuntimeError, match="failed during runtime initialization") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "executable changed" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("runtime_class", "display_name"),
    [
        (CodexCliRuntime, "Codex CLI"),
        (CopilotCliRuntime, "Copilot CLI"),
    ],
)
def test_check_time_timeout_is_fail_closed_but_retryable_for_codex_family(
    runtime_class: type[CodexCliRuntime],
    display_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "runtime-cli"
    cli_path.write_text("#!/bin/sh\necho runtime 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = runtime_class(cli_path=cli_path, cwd=tmp_path, model="test-model")
    successful_run = codex_cli_runtime_module.subprocess.run

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(str(cli_path), timeout=2)

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", timeout)
    assert (
        runtime.execution_identity_contract()["cli_executable_version"]
        == runtime._cli_executable_version_identity_snapshot
    )
    with pytest.raises(RuntimeError, match="timed out while verifying") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()
    assert str(excinfo.value).startswith(display_name)
    assert "executable changed" not in str(excinfo.value)

    # A transient check-time failure does not poison the initialization
    # baseline. The same runtime succeeds after the probe becomes available.
    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", successful_run)
    assert runtime._build_command(str(tmp_path / "last-message"), prompt="test")


def test_check_time_execution_failure_is_not_reported_as_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    def failed_probe(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 7, stdout="", stderr="temporary failure")

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", failed_probe)
    with pytest.raises(RuntimeError, match="failed while verifying") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()
    assert "executable changed" not in str(excinfo.value)


def test_in_place_mutation_during_version_probe_is_not_authorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    original = "#!/bin/sh\necho codex 1.0\n"
    cli_path.write_text(original, encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")
    original_inode = cli_path.stat().st_ino

    def mutate_during_probe(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cli_path.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="codex 1.0\n", stderr="")

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", mutate_during_probe)
    with pytest.raises(RuntimeError, match="executable version changed") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "failed while verifying" not in str(excinfo.value)
    assert cli_path.stat().st_ino == original_inode
    assert cli_path.read_text(encoding="utf-8") != original


def test_in_place_aba_during_version_probe_is_not_authorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    original = "#!/bin/sh\necho codex 1.0\n"
    cli_path.write_text(original, encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")
    original_inode = cli_path.stat().st_ino

    def mutate_and_restore_during_probe(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        cli_path.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
        cli_path.write_text(original, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="codex 1.0\n", stderr="")

    monkeypatch.setattr(
        codex_cli_runtime_module.subprocess,
        "run",
        mutate_and_restore_during_probe,
    )
    with pytest.raises(RuntimeError, match="executable version changed") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "failed while verifying" not in str(excinfo.value)
    assert cli_path.stat().st_ino == original_inode
    assert cli_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("probe_outcome", ["timeout", "os_error", "nonzero", "empty"])
def test_probe_window_aba_takes_precedence_over_probe_failure(
    probe_outcome: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    original = "#!/bin/sh\necho codex 1.0\n"
    cli_path.write_text(original, encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    def mutate_and_fail(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cli_path.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
        cli_path.write_text(original, encoding="utf-8")
        if probe_outcome == "timeout":
            raise subprocess.TimeoutExpired(str(cli_path), timeout=2)
        if probe_outcome == "os_error":
            raise OSError("deterministic probe failure")
        if probe_outcome == "nonzero":
            return subprocess.CompletedProcess(args, 7, stdout="", stderr="failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", mutate_and_fail)
    with pytest.raises(RuntimeError, match="executable version changed") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "timed out" not in str(excinfo.value)
    assert "failed while verifying" not in str(excinfo.value)


def test_atomic_aba_during_version_probe_is_not_authorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    original = "#!/bin/sh\necho codex 1.0\n"
    cli_path.write_text(original, encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")
    original_inode = cli_path.stat().st_ino
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
    replacement.chmod(0o755)
    parked_original = tmp_path / "parked-original"

    def replace_and_restore_during_probe(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        os.replace(cli_path, parked_original)
        os.replace(replacement, cli_path)
        os.replace(cli_path, replacement)
        os.replace(parked_original, cli_path)
        return subprocess.CompletedProcess(args, 0, stdout="codex 1.0\n", stderr="")

    monkeypatch.setattr(
        codex_cli_runtime_module.subprocess,
        "run",
        replace_and_restore_during_probe,
    )
    with pytest.raises(RuntimeError, match="executable version changed") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "failed while verifying" not in str(excinfo.value)
    assert cli_path.stat().st_ino == original_inode
    assert cli_path.read_text(encoding="utf-8") == original


def test_changed_symlink_target_is_rejected_before_version_execution(
    tmp_path: Path,
) -> None:
    effects_dir = tmp_path / "effects"
    effects_dir.mkdir()
    marker = effects_dir / "version-probe-ran"
    script = f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho codex 1.0\n"
    good_target = tmp_path / "good"
    replacement_target = tmp_path / "replacement"
    good_target.write_text(script, encoding="utf-8")
    replacement_target.write_text(script, encoding="utf-8")
    good_target.chmod(0o755)
    replacement_target.chmod(0o755)
    cli_link = tmp_path / "codex"
    cli_link.symlink_to(good_target.name)
    runtime = CodexCliRuntime(cli_path=cli_link, cwd=tmp_path, model="gpt-5")
    assert marker.exists()
    marker.unlink()

    cli_link.unlink()
    cli_link.symlink_to(replacement_target.name)

    with pytest.raises(RuntimeError, match="executable version changed"):
        runtime._verify_cli_executable_identity_unchanged()
    assert not marker.exists()


def test_intermediate_symlink_atomic_aba_during_probe_is_not_authorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_dir = tmp_path / "launch"
    middle_dir = tmp_path / "middle"
    targets_dir = tmp_path / "targets"
    launch_dir.mkdir()
    middle_dir.mkdir()
    targets_dir.mkdir()
    good_target = targets_dir / "good"
    bad_target = targets_dir / "bad"
    script = "#!/bin/sh\necho codex 1.0\n"
    good_target.write_text(script, encoding="utf-8")
    bad_target.write_text(script, encoding="utf-8")
    good_target.chmod(0o755)
    bad_target.chmod(0o755)

    launch_path = launch_dir / "codex"
    intermediate_hop = middle_dir / "hop"
    launch_path.symlink_to("../middle/hop")
    intermediate_hop.symlink_to("../targets/good")
    runtime = CodexCliRuntime(cli_path=launch_path, cwd=tmp_path, model="gpt-5")
    initialized = runtime._cli_executable_version_attestation_snapshot
    assert initialized is not None
    assert runtime._build_command(str(tmp_path / "stable-last-message"))
    original_hop_inode = intermediate_hop.lstat().st_ino

    bad_hop = middle_dir / "bad-hop"
    parked_hop = middle_dir / "parked-hop"
    bad_hop.symlink_to("../targets/bad")

    def swap_and_restore_intermediate_hop(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        os.replace(intermediate_hop, parked_hop)
        os.replace(bad_hop, intermediate_hop)
        os.replace(intermediate_hop, bad_hop)
        os.replace(parked_hop, intermediate_hop)
        return subprocess.CompletedProcess(args, 0, stdout="codex 1.0\n", stderr="")

    monkeypatch.setattr(
        codex_cli_runtime_module.subprocess,
        "run",
        swap_and_restore_intermediate_hop,
    )
    with pytest.raises(RuntimeError, match="executable version changed") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()

    assert "executable changed" not in str(excinfo.value)
    assert intermediate_hop.lstat().st_ino == original_hop_inode
    assert intermediate_hop.readlink() == Path("../targets/good")


def test_sibling_churn_between_attestations_does_not_claim_executable_drift(
    tmp_path: Path,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    (tmp_path / "unrelated-sibling").write_text("unrelated", encoding="utf-8")

    assert runtime._build_command(str(tmp_path / "last-message"))


def test_sibling_churn_during_probe_fails_closed_but_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")
    successful_run = codex_cli_runtime_module.subprocess.run

    sibling_number = 0

    def churn_sibling(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal sibling_number
        sibling_number += 1
        (tmp_path / f"unrelated-sibling-{sibling_number}").write_text(
            "unrelated",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="codex 1.0\n", stderr="")

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", churn_sibling)
    current = runtime._cli_executable_version_attestation(
        runtime._cli_executable_version_attestation_snapshot
    )
    assert current.state is codex_cli_runtime_module._CliExecutableVersionState.INDETERMINATE

    with pytest.raises(RuntimeError, match="authority became indeterminate") as excinfo:
        runtime._verify_cli_executable_identity_unchanged()
    assert "without claiming executable drift" in str(excinfo.value)
    assert "retry the execution" in str(excinfo.value)

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", successful_run)
    assert runtime._build_command(str(tmp_path / "last-message"))


def test_repeated_probe_timeouts_never_converge_to_false_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model an overloaded 16-way probe burst without wall-clock races."""
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(str(cli_path), timeout=2)

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", timeout)
    for _ in range(16):
        with pytest.raises(RuntimeError, match="timed out while verifying") as excinfo:
            runtime._verify_cli_executable_identity_unchanged()
        assert "executable changed" not in str(excinfo.value)


def test_successful_version_change_has_distinct_changed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho ignored\n", encoding="utf-8")
    cli_path.chmod(0o755)
    outputs = iter(("codex 1.0\n", "codex 2.0\n"))

    def version_probe(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(codex_cli_runtime_module.subprocess, "run", version_probe)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd=tmp_path, model="gpt-5")

    with pytest.raises(RuntimeError, match="executable version changed"):
        runtime._verify_cli_executable_identity_unchanged()


@pytest.mark.parametrize("runtime_class", [CodexCliRuntime, CopilotCliRuntime])
def test_atomic_executable_replacement_with_identical_evidence_is_changed(
    runtime_class: type[CodexCliRuntime],
    tmp_path: Path,
) -> None:
    """A same-bytes/same-version replacement still changes execution authority."""
    script = "#!/bin/sh\necho runtime 1.0\n"
    cli_path = tmp_path / "runtime-cli"
    cli_path.write_text(script, encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = runtime_class(cli_path=cli_path, cwd=tmp_path, model="test-model")
    initialized = runtime._cli_executable_version_attestation_snapshot
    assert initialized is not None
    assert initialized.filesystem_identity == (cli_path.stat().st_dev, cli_path.stat().st_ino)

    replacement = tmp_path / "replacement-cli"
    replacement.write_text(script, encoding="utf-8")
    replacement.chmod(0o755)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    assert replacement_identity != initialized.filesystem_identity
    os.replace(replacement, cli_path)

    current = runtime._cli_executable_version_attestation()
    assert current.state is codex_cli_runtime_module._CliExecutableVersionState.VERIFIED
    assert current.filesystem_identity == replacement_identity
    assert (
        runtime._compare_cli_executable_version_attestations(initialized, current)
        is codex_cli_runtime_module._CliExecutableVersionState.CHANGED
    )
    with pytest.raises(RuntimeError, match="executable version changed"):
        runtime._verify_cli_executable_identity_unchanged()


def test_atomic_symlink_target_replacement_with_identical_evidence_is_changed(
    tmp_path: Path,
) -> None:
    """Target inode drift remains visible through an unchanged launch symlink."""
    script = "#!/bin/sh\necho codex 1.0\n"
    target = tmp_path / "codex-target"
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)
    cli_link = tmp_path / "codex"
    cli_link.symlink_to(target.name)
    runtime = CodexCliRuntime(cli_path=cli_link, cwd=tmp_path, model="gpt-5")
    initialized = runtime._cli_executable_version_attestation_snapshot
    assert initialized is not None
    assert initialized.filesystem_identity == (target.stat().st_dev, target.stat().st_ino)

    replacement = tmp_path / "replacement-target"
    replacement.write_text(script, encoding="utf-8")
    replacement.chmod(0o755)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    assert replacement_identity != initialized.filesystem_identity
    os.replace(replacement, target)

    # The launch path and its textual target did not change, but the effective
    # executable object did.
    assert cli_link.readlink() == Path(target.name)
    assert (
        runtime._cli_executable_content_identity()
        == runtime._cli_executable_content_identity_snapshot
    )
    current = runtime._cli_executable_version_attestation()
    assert current.filesystem_identity == replacement_identity
    assert (
        runtime._compare_cli_executable_version_attestations(initialized, current)
        is codex_cli_runtime_module._CliExecutableVersionState.CHANGED
    )
    with pytest.raises(RuntimeError, match="executable version changed"):
        runtime._verify_cli_executable_identity_unchanged()


def test_execution_identity_tracks_launch_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retargeting an identical CLI symlink must change execution identity."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    target_a = tmp_path / "codex-a"
    target_b = tmp_path / "codex-b"
    script = "#!/bin/sh\necho codex 1.0\n"
    target_a.write_text(script, encoding="utf-8")
    target_b.write_text(script, encoding="utf-8")
    target_a.chmod(0o755)
    target_b.chmod(0o755)
    link = tmp_path / "codex"
    link.symlink_to(target_a)

    first = CodexCliRuntime(cli_path=link, cwd="/tmp/project", model="gpt-5")
    first_identity = first.execution_identity_contract()

    link.unlink()
    link.symlink_to(target_b)
    second = CodexCliRuntime(cli_path=link, cwd="/tmp/project", model="gpt-5")
    second_identity = second.execution_identity_contract()

    assert (
        first_identity["cli_executable_content_sha256"]
        == second_identity["cli_executable_content_sha256"]
    )
    assert first_identity["cli_executable_version"] != second_identity["cli_executable_version"]


def test_build_command_rejects_bare_cli_that_appears_after_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATH command that was unresolved at init must not be launched later."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    runtime = CodexCliRuntime(cli_path="late-codex", cwd="/tmp/project", model="gpt-5")

    cli_path = tmp_path / "late-codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)

    with pytest.raises(RuntimeError, match="unresolved at runtime initialization"):
        runtime._verify_cli_executable_identity_unchanged()


def test_build_command_rejects_bare_cli_that_remains_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-resolved PATH command still has no executable identity."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    runtime = CodexCliRuntime(cli_path="missing-codex", cwd="/tmp/project", model="gpt-5")

    with pytest.raises(RuntimeError, match="unresolved at runtime initialization"):
        runtime._verify_cli_executable_identity_unchanged()


def test_copilot_unresolved_initialization_never_authorizes_later_path_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_path = tmp_path / "empty-path"
    hostile_path = tmp_path / "hostile-path"
    empty_path.mkdir()
    hostile_path.mkdir()
    marker = tmp_path / "hostile-ran"
    monkeypatch.setenv("PATH", str(empty_path))
    runtime = CopilotCliRuntime(cli_path="copilot", cwd=tmp_path, model="test-model")

    hostile_cli = hostile_path / "copilot"
    hostile_cli.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho copilot 1.0\n",
        encoding="utf-8",
    )
    hostile_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(hostile_path))

    with pytest.raises(RuntimeError, match="failed during runtime initialization"):
        runtime._verify_cli_executable_identity_unchanged()
    assert not marker.exists()


def test_skill_dispatch_registry_fingerprint_tracks_mcp_tool_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged skill frontmatter authority must participate in resume identity."""
    first = (
        SkillToolMapping(
            skill_name="auto",
            mcp_tool="ouroboros_start_auto",
            skill_path="skills/auto/SKILL.md",
            mcp_args={},
            context_keys=(),
        ),
    )
    changed = (
        SkillToolMapping(
            skill_name="auto",
            mcp_tool="ouroboros_run_seed",
            skill_path="skills/auto/SKILL.md",
            mcp_args={},
            context_keys=(),
        ),
    )
    monkeypatch.setattr(
        "ouroboros.orchestrator.codex_cli_runtime.discover_skill_tool_mappings",
        lambda _skills_dir=None: first,
    )
    runtime = CodexCliRuntime(cli_path="/bin/echo", cwd="/tmp/project", model="gpt-5")
    original = runtime.execution_identity_contract()["skill_dispatch_registry_fingerprint"]

    monkeypatch.setattr(
        "ouroboros.orchestrator.codex_cli_runtime.discover_skill_tool_mappings",
        lambda _skills_dir=None: changed,
    )

    assert runtime._fingerprint_skill_dispatch_registry() != original
    runtime._reconcile_skill_dispatch_registry()
    assert runtime._drift.epoch == 1
    assert (
        runtime._skill_dispatch_registry_fingerprint
        == runtime._fingerprint_skill_dispatch_registry()
    )


def test_skill_dispatch_guard_rejects_process_local_dispatcher_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-process dispatch authority must not be replaceable after init."""

    async def first_dispatcher(_intercept, _handle):
        return ()

    async def replacement_dispatcher(_intercept, _handle):
        return ()

    monkeypatch.setattr(
        "ouroboros.orchestrator.codex_cli_runtime.discover_skill_tool_mappings",
        lambda _skills_dir=None: (),
    )
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        cwd="/tmp/project",
        model="gpt-5",
        skill_dispatcher=first_dispatcher,
    )
    original = runtime.execution_identity_contract()["skill_dispatcher_identity"]

    runtime._skill_dispatcher = replacement_dispatcher

    assert runtime._fingerprint_skill_dispatcher(runtime._skill_dispatcher) != original
    runtime._reconcile_skill_dispatch_registry()
    assert runtime._drift.epoch == 1


def test_execution_identity_keeps_content_digest_when_version_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portable identity must still bind executable bytes when --version fails."""
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setattr(
        "ouroboros.orchestrator.codex_cli_runtime.discover_skill_tool_mappings",
        lambda _skills_dir=None: (),
    )

    first = CodexCliRuntime(cli_path=cli, cwd="/tmp/project", model="gpt-5")
    first_identity = first.execution_identity_contract()

    cli.write_text("#!/bin/sh\necho changed >&2\nexit 1\n", encoding="utf-8")
    second = CodexCliRuntime(cli_path=cli, cwd="/tmp/project", model="gpt-5")
    second_identity = second.execution_identity_contract()

    assert first_identity["cli_executable_version"] is None
    assert second_identity["cli_executable_version"] is None
    assert first_identity["cli_executable_content_sha256"]
    assert (
        first_identity["cli_executable_content_sha256"]
        != second_identity["cli_executable_content_sha256"]
    )


def test_builtin_mcp_handler_registry_fingerprint_rejects_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in MCP handler authority must participate in same-process guards."""
    monkeypatch.setattr(
        "ouroboros.orchestrator.codex_cli_runtime.discover_skill_tool_mappings",
        lambda _skills_dir=None: (),
    )
    runtime = CodexCliRuntime(cli_path="/bin/echo", cwd="/tmp/project", model="gpt-5")
    original = runtime.execution_identity_contract()["builtin_mcp_handler_registry_fingerprint"]

    class _ReplacementHandler:
        definition = {"name": "ouroboros_interview"}

    runtime._builtin_mcp_handlers = {"ouroboros_interview": _ReplacementHandler()}

    assert runtime._fingerprint_builtin_mcp_handler_registry() != original
    runtime._reconcile_skill_dispatch_registry()
    assert runtime._drift.epoch == 1


def test_codex_config_fingerprint_tracks_handle_selectable_embedded_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'model = "gpt-test"\n\n[profiles.unused]\nmodel = "unused-a"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    runtime._resolved_fallback_profile = "reachable"
    original = runtime._fingerprint_codex_config_files()

    config_path.write_text(
        'model = "gpt-test"\n\n[profiles.unused]\nmodel = "unused-b"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() != original


def test_codex_config_fingerprint_tracks_reachable_embedded_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'model = "gpt-test"\n\n[profiles.reachable]\nmodel = "reachable-a"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    runtime._resolved_fallback_profile = "reachable"
    original = runtime._fingerprint_codex_config_files()

    config_path.write_text(
        'model = "gpt-test"\n\n[profiles.reachable]\nmodel = "reachable-b"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() != original


def test_codex_config_fingerprint_tracks_handle_selectable_profile_v2_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    (codex_home / "reachable.config.toml").write_text(
        'model_provider = "proxy-a"\n',
        encoding="utf-8",
    )
    (codex_home / "unused.config.toml").write_text(
        'model_provider = "proxy-a"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    runtime._resolved_fallback_profile = "reachable"
    original = runtime._fingerprint_codex_config_files()

    (codex_home / "unused.config.toml").write_text(
        'model_provider = "proxy-b"\n',
        encoding="utf-8",
    )

    assert runtime._fingerprint_codex_config_files() != original


def test_codex_config_fingerprint_tracks_reachable_profile_v2_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    profile_path = codex_home / "reachable.config.toml"
    profile_path.write_text('model_provider = "proxy-a"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    runtime._resolved_fallback_profile = "reachable"
    original = runtime._fingerprint_codex_config_files()

    profile_path.write_text('model_provider = "proxy-b"\n', encoding="utf-8")

    assert runtime._fingerprint_codex_config_files() != original


def test_profile_fingerprint_preserves_v1_hash_when_effort_is_dormant() -> None:
    """Null effort fields must not invalidate a pre-effort resume contract."""
    config = OuroborosConfig(
        llm_profiles={
            "standard": {
                "providers": {"codex": {"profile": "ouroboros-standard"}},
            },
        },
        llm_role_profiles={"agent_runtime": "standard"},
    )
    legacy_payload = {
        "version": 1,
        "llm_profiles": {
            "standard": {
                "model": None,
                "providers": {
                    "codex": {
                        "model": None,
                        "profile": "ouroboros-standard",
                    },
                },
            },
        },
        "llm_role_profiles": {"agent_runtime": "standard"},
    }
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        assert runtime._fingerprint_profile_resolution_config() == runtime._hash_json_payload(
            legacy_payload
        )


def test_profile_fingerprint_tracks_handle_selectable_ouroboros_profiles() -> None:
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    first = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "reachable"}}},
        },
        llm_role_profiles={"agent_runtime": "standard"},
    )
    second = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "reachable"}}},
            "unused": {"providers": {"codex": {"profile": "unused"}}},
        },
        llm_role_profiles={"agent_runtime": "standard", "unused_role": "unused"},
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        original = runtime._fingerprint_profile_resolution_config()
    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        assert runtime._fingerprint_profile_resolution_config() != original


def test_profile_fingerprint_tracks_reachable_ouroboros_profile() -> None:
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    first = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "reachable-a"}}},
        },
        llm_role_profiles={"agent_runtime": "standard"},
    )
    second = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "reachable-b"}}},
        },
        llm_role_profiles={"agent_runtime": "standard"},
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        original = runtime._fingerprint_profile_resolution_config()
    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        assert runtime._fingerprint_profile_resolution_config() != original


def test_profile_fingerprint_tracks_runtime_profile_role_mapping_when_backend_profile_set() -> None:
    """Runtime-profile sessions still re-resolve role profiles for child handles."""
    first = OuroborosConfig(
        llm_profiles={
            "worker": {"providers": {"codex": {"profile": "ouroboros-worker"}}},
            "standard": {"providers": {"codex": {"reasoning_effort": "low"}}},
        },
        llm_role_profiles={"agent_runtime_implementation": "standard"},
    )
    second = OuroborosConfig(
        llm_profiles={
            "worker": {"providers": {"codex": {"profile": "ouroboros-worker"}}},
            "standard": {"providers": {"codex": {"reasoning_effort": "high"}}},
        },
        llm_role_profiles={"agent_runtime_implementation": "standard"},
    )
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project", runtime_profile="worker")

    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        original = runtime._fingerprint_profile_resolution_config()
    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        assert runtime._fingerprint_profile_resolution_config() != original


def test_handle_llm_profile_change_invalidates_cached_command_fingerprint() -> None:
    """Profiles selected through runtime handle metadata must be frozen per selector."""
    first = OuroborosConfig(
        llm_profiles={
            "implementation": {"providers": {"codex": {"model": "gpt-a"}}},
        },
    )
    second = OuroborosConfig(
        llm_profiles={
            "implementation": {"providers": {"codex": {"model": "gpt-b"}}},
        },
    )
    handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        metadata={"llm_profile": "implementation"},
    )
    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        command = runtime._build_command("/tmp/last-message", runtime_handle=handle)
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gpt-a"

    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        command = runtime._build_command("/tmp/last-message", runtime_handle=handle)
    # Drift is observed, retires existing threads, and the routing now on
    # disk is honored instead of failing the AC.
    assert runtime._drift.epoch == 1
    assert command[command.index("--model") + 1] == "gpt-b"


def test_handle_selectable_llm_profile_enters_durable_identity() -> None:
    """Runtime recreation must not accept changed direct handle profile inputs."""
    first = OuroborosConfig(
        llm_profiles={
            "implementation": {"providers": {"codex": {"model": "gpt-a"}}},
        },
    )
    second = OuroborosConfig(
        llm_profiles={
            "implementation": {"providers": {"codex": {"model": "gpt-b"}}},
        },
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        original = CodexCliRuntime(
            cli_path="/bin/echo", cwd="/tmp/project"
        ).execution_identity_contract()
    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        changed = CodexCliRuntime(
            cli_path="/bin/echo", cwd="/tmp/project"
        ).execution_identity_contract()

    assert original["profile_resolution_fingerprint"] != changed["profile_resolution_fingerprint"]


def test_runtime_execution_identity_tracks_constructor_execution_inputs(
    tmp_path: Path,
) -> None:
    """Resume identity must include constructor inputs that affect execution behavior."""
    first_skills = tmp_path / "skills-a"
    second_skills = tmp_path / "skills-b"
    first_skills.mkdir()
    second_skills.mkdir()

    first = CodexCliRuntime(
        cli_path="/bin/echo",
        cwd="/tmp/project",
        skills_dir=first_skills,
        startup_output_timeout_seconds=1,
        stdout_idle_timeout_seconds=2,
    ).execution_identity_contract()
    second = CodexCliRuntime(
        cli_path="/bin/echo",
        cwd="/tmp/project",
        skills_dir=second_skills,
        startup_output_timeout_seconds=3,
        stdout_idle_timeout_seconds=4,
    ).execution_identity_contract()

    assert first["skills_dir"] == str(first_skills)
    assert first["startup_output_timeout_seconds"] == 1
    assert first["stdout_idle_timeout_seconds"] == 2
    assert first["skills_dir"] != second["skills_dir"]
    assert first["startup_output_timeout_seconds"] != second["startup_output_timeout_seconds"]
    assert first["stdout_idle_timeout_seconds"] != second["stdout_idle_timeout_seconds"]


def test_handle_codex_profile_file_change_invalidates_cached_command_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex-native profiles selected through handle metadata are command inputs."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    profile_path = codex_home / "custom.config.toml"
    profile_path.write_text('model_provider = "proxy-a"\n', encoding="utf-8")
    handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        metadata={"codex_profile": "custom"},
    )
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    command = runtime._build_command("/tmp/last-message", runtime_handle=handle)
    assert "--profile" in command
    assert command[command.index("--profile") + 1] == "custom"

    profile_path.write_text('model_provider = "proxy-b"\n', encoding="utf-8")
    command = runtime._build_command("/tmp/last-message", runtime_handle=handle)
    assert runtime._drift.epoch == 1
    assert command[command.index("--profile") + 1] == "custom"


def test_profile_resolution_fingerprint_canonicalizes_duplicate_codex_alias_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate Codex aliases are one invalid state regardless of insertion order."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    first = OuroborosConfig(
        llm_profiles={
            "qa": {
                "providers": {
                    "CODEX": {"model": "first-pin"},
                    "codex_cli": {"model": "second-pin"},
                }
            }
        },
        llm_role_profiles={"agent_runtime": "qa"},
    )
    second = OuroborosConfig(
        llm_profiles={
            "qa": {
                "providers": {
                    "codex_cli": {"model": "second-pin"},
                    "CODEX": {"model": "first-pin"},
                }
            }
        },
        llm_role_profiles={"agent_runtime": "qa"},
    )
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    with patch("ouroboros.providers.profiles.load_config", return_value=first):
        first_fingerprint = runtime._fingerprint_profile_resolution_config()
    with patch("ouroboros.providers.profiles.load_config", return_value=second):
        second_fingerprint = runtime._fingerprint_profile_resolution_config()

    assert first_fingerprint == second_fingerprint


def test_profile_resolution_fingerprint_canonicalizes_single_codex_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent single Codex aliases must not cause replay fingerprint drift."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    lower = OuroborosConfig(
        llm_profiles={"qa": {"providers": {"codex": {"model": "gpt-5"}}}},
        llm_role_profiles={"agent_runtime": "qa"},
    )
    upper = OuroborosConfig(
        llm_profiles={"qa": {"providers": {"CODEX": {"model": "gpt-5"}}}},
        llm_role_profiles={"agent_runtime": "qa"},
    )
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    with patch("ouroboros.providers.profiles.load_config", return_value=lower):
        lower_fingerprint = runtime._fingerprint_profile_resolution_config()
    with patch("ouroboros.providers.profiles.load_config", return_value=upper):
        upper_fingerprint = runtime._fingerprint_profile_resolution_config()

    assert lower_fingerprint == upper_fingerprint


class TestComposePromptDirectiveFencing:
    """System instructions and tooling must be fenced as binding directives."""

    def _runtime(self) -> CodexCliRuntime:
        return CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    def test_system_prompt_wrapped_in_authority_delimiter(self) -> None:
        composed = self._runtime()._compose_prompt("Do the thing", "Be terse.", None)
        assert "<system-directive>" in composed
        assert "</system-directive>" in composed
        # The binding preamble must precede the system text inside the fence.
        assert "binding instructions" in composed
        assert "Be terse." in composed
        # The markdown heading the model used to read as content is gone.
        assert "## System Instructions" not in composed

    def test_tools_wrapped_in_tooling_guidance_delimiter(self) -> None:
        composed = self._runtime()._compose_prompt("Do the thing", None, ["Read", "Edit"])
        assert "<tooling-guidance>" in composed
        assert "</tooling-guidance>" in composed
        assert "- Read" in composed
        assert "- Edit" in composed
        assert "## Tooling Guidance" not in composed

    def test_bare_prompt_passthrough_unchanged(self) -> None:
        # No system instructions and no tools → the task text is returned as-is,
        # with no delimiters added (preserves prior behavior).
        composed = self._runtime()._compose_prompt("Do the thing", None, None)
        assert composed == "Do the thing"

    def test_task_text_is_not_wrapped(self) -> None:
        composed = self._runtime()._compose_prompt("Do the thing", "Be terse.", None)
        # Directive fences must not swallow the task text itself.
        assert "Do the thing" in composed
        assert composed.rstrip().endswith("Do the thing")


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        encoded = "".join(f"{line}\n" for line in lines).encode()
        self._buffer = bytearray(encoded)

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        newline_index = self._buffer.find(b"\n")
        if newline_index < 0:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[: newline_index + 1])
        del self._buffer[: newline_index + 1]
        return data

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n < 0 or n >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data


class _FailingReadlineStream(_FakeStream):
    async def readline(self) -> bytes:
        msg = "readline() should not be used for Codex CLI stream parsing"
        raise AssertionError(msg)


class _FakeStdin:
    """Fake stdin that captures written data."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int = 0,
        *,
        stdout_stream: _FakeStream | None = None,
        stderr_stream: _FakeStream | None = None,
        pid: int | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout_stream or _FakeStream(stdout_lines)
        self.stderr = stderr_stream or _FakeStream(stderr_lines)
        self.pid = pid
        self.returncode: int | None = None
        self._returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode


class _BlockingStream:
    async def readline(self) -> bytes:
        await asyncio.Future()  # type: ignore[misc]
        return b""  # unreachable, satisfies mypy

    async def read(self, n: int = -1) -> bytes:
        del n
        await asyncio.Future()  # type: ignore[misc]
        return b""  # unreachable, satisfies mypy


class _TerminableProcess:
    def __init__(self) -> None:
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return -1 if self.returncode is None else self.returncode


class _ControlledBlockingStream:
    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

    async def readline(self) -> bytes:
        await self._done.wait()
        return b""

    async def read(self, n: int = -1) -> bytes:
        del n
        await self._done.wait()
        return b""


class _TimeoutTerminableProcess:
    def __init__(self) -> None:
        self._done = asyncio.Event()
        self.stdout = _ControlledBlockingStream(self._done)
        self.stderr = _ControlledBlockingStream(self._done)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return -1 if self.returncode is None else self.returncode


class TestCodexCliRuntime:
    """Tests for CodexCliRuntime."""

    @staticmethod
    def _write_wrapper(path: Path) -> Path:
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32 + b"zeude codex-wrapper")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_real_cli(path: Path) -> Path:
        path.write_text("#!/usr/bin/env node\nconsole.log('codex')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        frontmatter_lines: list[str],
    ) -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        frontmatter = "\n".join(frontmatter_lines)
        skill_md.write_text(
            f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
            encoding="utf-8",
        )
        return skill_md

    @pytest.mark.asyncio
    async def test_relative_cwd_is_frozen_for_command_and_subprocess(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        workspace = launch_cwd / "workspace"
        later_cwd = tmp_path / "later"
        workspace.mkdir(parents=True)
        later_cwd.mkdir()
        monkeypatch.chdir(launch_cwd)
        runtime = CodexCliRuntime(cli_path="codex", cwd="workspace")
        captured: dict[str, object] = {}

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        monkeypatch.chdir(later_cwd)
        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("run")]

        command = captured["command"]
        assert isinstance(command, tuple)
        assert runtime.working_directory == str(workspace)
        assert captured["cwd"] == str(workspace)
        assert command[command.index("-C") + 1] == str(workspace)
        assert messages[-1].data["subtype"] == "success"

    def test_build_command_for_new_session(self) -> None:
        """Builds a new-session exec command (prompt fed via stdin, not args)."""
        runtime = CodexCliRuntime(
            cli_path=_test_cli_path(),
            permission_mode="acceptEdits",
            model="o3",
            cwd="/tmp/project",
        )

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert command[:2] == [_test_cli_path(), "exec"]
        assert "--json" in command
        assert "--full-auto" not in command
        assert "workspace-write" in command
        assert "--model" in command
        assert "o3" in command
        assert "-C" in command
        assert _EXPECTED_PROJECT_CWD in command

    def test_build_command_for_resume(self) -> None:
        """Builds an exec resume command when a session id is provided."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            resume_session_id="thread-123",
        )

        assert command[1] == "exec"
        assert command[-2:] == ["resume", "thread-123"]
        resume_index = command.index("resume")
        assert command.index("--json") < resume_index
        assert command.index("--skip-git-repo-check") < resume_index
        assert command.index("--output-last-message") < resume_index
        assert command.index("-C") < resume_index
        assert command[command.index("-C") + 1] == _EXPECTED_PROJECT_CWD

    def test_build_command_uses_effort_for_runtime_session_role(self) -> None:
        """Agent runtime sessions should pass role effort without a Codex profile file."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"reasoning_effort": "medium"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" not in command
        assert "model_reasoning_effort=medium" in command
        assert "--model" not in command

    def test_build_command_keeps_role_effort_with_explicit_model_pin(self) -> None:
        """A stage model pin replaces only the role model, not its effort level."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"reasoning_effort": "high"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project", model="terra")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert command[command.index("--model") + 1] == "terra"
        assert "model_reasoning_effort=high" in command
        assert "--profile" not in command

    def test_build_command_matches_codex_0134_unified_profile_v2_contract(self) -> None:
        """Codex 0.134 uses --profile to load ~/.codex/<name>.config.toml files."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "frontier": {
                    "providers": {"codex": {"profile": "ouroboros-frontier"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "frontier"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert "--profile-v2" not in command
        assert command[command.index("--profile") + 1] == "ouroboros-frontier"

    def test_build_command_uses_default_runtime_profile_for_resumed_roleless_handle(self) -> None:
        """Role-less resumes keep the fallback profile frozen at runtime construction."""
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime": "standard"},
        )
        drifted_config = OuroborosConfig(
            llm_profiles={
                "frontier": {
                    "providers": {"codex": {"profile": "drifted-frontier"}},
                },
            },
            llm_role_profiles={"agent_runtime": "frontier"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="agent_runtime",
            native_session_id="thread-123",
            metadata={},
        )

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=drifted_config,
        ):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
                resume_session_id="thread-123",
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-standard"
        assert "--model" not in command

    def test_build_command_rejects_mid_run_role_profile_drift(self) -> None:
        """Explicit-role commands cannot re-read a changed profile mapping mid-run."""
        original_config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )
        drifted_config = OuroborosConfig(
            llm_profiles={
                "frontier": {
                    "providers": {"codex": {"profile": "drifted-frontier"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "frontier"},
        )
        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=original_config,
        ):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=drifted_config,
        ):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )
        # The role remap changes both Ouroboros routing and which native
        # Codex profile file is in force: each is observed, neither is fatal,
        # and the command follows the routing now on disk.
        assert runtime._drift.epoch >= 1
        assert command[command.index("--profile") + 1] == "drifted-frontier"

    def test_build_command_does_not_double_prefix_prefixed_runtime_handle_kind(self) -> None:
        """Already-prefixed runtime handle kinds are treated as logical role keys."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="agent_runtime_evaluation",
            metadata={},
        )
        config = OuroborosConfig(
            llm_profiles={
                "deep": {
                    "providers": {"codex": {"profile": "ouroboros-deep"}},
                },
            },
            llm_role_profiles={"agent_runtime_evaluation": "deep"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-deep"
        assert "--model" not in command

    def test_runtime_profile_prevents_duplicate_role_profile_flags(self) -> None:
        """Worker isolation owns Codex's singular --profile flag when both resolve."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(
                cli_path="codex",
                cwd="/tmp/project",
                runtime_profile="worker",
            )
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert command.count("--profile") == 1
        assert command[command.index("--profile") + 1] == "ouroboros-worker"
        assert "ouroboros-standard" not in command

    def test_build_command_uses_runtime_profile_provider_model_fallback(self) -> None:
        """Codex runtime profiles without Codex-native profile anchors should use models."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"model": "gpt-5.3-codex"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.3-codex"
        assert "--profile" not in command

    def test_build_command_uses_runtime_profile_top_level_model_fallback(self) -> None:
        """Agent runtime should honor provider-neutral profile model fallback."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={"standard": {"model": "gpt-5.3-codex"}},
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.3-codex"
        assert "--profile" not in command

    def test_build_command_explicit_model_wins_over_runtime_profile(self) -> None:
        """Explicit runtime model overrides keep existing --model behavior."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", model="gpt-5.5", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.5"
        assert "--profile" not in command

    def test_build_command_uses_explicit_runtime_profile_metadata(self) -> None:
        """Runtime metadata can directly select an Ouroboros profile."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"llm_profile": "deep"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "deep": {
                    "providers": {"codex": {"profile": "ouroboros-deep"}},
                },
            },
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-deep"

    def test_build_command_uses_explicit_runtime_profile_model_fallback(self) -> None:
        """Explicit Ouroboros profile metadata should still fall back to model."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"llm_profile": "deep"},
        )
        config = OuroborosConfig(llm_profiles={"deep": {"model": "gpt-5.5"}})

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.5"
        assert "--profile" not in command

    def test_build_command_rejects_first_use_explicit_llm_profile_drift(self) -> None:
        """First use of handle-selected llm_profile must compare with init-time identity."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"llm_profile": "deep"},
        )
        original_config = OuroborosConfig(llm_profiles={"deep": {"model": "gpt-a"}})
        drifted_config = OuroborosConfig(llm_profiles={"deep": {"model": "gpt-b"}})

        with patch("ouroboros.providers.profiles.load_config", return_value=original_config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        with patch("ouroboros.providers.profiles.load_config", return_value=drifted_config):
            runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )
        assert runtime._drift.epoch == 1

    def test_build_command_allows_first_use_arbitrary_llm_role_from_init_identity(self) -> None:
        """Arbitrary llm_role metadata must be included in the initialization fingerprint."""
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="qa_session",
            metadata={"llm_role": "qa"},
        )
        config = OuroborosConfig(
            llm_profiles={"qa-profile": {"providers": {"codex": {"model": "gpt-qa"}}}},
            llm_role_profiles={"qa": "qa-profile"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-qa"

    def test_build_command_rejects_first_use_explicit_codex_profile_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First use of handle-selected codex_profile must compare native profile files."""
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("", encoding="utf-8")
        (codex_home / "deep.config.toml").write_text('model = "gpt-a"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"codex_profile": "deep"},
        )

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=OuroborosConfig(),
        ):
            runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        (codex_home / "deep.config.toml").write_text('model = "gpt-b"\n', encoding="utf-8")

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=OuroborosConfig(),
        ):
            runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )
        assert runtime._drift.epoch == 1

    def test_build_command_omits_profile_flag_when_runtime_profile_unset(self) -> None:
        """Default runtime_profile=None preserves existing command shape (regression)."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=OuroborosConfig(),
        ):
            command = runtime._build_command(output_last_message_path="/tmp/out.txt")

        assert "--profile" not in command

    def test_build_command_adds_worker_profile_when_configured(self) -> None:
        """runtime_profile='worker' maps to Codex `--profile ouroboros-worker`."""
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            runtime_profile="worker",
        )

        command = runtime._build_command(output_last_message_path="/tmp/out.txt")

        assert "--profile" in command
        profile_index = command.index("--profile")
        assert command[profile_index + 1] == "ouroboros-worker"
        # Profile must come before the rest of the args so Codex resolves
        # the profile-managed defaults before per-flag overrides.
        assert profile_index < command.index("--json")

    def test_build_command_skips_unknown_runtime_profile_with_warning(self) -> None:
        """Unmapped runtime_profile values fall back to no profile flag and log a warning."""
        with patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning:
            runtime = CodexCliRuntime(
                cli_path="codex",
                cwd="/tmp/project",
                runtime_profile="future-tier",
            )

        with patch(
            "ouroboros.providers.profiles.load_config",
            return_value=OuroborosConfig(),
        ):
            command = runtime._build_command(output_last_message_path="/tmp/out.txt")

        assert "--profile" not in command
        mock_warning.assert_called_once()
        warning_args = mock_warning.call_args
        assert warning_args.args[0] == "codex_cli_runtime.runtime_profile_unmapped"
        assert warning_args.kwargs["runtime_profile"] == "future-tier"

    def test_resolve_cli_path_falls_back_from_wrapper(self, tmp_path: Path) -> None:
        """Runtime should bypass wrappers the same way provider adapters do."""
        wrapper = self._write_wrapper(tmp_path / "codex-wrapper")
        real_dir = tmp_path / "bin"
        real_dir.mkdir()
        real_cli = self._write_real_cli(real_dir / "codex")

        with (
            patch.dict(os.environ, {"PATH": str(real_dir)}),
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
        ):
            runtime = CodexCliRuntime(cli_path=wrapper)

        assert runtime._cli_path == str(real_cli)
        mock_warning.assert_called_once_with(
            "codex_cli_runtime.cli_wrapper_detected",
            wrapper_path=str(wrapper),
            hint="Searching PATH for the real Codex CLI.",
        )
        mock_info.assert_any_call(
            "codex_cli_runtime.cli_resolved_via_fallback",
            fallback_path=str(real_cli),
        )

    def test_build_command_uses_read_only_for_default_permission_mode(self) -> None:
        """Default permission mode keeps the runtime in read-only mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="default")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert "--sandbox" in command
        assert "read-only" in command

    def test_build_command_uses_dangerous_bypass_for_bypass_permissions(self) -> None:
        """bypassPermissions uses Codex's no-approval/no-sandbox mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="bypassPermissions")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in command

    @pytest.mark.asyncio
    async def test_execute_task_marks_resume_bootstrap_failures_recoverable(self) -> None:
        """Resume failures before any Codex event should stay retryable."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            del command, kwargs
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=["error: unexpected argument '-C' found"],
                returncode=2,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task(
                    "resume the task",
                    resume_session_id="thread-123",
                )
            ]

        assert len(messages) == 1
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "CodexCliError"
        assert messages[0].data["recoverable"] is True
        assert messages[0].data["recovery"]["kind"] == "resume_retry"
        assert messages[0].data["recovery"]["resume_session_id"] == "thread-123"

    @pytest.mark.asyncio
    async def test_execute_task_surfaces_model_pin_version_guidance(self) -> None:
        """Execute-stage model pins receive the same App/CLI mismatch guidance."""
        runtime = CodexCliRuntime(cli_path=_test_cli_path(), cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            del command, kwargs
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=["Codex: model not found"],
                returncode=1,
            )

        versions = {
            _test_cli_path(): "codex-cli 0.139.0",
            "/Applications/ChatGPT.app/Contents/Resources/codex": "codex-cli 0.140.0",
        }
        with (
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch(
                "ouroboros.providers.codex_cli_adapter.CodexCliLLMAdapter._codex_version",
                side_effect=lambda path: versions.get(path),
            ),
        ):
            messages = [message async for message in runtime.execute_task("run the task")]

        result = messages[-1]
        assert result.is_error
        assert result.data["failure_category"] == "codex_model_unavailable"
        assert result.data["codex_app_cli_versions_match"] is False
        assert "Update both Codex installations" in result.content

    @pytest.mark.asyncio
    async def test_execute_task_surfaces_model_guidance_from_turn_failed_event(self) -> None:
        """JSONL turn failures must not bypass App/CLI mismatch diagnostics."""
        runtime = CodexCliRuntime(cli_path=_test_cli_path(), cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            del command, kwargs
            return _FakeProcess(
                stdout_lines=[
                    json.dumps({"type": "turn.failed", "error": {"message": "model not found"}})
                ],
                stderr_lines=[],
                returncode=1,
            )

        versions = {
            _test_cli_path(): "codex-cli 0.139.0",
            "/Applications/ChatGPT.app/Contents/Resources/codex": "codex-cli 0.140.0",
        }
        with (
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch(
                "ouroboros.providers.codex_cli_adapter.CodexCliLLMAdapter._codex_version",
                side_effect=lambda path: versions.get(path),
            ),
        ):
            messages = [message async for message in runtime.execute_task("run the task")]

        result = messages[-1]
        assert result.is_error
        assert result.data["failure_category"] == "codex_model_unavailable"
        assert result.data["codex_app_cli_versions_match"] is False
        assert "Update both Codex installations" in result.content

    @pytest.mark.asyncio
    async def test_execute_task_starts_codex_in_dedicated_process_session(self) -> None:
        """Codex workers run in their own session so cleanup can reap descendants."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime._use_process_group = True
        runtime._completed_process_group_shutdown_timeout_seconds = 0.001
        captured_kwargs: dict[str, Any] = {}
        sent_signals: list[int] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            del command
            captured_kwargs.update(kwargs)
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=[],
                returncode=0,
                pid=4242,
            )

        def fake_killpg(_pgid: int, sig: int) -> None:
            sent_signals.append(sig)

        with (
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch("ouroboros.orchestrator.codex_cli_runtime.os.getpgid", return_value=4242),
            patch("ouroboros.providers.codex_cli_stream.os.killpg", side_effect=fake_killpg),
        ):
            messages = [message async for message in runtime.execute_task("complete the task")]

        assert captured_kwargs["start_new_session"] is True
        assert sent_signals == [signal.SIGTERM, signal.SIGKILL]
        assert messages[-1].content == "Codex CLI task completed."

    def test_convert_thread_started_event(self) -> None:
        """Converts thread.started to a system message with a resume handle."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {"type": "thread.started", "thread_id": "thread-123"},
            current_handle=None,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.type == "system"
        assert message.resume_handle is not None
        assert message.resume_handle.backend == "codex_cli"
        assert message.resume_handle.native_session_id == "thread-123"

    def test_convert_thread_started_event_preserves_existing_handle_metadata(self) -> None:
        """Fresh runtime handles retain pre-seeded scope metadata when the thread starts."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        seeded_handle = RuntimeHandle(
            backend="codex_cli",
            kind="level_coordinator",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "level",
                "level_number": 2,
                "session_role": "coordinator",
            },
        )

        messages = runtime._convert_event(
            {"type": "thread.started", "thread_id": "thread-123"},
            current_handle=seeded_handle,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.resume_handle is not None
        assert message.resume_handle.native_session_id == "thread-123"
        assert message.resume_handle.kind == "level_coordinator"
        assert message.resume_handle.cwd == seeded_handle.cwd
        assert message.resume_handle.approval_mode == "acceptEdits"
        assert message.resume_handle.metadata == {
            **seeded_handle.metadata,
            "ouroboros_runtime_drift_epoch": 0,
        }

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (
                {"type": "thread.started", "thread_id": "thread-123", "model": "gpt-5.4"},
                ("gpt-5.4", "runtime_stream:thread.started:event.model"),
            ),
            (
                {
                    "type": "turn.completed",
                    "session": {"selected_model": "gpt-5.4-mini"},
                },
                ("gpt-5.4-mini", "runtime_stream:turn.completed:session.selected_model"),
            ),
            # Item payloads can contain arbitrary provider/tool metadata. They
            # are not a Codex lifecycle declaration of the effective model.
            (
                {"type": "item.completed", "item": {"model": "gpt-pretend"}},
                None,
            ),
            # A text sentence is never a model identifier, even on a lifecycle event.
            (
                {"type": "turn.started", "model": "the model is gpt-pretend"},
                None,
            ),
        ],
    )
    def test_runtime_reported_model_requires_a_lifecycle_model_field(
        self,
        event: dict[str, object],
        expected: tuple[str, str] | None,
    ) -> None:
        """Automatic-mode telemetry never promotes configuration or item text to fact."""
        assert CodexCliRuntime._runtime_reported_model(event) == expected

    def test_convert_command_execution_event(self) -> None:
        """Completed-only command items synthesize a Bash start+result pair."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest -q"},
            },
            current_handle=None,
        )

        assert len(messages) == 2
        start, result = messages
        assert start.tool_name == "Bash"
        assert start.data["tool_input"]["command"] == "pytest -q"
        assert start.data.get("subtype") is None
        assert result.tool_name == "Bash"
        assert result.data["tool_input"]["command"] == "pytest -q"
        assert result.data["subtype"] == "tool_result"

    def test_convert_command_execution_preserves_output_metadata(self) -> None:
        """Command output must remain available for fat-harness verification."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "pytest",
                    "output": "1 passed in 0.01s",
                    "exit_code": 0,
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2
        result = messages[1]
        assert result.tool_name == "Bash"
        assert result.data["tool_input"]["command"] == "pytest"
        assert result.data["output"] == "1 passed in 0.01s"
        assert result.data["exit_code"] == 0
        assert result.data["is_error"] is False

    def test_convert_command_execution_preserves_aggregated_output_metadata(self) -> None:
        """Current Codex JSONL aggregated output remains available to the verifier."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": ("/bin/zsh -lc 'python3 -m pytest --doctest-modules -q hello.py'"),
                    "aggregated_output": ". [100%]\n1 passed in 0.01s\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2
        result = messages[1]
        assert result.data["output"] == ". [100%]\n1 passed in 0.01s"
        assert result.data["exit_code"] == 0
        assert result.data["status"] == "completed"
        assert result.data["is_error"] is False

    def test_convert_command_execution_preserves_nested_output_metadata(self) -> None:
        """Codex command result fields may arrive under nested output/result objects."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'python -m pytest test_hello.py'",
                    "output": {
                        "stdout": "1 passed in 0.01s",
                        "exit_code": 0,
                    },
                    "result": {"status": "completed", "success": True},
                },
            },
            current_handle=None,
        )

        assert len(messages) == 2
        result = messages[1]
        assert result.tool_name == "Bash"
        assert result.data["tool_input"]["command"] == (
            "/bin/zsh -lc 'python -m pytest test_hello.py'"
        )
        assert result.data["stdout"] == "1 passed in 0.01s"
        assert result.data["exit_code"] == 0
        assert result.data["status"] == "completed"
        # The explicit success signal is carried by the tri-state is_error
        # instead of a "success" subtype (fail-closed, #1692 blocker 1).
        assert result.data["subtype"] == "tool_result"
        assert result.data["is_error"] is False

    def test_convert_file_change_event_emits_each_changed_file(self) -> None:
        """Multi-file Codex changes should create one start+result pair per path."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {"path": "/tmp/project/hello.py"},
                        {"path": "/tmp/project/test_hello.py"},
                    ],
                },
            },
            current_handle=None,
        )

        assert [message.tool_name for message in messages] == ["Edit"] * 4
        assert [message.data["tool_input"]["file_path"] for message in messages] == [
            "/tmp/project/hello.py",
            "/tmp/project/hello.py",
            "/tmp/project/test_hello.py",
            "/tmp/project/test_hello.py",
        ]
        results = [message for message in messages if message.data.get("subtype") == "tool_result"]
        assert len(results) == 2
        # A completion without an explicit status must never be stamped
        # success (fail-closed, #1692 blocker 1): no success subtype, no
        # is_error verdict, no hardcoded tool.completed lifecycle stamp.
        assert all(message.data.get("subtype") != "success" for message in messages)
        assert all("is_error" not in message.data for message in results)
        assert all(
            message.data.get("runtime_event_type") != "tool.completed" for message in messages
        )

    def test_convert_turn_completed_with_usage_emits_system_usage_message(self) -> None:
        """turn.completed with token usage surfaces a non-final system message."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 512,
                    "cached_input_tokens": 128,
                    "output_tokens": 64,
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.type == "system"
        assert message.content == ""
        assert message.is_final is False
        assert message.data["subtype"] == "turn.completed"
        assert message.data["usage"] == {
            "input_tokens": 512,
            "cached_input_tokens": 128,
            "output_tokens": 64,
        }

    def test_convert_turn_completed_rejects_partially_malformed_usage(self) -> None:
        """One malformed known counter is preserved as an attempt-level veto."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": "512",  # string → dropped
                    "output_tokens": 64,  # valid → kept
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        assert messages[0].data == {
            "subtype": "turn.completed",
            "usage_invalid": True,
        }

    @pytest.mark.parametrize("invalid_total", ["576", -1, float("nan"), True])
    def test_convert_turn_completed_does_not_fallback_from_invalid_total(
        self,
        invalid_total: object,
    ) -> None:
        """A present invalid total cannot fall back to a smaller component sum."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "turn.completed",
                "usage": {
                    "total_tokens": invalid_total,
                    "input_tokens": 512,
                    "output_tokens": 64,
                },
            },
            current_handle=None,
        )

        assert len(messages) == 1
        assert messages[0].data["usage_invalid"] is True

    def test_convert_turn_completed_without_usage_is_dropped(self) -> None:
        """turn.completed without a usable usage object stays a dropped event."""
        runtime = CodexCliRuntime(cli_path="codex")

        assert runtime._convert_event({"type": "turn.completed"}, current_handle=None) == []
        malformed = runtime._convert_event(
            {"type": "turn.completed", "usage": {"input_tokens": "x"}},
            current_handle=None,
        )
        assert len(malformed) == 1
        assert malformed[0].data["usage_invalid"] is True

    def test_convert_turn_completed_ignores_unknown_only_usage_shape(self) -> None:
        """Provider diagnostics with no recognized token counter are not corruption."""
        runtime = CodexCliRuntime(cli_path="codex")

        assert (
            runtime._convert_event(
                {"type": "turn.completed", "usage": {"request_id": "req-1"}},
                current_handle=None,
            )
            == []
        )

    def test_runtime_does_not_expose_local_dispatch_parser_helpers(self) -> None:
        """Dispatch parsing and metadata resolution live in the shared router."""
        obsolete_helpers = {
            "_extract_first_argument",
            "_load_skill_frontmatter",
            "_normalize_mcp_frontmatter",
            "_resolve_dispatch_templates",
            "_resolve_skill_dispatch",
            "_resolve_skill_intercept",
        }

        assert obsolete_helpers.isdisjoint(dir(CodexCliRuntime))

    def test_runtime_source_does_not_reference_removed_dispatch_parser_helpers(self) -> None:
        """Removed local parser helpers should not remain referenced by the runtime."""
        runtime_source = inspect.getsource(codex_cli_runtime_module)
        obsolete_helper_references = {
            "_extract_first_argument(",
            "_load_skill_frontmatter(",
            "_normalize_mcp_frontmatter(",
            "_resolve_dispatch_templates(",
            "_resolve_skill_dispatch(",
            "_resolve_skill_intercept(",
            "SkillInterceptRequest",
        }

        assert all(reference not in runtime_source for reference in obsolete_helper_references)

    @pytest.mark.asyncio
    async def test_execute_task_routes_ooo_input_through_shared_stateless_router(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex CLI runtime should pass through the router's Resolved result."""
        resolved_sentinel = Resolved(
            skill_name="router-skill",
            command_prefix="ooo router-skill",
            prompt="ooo run seed.yaml",
            skill_path=tmp_path / "router-skill" / "SKILL.md",
            mcp_tool="router_only_tool",
            mcp_args={
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            },
            first_argument="resolved-first-argument",
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch.object(
                SharedSkillDispatchRouter,
                "resolve",
                autospec=True,
                return_value=resolved_sentinel,
            ) as mock_resolve,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_resolve.assert_called_once()
        assert isinstance(mock_resolve.call_args.args[0], SharedSkillDispatchRouter)
        request = mock_resolve.call_args.args[1]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo run seed.yaml"
        assert request.cwd == _EXPECTED_PROJECT_CWD
        assert request.skills_dir == tmp_path
        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request is resolved_sentinel
        assert dispatcher.await_args.args[1] is None
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_builtin_dispatcher_consumes_resolved_router_result(
        self,
        tmp_path: Path,
    ) -> None:
        """Built-in dispatch should consume Resolved metadata without re-parsing."""
        resolved = Resolved(
            skill_name="router-skill",
            command_prefix="ooo router-skill",
            prompt="ooo run prompt-derived.yaml",
            skill_path=tmp_path / "router-skill" / "SKILL.md",
            mcp_tool="router_only_tool",
            mcp_args={
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            },
            first_argument="resolved-first-argument",
        )
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Router dispatch"),),
                    meta={"execution_id": "exec-router"},
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch.object(
                runtime, "_get_mcp_tool_handler", return_value=fake_handler
            ) as mock_lookup,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [
                message async for message in runtime.execute_task("ooo run prompt-derived.yaml")
            ]

        mock_resolve.assert_called_once()
        mock_lookup.assert_called_once_with("router_only_tool")
        fake_handler.handle.assert_awaited_once_with(
            {
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            }
        )
        mock_exec.assert_not_called()
        assert messages[0].tool_name == "router_only_tool"
        assert messages[0].data["tool_input"] == {
            "seed_path": "resolved-by-router.yaml",
            "nested": {"source": "router"},
        }
        assert messages[0].data["skill_name"] == "router-skill"
        assert messages[0].data["command_prefix"] == "ooo router-skill"
        assert messages[1].content == "Router dispatch"
        assert messages[1].data["execution_id"] == "exec-router"

    @pytest.mark.asyncio
    async def test_execute_task_streams_messages_and_final_result(self) -> None:
        """Streams parsed JSON events and returns the final output file content."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout_lines=[
                    json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "content": [{"text": "Working..."}],
                            },
                        }
                    ),
                ],
                stderr_lines=[],
                returncode=0,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert [message.type for message in messages] == ["system", "assistant", "result"]
        assert messages[-1].content == "Final answer"
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"
        assert messages[-1].data["model_observation"] == {
            "mode": "automatic",
            "status": "unreported",
            "requested_model": None,
            "effective_model": None,
            "source": None,
        }

    @pytest.mark.asyncio
    async def test_execute_task_surfaces_only_runtime_reported_effective_model(self) -> None:
        """A stream declaration upgrades automatic mode from unreported to observed."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout_lines=[
                    json.dumps(
                        {
                            "type": "thread.started",
                            "thread_id": "thread-123",
                            "model": "gpt-5.4",
                        }
                    ),
                ],
                stderr_lines=[],
                returncode=0,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        model_message = next(
            message for message in messages if message.data.get("subtype") == "model.observed"
        )
        assert model_message.content == "Codex selected model: gpt-5.4"
        assert model_message.data["model_observation"] == {
            "mode": "automatic",
            "status": "observed",
            "requested_model": None,
            "effective_model": "gpt-5.4",
            "source": "runtime_stream:thread.started:event.model",
        }
        assert messages[-1].data["model_observation"] == model_message.data["model_observation"]

    @pytest.mark.asyncio
    async def test_execute_task_handles_large_jsonl_events_without_readline(self) -> None:
        """Large Codex JSONL events should stream without relying on readline()."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        large_text = "A" * 200_000

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            stdout_lines = [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "content": [{"text": large_text}],
                        },
                    }
                ),
            ]
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=[],
                returncode=0,
                stdout_stream=_FailingReadlineStream(stdout_lines),
                stderr_stream=_FailingReadlineStream([]),
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert [message.type for message in messages] == ["system", "assistant", "result"]
        assert messages[1].content == large_text
        assert messages[-1].content == "Final answer"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_intercept_frontmatter_is_invalid(
        self,
        tmp_path: Path,
    ) -> None:
        """Invalid frontmatter bypasses intercept and preserves the original prompt."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                "mcp_tool: ouroboros_help",
                "mcp_args:",
                '  - "$1"',
            ],
        )
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            # Prompt is now fed via stdin, not as CLI arg
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo help")]

        assert captured_processes[0].stdin.written == b"ooo help"
        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert (
            mock_warning.call_args.kwargs["error"]
            == "mcp_args must be a mapping with string keys and YAML-safe values"
        )
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_logs_legacy_frontmatter_missing_event_name(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing MCP metadata preserves the legacy Codex structured log event."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                "mcp_tool: ouroboros_help",
            ],
        )
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo help")]

        assert captured_processes[0].stdin.written == b"ooo help"
        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_missing"
        )
        assert (
            mock_warning.call_args.kwargs["error"] == "missing required frontmatter key: mcp_args"
        )
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_router_returns_not_handled(
        self,
        tmp_path: Path,
    ) -> None:
        """Router NotHandled outcomes preserve normal Codex pass-through behavior."""
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo missing seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo missing seed.yaml"
        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_not_called()
        mock_info.assert_called_once()
        assert mock_info.call_args.args[0] == "codex_cli_runtime.task_started"
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_valid_intercepts(self, tmp_path: Path) -> None:
        """Exact prefixes with valid frontmatter dispatch before Codex CLI."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: ouroboros-run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert isinstance(intercept_request, Resolved)
        assert intercept_request.skill_name == "run"
        assert intercept_request.mcp_tool == "ouroboros_execute_seed"
        assert intercept_request.first_argument == "seed.yaml"
        assert intercept_request.mcp_args == {"seed_path": "seed.yaml"}
        mock_exec.assert_not_called()
        mock_warning.assert_not_called()
        mock_info.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_slash_prefix_intercepts(
        self, tmp_path: Path
    ) -> None:
        """Host-facing slash prefixes remain routed through the shared router."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: ouroboros-run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task("/ouroboros:ouroboros-run seed.yaml")
            ]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert isinstance(intercept_request, Resolved)
        assert intercept_request.skill_name == "run"
        assert intercept_request.command_prefix == "/ouroboros:ouroboros-run"
        assert intercept_request.first_argument == "seed.yaml"
        assert intercept_request.mcp_args == {"seed_path": "seed.yaml"}
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_uses_builtin_dispatcher_for_run_intercepts(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo run` dispatches to the local execute-seed MCP handler by default."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Seed Execution SUCCESS"),),
                    meta={
                        "session_id": "sess-123",
                        "execution_id": "exec-456",
                    },
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch.object(
                runtime, "_get_mcp_tool_handler", return_value=fake_handler
            ) as mock_lookup,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_lookup.assert_called_once_with("ouroboros_execute_seed")
        fake_handler.handle.assert_awaited_once_with({"seed_path": "seed.yaml"})
        mock_exec.assert_not_called()
        assert messages[0].tool_name == "ouroboros_execute_seed"
        assert messages[0].data["tool_input"] == {"seed_path": "seed.yaml"}
        assert messages[1].type == "result"
        assert messages[1].content == "Seed Execution SUCCESS"
        assert messages[1].data["subtype"] == "success"
        assert messages[1].data["session_id"] == "sess-123"
        assert messages[1].data["execution_id"] == "exec-456"

    @pytest.mark.asyncio
    async def test_execute_task_falls_back_when_builtin_dispatcher_returns_recoverable_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Recoverable local MCP errors fall back to normal Codex execution."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.err(
                MCPToolError(
                    "Seed tool unavailable",
                    tool_name="ouroboros_execute_seed",
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch.object(runtime, "_get_mcp_tool_handler", return_value=fake_handler),
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
        fake_handler.handle.assert_awaited_once_with({"seed_path": "seed.yaml"})
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["error_type"] == "MCPToolError"
        assert mock_warning.call_args.kwargs["error"] == "Seed tool unavailable"
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_on_recoverable_dispatch_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Recoverable MCP dispatch errors should fall through to the Codex CLI."""
        skill_md = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(
                    type="result",
                    content="Tool call timed out",
                    data={
                        "subtype": "error",
                        "recoverable": True,
                        "error_type": "MCPTimeoutError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback after timeout", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
        assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
        assert mock_warning.call_args.kwargs["path"] == str(skill_md)
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert mock_warning.call_args.kwargs["error_type"] == "MCPTimeoutError"
        assert mock_warning.call_args.kwargs["error"] == "Tool call timed out"
        assert messages[-1].content == "Codex fallback after timeout"

    @pytest.mark.asyncio
    async def test_execute_task_terminates_child_process_when_cancelled(self) -> None:
        """Cancelling task consumption should terminate the spawned Codex process."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        process = _TerminableProcess()

        async def _consume() -> list[AgentMessage]:
            return [message async for message in runtime.execute_task("Do the work")]

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            consumer = asyncio.create_task(_consume())
            await asyncio.sleep(0)
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer

        assert process.terminated or process.killed

    @pytest.mark.asyncio
    async def test_execute_task_times_out_when_codex_never_emits_output(self) -> None:
        """Silent Codex startups should fail fast instead of hanging forever."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime._startup_output_timeout_seconds = 0.01
        runtime._stdout_idle_timeout_seconds = 0.01
        process = _TimeoutTerminableProcess()

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "TimeoutError"
        assert process.terminated or process.killed

    @pytest.mark.asyncio
    async def test_execute_task_dispatches_interview_with_initial_context(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo interview` resolves templates before dispatching to the tool handler."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Starting interview"),
                AgentMessage(
                    type="result", content="Interview started", data={"subtype": "success"}
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task('ooo interview "Build a REST API"')
            ]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request.mcp_tool == "ouroboros_interview"
        assert intercept_request.first_argument == "Build a REST API"
        assert intercept_request.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": _EXPECTED_PROJECT_CWD,
        }
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Starting interview",
            "Interview started",
        ]

    @pytest.mark.asyncio
    async def test_execute_task_passes_runtime_handle_into_interview_dispatcher(
        self,
        tmp_path: Path,
    ) -> None:
        """Interview intercepts forward the current runtime handle for session reuse."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Continuing interview"),
                AgentMessage(type="result", content="Next question", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task(
                    'ooo interview "Use PostgreSQL"',
                    resume_handle=resume_handle,
                )
            ]

        dispatcher.assert_awaited_once()
        assert dispatcher.await_args.args[1] == resume_handle
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Continuing interview",
            "Next question",
        ]

    @pytest.mark.asyncio
    async def test_execute_task_local_interview_dispatch_preserves_resume_handle(
        self,
        tmp_path: Path,
    ) -> None:
        """Local interview dispatch reuses the native runtime handle and interview session."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
            ],
        )
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )

        class _FakeInterviewHandler:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            async def handle(
                self, arguments: dict[str, str]
            ) -> Result[MCPToolResult, MCPToolError]:
                self.calls.append(arguments)
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="Next question"),),
                        is_error=False,
                        meta={"session_id": "interview-456"},
                    )
                )

        handler = _FakeInterviewHandler()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )
        runtime._builtin_mcp_handlers = {"ouroboros_interview": handler}
        runtime._builtin_mcp_handler_registry_fingerprint = (
            runtime._fingerprint_builtin_mcp_handler_registry()
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task(
                    'ooo interview "Use PostgreSQL"',
                    resume_handle=resume_handle,
                )
            ]

        mock_exec.assert_not_called()
        # Resume must drop initial_context so InterviewHandler branches on
        # session_id instead of restarting a new interview.
        assert len(handler.calls) == 1
        call_args = handler.calls[0]
        assert call_args["session_id"] == "interview-123"
        assert call_args["answer"] == "Use PostgreSQL"
        assert "initial_context" not in call_args
        assert messages[0].resume_handle is not None
        assert messages[0].resume_handle.native_session_id == "thread-123"
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"
        assert (
            messages[-1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-456"
        )
        assert messages[-1].content == "Next question"

    @pytest.mark.asyncio
    async def test_execute_task_preserves_nonrecoverable_dispatch_errors(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-recoverable intercepted errors should be returned directly."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(
                    type="result",
                    content="Seed validation failed",
                    data={"subtype": "error", "error_type": "MCPToolError"},
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Dispatching",
            "Seed validation failed",
        ]
        assert messages[-1].is_error is True

    @pytest.mark.asyncio
    async def test_execute_task_logs_dispatch_failure_context_and_falls_back(
        self,
        tmp_path: Path,
    ) -> None:
        """Intercept dispatcher failures warn with context and fall through to Codex."""
        skill_md = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  mode: "fast"',
            ],
        )
        dispatcher = AsyncMock(side_effect=RuntimeError("tool unavailable"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
        assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
        assert mock_warning.call_args.kwargs["path"] == str(skill_md)
        assert mock_warning.call_args.kwargs["first_argument"] == "seed.yaml"
        assert mock_warning.call_args.kwargs["prompt_preview"] == "ooo run seed.yaml"
        assert mock_warning.call_args.kwargs["mcp_arg_keys"] == ("mode", "seed_path")
        assert mock_warning.call_args.kwargs["mcp_args_preview"] == {
            "seed_path": "seed.yaml",
            "mode": "fast",
        }
        assert mock_warning.call_args.kwargs["fallback"] == "pass_through_to_codex"
        assert mock_warning.call_args.kwargs["error_type"] == "RuntimeError"
        assert mock_warning.call_args.kwargs["error"] == "tool unavailable"
        assert mock_warning.call_args.kwargs["exc_info"] is True
        mock_info.assert_called_once()
        assert mock_info.call_args.args[0] == "codex_cli_runtime.task_started"
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_auto_dispatch_failure_does_not_fall_back(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo auto` must fail closed when the MCP dispatch tool is unavailable."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(side_effect=LookupError("No local handler registered"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        mock_exec.assert_not_called()
        assert len(messages) == 1
        assert messages[0].is_error is True
        assert messages[0].content.startswith("Cannot run ooo auto")
        assert "`ouroboros_start_auto` is unavailable" in messages[0].content
        assert "ouroboros mcp doctor" in messages[0].content
        assert mock_warning.call_args.kwargs["fallback"] == "terminal_error"
        assert messages[0].data == {
            "subtype": "error",
            "error_type": "SkillDispatchUnavailable",
            "skill_name": "auto",
            "tool_name": "ouroboros_start_auto",
            "command_prefix": "ooo auto",
            "dispatch_error_type": "LookupError",
            "dispatch_error": "No local handler registered",
            "dispatch_error_category": "local_handler_missing",
        }

    @pytest.mark.asyncio
    async def test_execute_task_auto_connection_error_preserves_real_cause(
        self,
        tmp_path: Path,
    ) -> None:
        """Auto transport failures must fail closed without being rewritten as setup issues."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
                AgentMessage(
                    type="result",
                    content="Auto MCP server unavailable",
                    data={
                        "subtype": "error",
                        "recoverable": True,
                        "error_type": "MCPConnectionError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert mock_warning.call_args.kwargs["fallback"] == "terminal_error"
        assert mock_warning.call_args.kwargs["error_type"] == "MCPConnectionError"
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Calling tool: ouroboros_start_auto",
            "Auto MCP server unavailable",
        ]
        assert messages[-1].data["error_type"] == "MCPConnectionError"
        assert messages[-1].data["error_type"] != "SkillDispatchUnavailable"

    @pytest.mark.asyncio
    async def test_execute_task_auto_resource_not_found_dispatch_error_does_not_fall_back(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing production MCP tool registrations hard-fail as dispatch unavailable."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
                AgentMessage(
                    type="result",
                    content="Tool ouroboros_start_auto not found",
                    data={
                        "subtype": "error",
                        "recoverable": True,
                        "error_type": "MCPResourceNotFoundError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        assert mock_warning.call_args.kwargs["fallback"] == "terminal_error"
        mock_exec.assert_not_called()
        assert len(messages) == 1
        assert messages[0].data["error_type"] == "SkillDispatchUnavailable"
        assert messages[0].data["dispatch_error_type"] == "MCPResourceNotFoundError"
        assert messages[0].data["dispatch_error"] == "Tool ouroboros_start_auto not found"
        assert messages[0].data["dispatch_error_category"] == "mcp_registration_missing"

    @pytest.mark.asyncio
    async def test_execute_task_auto_transport_closed_reports_transport_not_setup(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex App MCP transport closures should not be misreported as setup drift."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
                AgentMessage(
                    type="result",
                    content="MCPClientError: Transport closed",
                    data={
                        "subtype": "error",
                        "error_type": "MCPClientError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        assert mock_warning.call_args.kwargs["fallback"] == "terminal_error"
        mock_exec.assert_not_called()
        assert len(messages) == 1
        assert messages[0].data["error_type"] == "SkillDispatchUnavailable"
        assert messages[0].data["dispatch_error_type"] == "MCPClientError"
        assert messages[0].data["dispatch_error"] == "MCPClientError: Transport closed"
        assert messages[0].data["dispatch_error_category"] == "mcp_transport_closed"
        assert "MCP transport closed" in messages[0].content
        assert "not proof that the tool is unregistered" in messages[0].content
        assert "setup to register" not in messages[0].content

    @pytest.mark.asyncio
    async def test_execute_task_auto_recoverable_pipeline_error_preserves_real_cause(
        self,
        tmp_path: Path,
    ) -> None:
        """Auto pipeline failures must not be rewritten as dispatch-unavailable errors."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
                AgentMessage(
                    type="result",
                    content="Auto pipeline failed: model provider crashed",
                    data={
                        "subtype": "error",
                        "recoverable": True,
                        "error_type": "MCPToolError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert mock_warning.call_args.kwargs["fallback"] == "terminal_error"
        assert mock_warning.call_args.kwargs["error_type"] == "MCPToolError"
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Calling tool: ouroboros_start_auto",
            "Auto pipeline failed: model provider crashed",
        ]
        assert messages[-1].data["error_type"] == "MCPToolError"
        assert messages[-1].data["error_type"] != "SkillDispatchUnavailable"

    @pytest.mark.asyncio
    async def test_execute_task_auto_key_error_falls_back_with_real_cause(
        self,
        tmp_path: Path,
    ) -> None:
        """LookupError subclasses such as KeyError must not be treated as missing tools."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(side_effect=KeyError("internal_state"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs["error_type"] == "KeyError"
        assert mock_warning.call_args.kwargs["fallback"] == "pass_through_to_codex"
        mock_exec.assert_called_once()
        assert messages[-1].content == "Codex fallback"
        assert all(
            message.data.get("error_type") != "SkillDispatchUnavailable" for message in messages
        )

    @pytest.mark.asyncio
    async def test_execute_task_auto_unexpected_dispatch_error_falls_back_with_real_cause(
        self,
        tmp_path: Path,
    ) -> None:
        """Unexpected auto dispatch errors must not be misreported as missing MCP tools."""
        self._write_skill(
            tmp_path,
            "auto",
            [
                "name: auto",
                'description: "Automatically converge from goal to A-grade Seed and execute it"',
                "mcp_tool: ouroboros_start_auto",
                "mcp_args:",
                '  goal: "$goal"',
                '  cwd: "$CWD"',
            ],
        )
        dispatcher = AsyncMock(side_effect=RuntimeError("handler crashed"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo auto Build a CLI")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args.kwargs["error_type"] == "RuntimeError"
        assert mock_warning.call_args.kwargs["error"] == "handler crashed"
        mock_exec.assert_called_once()
        assert messages[-1].content == "Codex fallback"
        assert all(
            message.data.get("error_type") != "SkillDispatchUnavailable" for message in messages
        )

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_interview_intercept_dispatcher_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """Dispatcher failures log a warning and pass `ooo interview` through to Codex."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
            ],
        )
        dispatcher = AsyncMock(side_effect=RuntimeError("Interview session unavailable"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task('ooo interview "Build a REST API"')
            ]

        assert captured_processes[0].stdin.written == b'ooo interview "Build a REST API"'
        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request.skill_name == "interview"
        assert intercept_request.mcp_tool == "ouroboros_interview"
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "interview"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_interview"
        assert mock_warning.call_args.kwargs["error"] == "Interview session unavailable"
        assert messages[-1].content == "Codex fallback"

    def test_llm_backend_propagated_to_builtin_handlers(self) -> None:
        """llm_backend param is used in _get_builtin_mcp_handlers, not hardcoded."""
        runtime = CodexCliRuntime(cli_path="codex", llm_backend="litellm")
        assert runtime._llm_backend == "litellm"

    @pytest.mark.asyncio
    async def test_execute_task_missing_executable_fails_before_launch(self) -> None:
        """A missing executable fails closed before subprocess launch."""
        runtime = CodexCliRuntime(cli_path="/nonexistent/codex", cwd="/tmp/project")

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("/nonexistent/codex"),
        ):
            messages = [message async for message in runtime.execute_task("hello")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        # The unrepairable attestation is observed (not fatal); the launch
        # itself then reports the real cause.
        assert runtime._drift.epoch == 1
        assert "Codex CLI not found" in messages[0].content


# ---------------------------------------------------------------------------
# Drift is observed, not fatal: the run continues; existing threads retire.
# ---------------------------------------------------------------------------


def test_drift_retires_resume_handles_created_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread created under the old inputs is never resumed after drift."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    handle = runtime._build_runtime_handle("thread-old")
    assert handle is not None
    assert handle.metadata["ouroboros_runtime_drift_epoch"] == 0
    assert runtime._resolve_resume_session_id(handle) == "thread-old"

    config_path.write_text('model = "gpt-other"\n', encoding="utf-8")
    command = runtime._build_command(
        "/tmp/last-message", resume_session_id="thread-old", runtime_handle=handle
    )

    assert runtime._drift.epoch == 1
    assert "resume" not in command
    assert runtime._resolve_resume_session_id(handle) is None
    # A thread started after the drift is stamped with the new epoch and resumes.
    fresh = runtime._build_runtime_handle("thread-new", handle)
    assert fresh is not None
    assert fresh.metadata["ouroboros_runtime_drift_epoch"] == 1
    assert runtime._resolve_resume_session_id(fresh) == "thread-new"


def test_drift_is_reported_to_telemetry_with_closed_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str | None] = []
    monkeypatch.setattr(
        "ouroboros.orchestrator.runtime_drift.usage_telemetry.capture_runtime_drift",
        lambda kind: captured.append(kind),
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

    config_path.write_text('model = "gpt-other"\n', encoding="utf-8")
    runtime._reconcile_codex_config_files()
    runtime._reconcile_codex_config_files()
    runtime._drift.observe("not-a-real-kind", "spoofed")

    assert captured == ["codex_config", "unknown"]


def test_unloadable_config_mid_run_keeps_frozen_routing_instead_of_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config that stops loading is observed, not adopted as new routing.

    The resolver's role path degrades to the unprofiled default when
    ``load_config`` raises; re-baselining on that would silently drop the
    selected model/profile from every later command. Keep the frozen route.
    """
    from ouroboros.config.loader import ConfigError

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "ouroboros-standard"}}},
        },
        llm_role_profiles={
            "agent_runtime": "standard",
            "agent_runtime_implementation": "standard",
            "agent_runtime_qa": "standard",
        },
    )
    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        role_command = runtime._build_command("/tmp/out.txt", runtime_handle=handle)
        default_command = runtime._build_command("/tmp/out.txt")
    assert role_command[role_command.index("--profile") + 1] == "ouroboros-standard"
    assert default_command[default_command.index("--profile") + 1] == "ouroboros-standard"

    captured: list[str | None] = []
    monkeypatch.setattr(
        "ouroboros.orchestrator.runtime_drift.usage_telemetry.capture_runtime_drift",
        lambda kind: captured.append(kind),
    )
    with patch(
        "ouroboros.providers.profiles.load_config",
        side_effect=ConfigError("config.yaml is not valid YAML"),
    ):
        role_command = runtime._build_command("/tmp/out.txt", runtime_handle=handle)
        role_again = runtime._build_command("/tmp/out.txt", runtime_handle=handle)
        default_command = runtime._build_command("/tmp/out.txt")
        # A selector first used *during* the unloadable interval has no warmed
        # routing to serve; it gets the frozen role default, not the
        # resolver's unprofiled fallback.
        cold_handle = RuntimeHandle(
            backend="codex_cli", kind="qa_session", metadata={"session_role": "qa"}
        )
        cold_command = runtime._build_command("/tmp/out.txt", runtime_handle=cold_handle)

    # Observed once per selector as an unavailable baseline, never as adopted routing.
    assert captured == ["baseline_unavailable", "baseline_unavailable"]
    assert role_command[role_command.index("--profile") + 1] == "ouroboros-standard"
    assert role_again == role_command
    assert default_command[default_command.index("--profile") + 1] == "ouroboros-standard"
    assert cold_command[cold_command.index("--profile") + 1] == "ouroboros-standard"
    assert runtime._resolved_fallback_profile == "ouroboros-standard"

    # Once the config loads again, valid routing is adopted as before.
    drifted = OuroborosConfig(
        llm_profiles={
            "frontier": {"providers": {"codex": {"profile": "drifted-frontier"}}},
        },
        llm_role_profiles={
            "agent_runtime": "frontier",
            "agent_runtime_implementation": "frontier",
        },
    )
    with patch("ouroboros.providers.profiles.load_config", return_value=drifted):
        role_command = runtime._build_command("/tmp/out.txt", runtime_handle=handle)
    # (The remap also changes which native Codex profile is in force, so a
    # ``codex_config`` observation may follow; the unavailable state itself
    # is not reported again.)
    assert captured[:3] == ["baseline_unavailable", "baseline_unavailable", "profile_routing"]
    assert captured.count("baseline_unavailable") == 2
    assert role_command[role_command.index("--profile") + 1] == "drifted-frontier"


def test_routing_drift_observed_by_the_last_reconcile_still_retires_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume decision is sealed after *every* reconcile in one command build.

    Reviewer probe: routing is unchanged at the first check and changed by the
    time ``_resolve_runtime_codex_config`` re-checks it. The command must carry
    neither the old thread nor a mix of generations.
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config_a = OuroborosConfig(
        llm_profiles={"a": {"providers": {"codex": {"profile": "profile-a"}}}},
        llm_role_profiles={"agent_runtime_implementation": "a"},
    )
    config_b = OuroborosConfig(
        llm_profiles={"b": {"providers": {"codex": {"profile": "profile-b"}}}},
        llm_role_profiles={"agent_runtime_implementation": "b"},
    )
    live = {"config": config_a}
    with patch("ouroboros.providers.profiles.load_config", side_effect=lambda: live["config"]):
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        handle = runtime._build_runtime_handle(
            "thread-old",
            RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                metadata={"session_role": "implementation"},
            ),
        )
        assert handle is not None
        first = runtime._build_command(
            "/tmp/out.txt", resume_session_id="thread-old", runtime_handle=handle
        )
        assert first[first.index("--profile") + 1] == "profile-a"
        assert "thread-old" in first

        # Flip routing between the first profile check and the routing
        # resolution inside the same ``_build_command`` call.
        original = runtime._reconcile_codex_config_files

        def flip_then_reconcile(runtime_handle: RuntimeHandle | None = None) -> None:
            live["config"] = config_b
            original(runtime_handle)

        monkeypatch.setattr(runtime, "_reconcile_codex_config_files", flip_then_reconcile)
        command = runtime._build_command(
            "/tmp/out.txt", resume_session_id="thread-old", runtime_handle=handle
        )

    assert runtime._drift.epoch >= 1
    assert command[command.index("--profile") + 1] == "profile-b"
    assert "resume" not in command
    assert "thread-old" not in command


def test_handles_are_stamped_with_the_invocation_admitted_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread launched at epoch 0 stays epoch 0 even if drift is observed mid-stream.

    Reviewer probe: invocation A builds its command at epoch 0, a concurrent
    invocation B observes drift, then A's ``thread.started`` arrives. A's
    thread was created under the old inputs and must not resume.
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    scope_a = codex_cli_runtime_module._CodexItemCorrelationScope()
    assert runtime._build_command("/tmp/out.txt")
    scope_a.admitted_drift_epoch = runtime._drift.epoch  # what the stream loop records
    assert scope_a.admitted_drift_epoch == 0

    runtime._drift.observe("codex_config", "observed by a concurrent invocation")
    assert runtime._drift.epoch == 1

    started = runtime._convert_event(
        {"type": "thread.started", "thread_id": "thread-a"}, None, item_scope=scope_a
    )
    handle_a = started[0].resume_handle
    assert handle_a is not None
    assert handle_a.metadata["ouroboros_runtime_drift_epoch"] == 0
    assert runtime._resolve_resume_session_id(handle_a) is None

    # An invocation admitted after the drift is attributed to the new epoch.
    scope_b = codex_cli_runtime_module._CodexItemCorrelationScope()
    scope_b.admitted_drift_epoch = runtime._drift.epoch
    handle_b = runtime._convert_event(
        {"type": "thread.started", "thread_id": "thread-b"}, None, item_scope=scope_b
    )[0].resume_handle
    assert handle_b is not None
    assert handle_b.metadata["ouroboros_runtime_drift_epoch"] == 1
    assert runtime._resolve_resume_session_id(handle_b) == "thread-b"


def test_unchanged_unavailable_attestation_is_reported_once_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that keeps timing out does not advance the epoch on every command."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd="/tmp/project", model="gpt-5")
    assert runtime._build_command("/tmp/out.txt")
    assert runtime._drift.epoch == 0

    captured: list[str | None] = []
    monkeypatch.setattr(
        "ouroboros.orchestrator.runtime_drift.usage_telemetry.capture_runtime_drift",
        lambda kind: captured.append(kind),
    )
    timed_out = codex_cli_runtime_module._CliExecutableVersionAttestation(
        state=codex_cli_runtime_module._CliExecutableVersionState.TIMED_OUT
    )
    with patch.object(runtime, "_cli_executable_version_attestation", return_value=timed_out):
        for _ in range(3):
            assert runtime._build_command("/tmp/out.txt")

    # Unavailable evidence, not a changed binary: one observation, then idempotent.
    assert captured == ["baseline_unavailable"]
    assert runtime._drift.epoch == 1

    # The probe is retried on every command, so once it succeeds again the
    # runtime re-attests and stops carrying the unavailable state.
    assert runtime._build_command("/tmp/out.txt")
    assert runtime._build_command("/tmp/out.txt")
    assert runtime._cli_attestation_unavailable_detail is None
    assert runtime._drift.epoch == 1
    assert captured == ["baseline_unavailable"]


@pytest.mark.asyncio
async def test_resume_retired_inside_the_build_is_not_reported_as_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An early failure after in-build retirement is not a resume-bootstrap failure.

    ``attempted_resume_session_id`` is resolved before the command build; if
    the build retires it, the launch never resumed anything, so the result
    must not carry ``resume_retry`` recovery (the runner would pause and tell
    the user to retry the same --resume session).
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-test"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="/nonexistent/codex", cwd="/tmp/project")
    handle = runtime._build_runtime_handle("thread-old")
    assert handle is not None

    config_path.write_text('model = "gpt-other"\n', encoding="utf-8")
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("/nonexistent/codex"),
    ):
        messages = [
            message async for message in runtime.execute_task("hello", resume_handle=handle)
        ]

    # Config drift (plus the unresolvable executable's attestation) observed;
    # the retired resume is not reported as attempted.
    assert runtime._drift.epoch >= 1
    assert messages[-1].type == "result"
    assert messages[-1].is_error
    assert messages[-1].data.get("recovery") is None


def test_bare_resume_session_id_is_retired_once_any_drift_was_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handle manufactured from a bare session id has unknown admission history.

    Adversarial probe: after a drift, ``execute_task(resume_session_id=...)``
    used to stamp the manufactured handle with the *current* epoch and resume
    the pre-drift thread. It is stamped unknown and retired instead; with no
    drift observed yet it still resumes as before.
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
    from ouroboros.orchestrator.runtime_drift import DRIFT_EPOCH_UNKNOWN

    fresh = runtime._build_runtime_handle("thread-old", drift_epoch=DRIFT_EPOCH_UNKNOWN)
    assert fresh is not None
    assert runtime._resolve_resume_session_id(fresh) == "thread-old"

    runtime._drift.observe("codex_config", "drift after thread-old was created")
    stale = runtime._build_runtime_handle("thread-old", drift_epoch=DRIFT_EPOCH_UNKNOWN)
    assert stale is not None
    assert runtime._resolve_resume_session_id(stale) is None
    command = runtime._build_command(
        "/tmp/out.txt", resume_session_id="thread-old", runtime_handle=stale
    )
    assert "resume" not in command

    # Through the real entry point: the manufactured handle is stamped unknown
    # and the build receives no resume target.
    seen: dict[str, Any] = {}

    def capture(**kwargs: Any) -> list[str]:
        seen.update(kwargs)
        raise RuntimeError("stop before launch")

    monkeypatch.setattr(runtime, "_build_command", capture)

    async def drive() -> None:
        async for _ in runtime.execute_task("hello", resume_session_id="thread-old"):
            pass

    asyncio.run(drive())
    assert seen["resume_session_id"] is None
    assert seen["runtime_handle"].metadata["ouroboros_runtime_drift_epoch"] == DRIFT_EPOCH_UNKNOWN


def test_bool_epoch_stamp_is_not_mistaken_for_the_current_epoch() -> None:
    from ouroboros.orchestrator.runtime_drift import RuntimeDriftLedger

    ledger = RuntimeDriftLedger(runtime_backend="codex")
    ledger.observe("codex_config", "x")
    assert ledger.epoch == 1
    handle = RuntimeHandle(backend="codex_cli", metadata={"ouroboros_runtime_drift_epoch": True})
    assert ledger.handle_predates_drift(handle) is True


def test_cli_upgrade_mid_run_is_observed_and_new_binary_becomes_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    runtime = CodexCliRuntime(cli_path=cli_path, cwd="/tmp/project", model="gpt-5")
    assert runtime._build_command("/tmp/last-message")
    original_content = runtime._cli_executable_content_identity_snapshot

    cli_path.write_text("#!/bin/sh\necho codex 2.0\n", encoding="utf-8")
    cli_path.chmod(0o755)

    assert runtime._build_command("/tmp/last-message")
    assert runtime._drift.epoch == 1
    # Re-baselined on the upgraded binary: the next command sees no drift.
    assert runtime._build_command("/tmp/last-message")
    assert runtime._drift.epoch == 1
    assert runtime._cli_executable_content_identity_snapshot != original_content
    assert runtime._cli_executable_content_identity_snapshot == (
        runtime._cli_executable_content_identity()
    )
