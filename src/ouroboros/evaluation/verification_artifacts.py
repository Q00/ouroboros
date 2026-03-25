"""Build post-run QA verification artifacts from canonical mechanical checks.

This module provides a runtime-neutral evidence path for post-execution QA.
Instead of relying on an agent-authored final summary, it runs the repository's
configured mechanical checks, persists raw outputs, and renders a compact
summary plus a detailed reference string for the QA judge.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from ouroboros.core.text import truncate_head_tail
from ouroboros.evaluation.languages import build_mechanical_config
from ouroboros.evaluation.mechanical import MechanicalConfig, run_command
from ouroboros.evaluation.models import CheckType

_ARTIFACT_BASE_DIR = Path.home() / ".ouroboros" / "artifacts"
_OUTPUT_EXCERPT_HEAD = 500
_OUTPUT_EXCERPT_TAIL = 2000
_OUTCOME_LINE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class VerificationRunArtifact:
    """Persisted evidence for a single mechanical verification run."""

    check_type: str
    command: tuple[str, ...]
    exit_code: int
    passed: bool
    timed_out: bool
    stdout_path: str
    stderr_path: str
    stdout_excerpt: str
    stderr_excerpt: str
    final_outcome: str
    is_canonical: bool = True
    is_integrated: bool = False


@dataclass(frozen=True, slots=True)
class VerificationArtifacts:
    """Rendered QA artifact plus persisted raw evidence references."""

    artifact: str
    reference: str
    artifact_dir: str
    manifest_path: str
    changed_files: tuple[str, ...] = ()
    runs: tuple[VerificationRunArtifact, ...] = ()


def _configured_commands(config: MechanicalConfig) -> list[tuple[CheckType, tuple[str, ...]]]:
    commands: list[tuple[CheckType, tuple[str, ...]]] = []
    for check_type, command in (
        (CheckType.LINT, config.lint_command),
        (CheckType.BUILD, config.build_command),
        (CheckType.TEST, config.test_command),
        (CheckType.STATIC, config.static_command),
        (CheckType.COVERAGE, config.coverage_command),
    ):
        if command:
            commands.append((check_type, command))
    return commands


def _artifact_dir_for(execution_id: str) -> Path:
    return _ARTIFACT_BASE_DIR / execution_id


def _last_nonempty_lines(text: str, *, limit: int = _OUTCOME_LINE_LIMIT) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    return "\n".join(lines[-limit:])


def _quote_command(command: tuple[str, ...]) -> str:
    return " ".join(command)


async def _capture_git_command(
    command: tuple[str, ...],
    working_dir: Path,
    destination: Path,
) -> str:
    result = await run_command(command, timeout=30, working_dir=working_dir)
    text = result.stdout or result.stderr
    text = text.rstrip("\n")
    destination.write_text(text, encoding="utf-8")
    return text


def _parse_changed_files(git_status_porcelain: str) -> tuple[str, ...]:
    seen: set[str] = set()
    changed_files: list[str] = []
    entries = git_status_porcelain.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        if len(entry) < 4:
            index += 1
            continue

        status = entry[:2]
        path = entry[3:]
        paths = [path]
        if "R" in status or "C" in status:
            new_path = entries[index + 1] if index + 1 < len(entries) else ""
            paths = [path, new_path]
            index += 1

        for candidate in paths:
            if candidate and candidate not in seen:
                seen.add(candidate)
                changed_files.append(candidate)
        index += 1

    return tuple(changed_files)


async def _capture_git_state(
    working_dir: Path, artifact_dir: Path
) -> tuple[tuple[str, ...], str, str]:
    git_status = await _capture_git_command(
        ("git", "status", "--short"),
        working_dir,
        artifact_dir / "git-status.txt",
    )
    git_status_porcelain = await _capture_git_command(
        ("git", "status", "--porcelain=v1", "-z"),
        working_dir,
        artifact_dir / "git-status-porcelain.txt",
    )
    git_diff_stat = await _capture_git_command(
        ("git", "diff", "--stat", "--find-renames"),
        working_dir,
        artifact_dir / "git-diff-stat.txt",
    )
    changed_files = _parse_changed_files(git_status_porcelain)
    return changed_files, git_status, git_diff_stat


def _render_run_summary(run: VerificationRunArtifact) -> list[str]:
    lines = [
        f"### {run.check_type.upper()} [{'PASS' if run.passed else 'FAIL'}]",
        f"Command: {_quote_command(run.command)}",
        f"Exit Code: {run.exit_code}",
        f"Integrated: {'yes' if run.is_integrated else 'no'}",
        "Final Outcome:",
        run.final_outcome,
    ]
    if run.stderr_excerpt:
        lines.extend(["Stderr Excerpt:", run.stderr_excerpt])
    return lines


def _render_compact_artifact(
    execution_id: str,
    working_dir: Path,
    execution_output: str,
    changed_files: tuple[str, ...],
    git_diff_stat: str,
    runs: tuple[VerificationRunArtifact, ...],
) -> str:
    integrated_run = next((run for run in runs if run.is_integrated), None)
    lines = [
        "# Verification Summary",
        f"Execution ID: {execution_id}",
        f"Project Dir: {working_dir}",
        f"Integrated Verification: {'present' if integrated_run else 'missing'}",
        (
            f"Canonical Test Command: {_quote_command(integrated_run.command)}"
            if integrated_run
            else "Canonical Test Command: (not detected)"
        ),
        "",
        "## Repository Changes",
    ]

    if changed_files:
        lines.extend(f"- {path}" for path in changed_files)
    else:
        lines.append("- (no changed files detected)")

    if git_diff_stat:
        lines.extend(["", "## Diff Stat", git_diff_stat])

    for run in runs:
        lines.extend(["", *_render_run_summary(run)])

    if execution_output.strip():
        lines.extend(
            [
                "",
                "## Execution Narrative",
                truncate_head_tail(
                    execution_output,
                    head=_OUTPUT_EXCERPT_HEAD,
                    tail=_OUTPUT_EXCERPT_TAIL,
                ),
            ]
        )

    return "\n".join(lines)


def _render_reference(
    execution_id: str,
    artifact_dir: Path,
    working_dir: Path,
    execution_output: str,
    changed_files: tuple[str, ...],
    git_status: str,
    git_diff_stat: str,
    runs: tuple[VerificationRunArtifact, ...],
) -> str:
    lines = [
        "# Raw Verification Evidence",
        f"Execution ID: {execution_id}",
        f"Project Dir: {working_dir}",
        f"Artifact Dir: {artifact_dir}",
        f"Manifest: {artifact_dir / 'manifest.json'}",
        "",
        "## Changed Files",
    ]

    if changed_files:
        lines.extend(f"- {path}" for path in changed_files)
    else:
        lines.append("- (none)")

    if git_status:
        lines.extend(["", "## git status --short", git_status])
    if git_diff_stat:
        lines.extend(["", "## git diff --stat --find-renames", git_diff_stat])

    for run in runs:
        lines.extend(
            [
                "",
                f"## {run.check_type.upper()}",
                f"Command: {_quote_command(run.command)}",
                f"Exit Code: {run.exit_code}",
                f"Timed Out: {run.timed_out}",
                f"Stdout Log: {run.stdout_path}",
                f"Stderr Log: {run.stderr_path}",
                "Stdout Excerpt:",
                run.stdout_excerpt or "(empty)",
                "Stderr Excerpt:",
                run.stderr_excerpt or "(empty)",
                "Final Outcome:",
                run.final_outcome,
            ]
        )

    if execution_output.strip():
        lines.extend(
            [
                "",
                "## Execution Narrative",
                truncate_head_tail(
                    execution_output,
                    head=_OUTPUT_EXCERPT_HEAD,
                    tail=_OUTPUT_EXCERPT_TAIL,
                ),
            ]
        )

    return "\n".join(lines)


async def build_verification_artifacts(
    execution_id: str,
    execution_output: str,
    working_dir: Path,
) -> VerificationArtifacts:
    """Run canonical mechanical checks and build QA evidence strings."""
    artifact_dir = _artifact_dir_for(execution_id)
    runs_dir = artifact_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    config = build_mechanical_config(working_dir)
    commands = _configured_commands(config)
    changed_files, git_status, git_diff_stat = await _capture_git_state(working_dir, artifact_dir)

    runs: list[VerificationRunArtifact] = []
    for index, (check_type, command) in enumerate(commands, start=1):
        result = await run_command(command, timeout=config.timeout_seconds, working_dir=working_dir)

        run_dir = runs_dir / f"{index:02d}-{check_type.value}"
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        combined_output = result.stdout if result.stdout.strip() else result.stderr
        runs.append(
            VerificationRunArtifact(
                check_type=check_type.value,
                command=command,
                exit_code=result.return_code,
                passed=result.return_code == 0 and not result.timed_out,
                timed_out=result.timed_out,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                stdout_excerpt=truncate_head_tail(
                    result.stdout,
                    head=_OUTPUT_EXCERPT_HEAD,
                    tail=_OUTPUT_EXCERPT_TAIL,
                ),
                stderr_excerpt=truncate_head_tail(
                    result.stderr,
                    head=_OUTPUT_EXCERPT_HEAD,
                    tail=_OUTPUT_EXCERPT_TAIL,
                ),
                final_outcome=_last_nonempty_lines(combined_output),
                is_integrated=check_type == CheckType.TEST,
            )
        )

    rendered_runs = tuple(runs)
    manifest = {
        "execution_id": execution_id,
        "working_dir": str(working_dir),
        "artifact_dir": str(artifact_dir),
        "changed_files": list(changed_files),
        "runs": [asdict(run) for run in rendered_runs],
        "has_integrated_verification": any(run.is_integrated for run in rendered_runs),
    }
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    artifact = _render_compact_artifact(
        execution_id,
        working_dir,
        execution_output,
        changed_files,
        git_diff_stat,
        rendered_runs,
    )
    reference = _render_reference(
        execution_id,
        artifact_dir,
        working_dir,
        execution_output,
        changed_files,
        git_status,
        git_diff_stat,
        rendered_runs,
    )

    return VerificationArtifacts(
        artifact=artifact,
        reference=reference,
        artifact_dir=str(artifact_dir),
        manifest_path=str(manifest_path),
        changed_files=changed_files,
        runs=rendered_runs,
    )
