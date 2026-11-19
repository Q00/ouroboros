"""Repair Seed preflight findings from repository evidence (auto mode).

``ooo auto`` has no human to answer a preflight question, yet some questions
are answered by the repository itself. The motivating case: a Seed whose
``verify_command`` runs ``./runtests.py`` while the checkout keeps that script
at ``tests/runtests.py``. Preflight is right to reject the command as written;
blocking the whole session on "does ``runtests.py`` exist?" is not, because
listing the checkout answers it.

This module repairs only what the filesystem decides:

* The finding must be a blocking ``verify_program_missing`` finding with a
  relative ``subject`` path. No prose field (``detail`` / ``question``) is
  read.
* Candidates come from listing the workspace (``git ls-files`` for tracked and
  unignored files, else a bounded directory walk that skips hidden
  directories). A candidate is a file whose relative path *ends with* the
  missing path's components, so ``runtests.py`` matches ``tests/runtests.py``
  and ``scripts/run.sh`` matches only ``.../scripts/run.sh``. This is path
  equality on listed files, not text classification.
* Exactly one candidate relocates the command to it. Several leave the finding
  unresolved, and the caller blocks as before.
* Zero candidates mean no file in the checkout can be the one the command
  names. When the command hands that path to node's built-in test runner
  (``node --test test/scene.test.js``), the file is a test the run itself
  writes, and the repair gives preflight's own answer: the path is added to
  ``expected_artifacts`` of every criterion whose command runs it. The worker
  is shown expected artifacts (``verify_command`` stays hidden from it), so the
  run creates the file at the path the command runs. A path the Seed claims
  already exists (``existing_dependencies``), a path outside the workspace, a
  criterion with its own ``verify_cwd``, and any program not run by
  ``node --test`` are never declared, so an invented harness or existing-code
  path still blocks.
* Every rewrite is re-checked by :func:`run_seed_preflight` and kept only when
  the Seed has strictly fewer blocking findings afterwards, so a repair can
  never trade one defect for another, and the preflight check itself is never
  relaxed. All declarations together count as one repair round.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
from uuid import uuid4

from ouroboros.auto.seed_preflight import (
    _STATIC_SHELL_WORD,
    PreflightFinding,
    SeedPreflightReport,
    _claimed_dependency_files,
    _command_program_tokens,
    _command_test_runner_operands,
    _decode_static_shell_word,
    _normalize_workspace_path,
    run_seed_preflight,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

REPAIRABLE_FINDING_CODE = "verify_program_missing"
RELOCATED_ACTION = "relocated"
DECLARED_ACTION = "declared_expected_artifact"

# Bounds on the workspace listing. A listing that hits the cap is treated as
# incomplete, and an incomplete listing cannot prove a candidate is unique.
_MAX_LISTED_FILES = 200_000
_GIT_LIST_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class PreflightRepair:
    """One Seed rewrite proven by the workspace listing.

    ``action`` is :data:`RELOCATED_ACTION` when the verify commands were pointed
    at ``evidence_path``, the one listed file ending with ``subject``. It is
    :data:`DECLARED_ACTION` when no listed file matches and ``subject`` was
    added to ``expected_artifacts``; ``evidence_path`` is then ``None`` and the
    commands, listed for the criteria that now declare it, are unchanged.
    """

    code: str
    subject: str
    evidence_path: str | None
    commands_before: tuple[str, ...]
    commands_after: tuple[str, ...]
    action: str = RELOCATED_ACTION


@dataclass(frozen=True, slots=True)
class PreflightRepairOutcome:
    """Result of :func:`repair_seed_preflight`.

    ``kept_rounds`` counts the rounds that kept a rewrite: the share of the
    repair budget this outcome used.
    """

    seed: Seed
    report: SeedPreflightReport
    repairs: tuple[PreflightRepair, ...]
    rounds: int
    kept_rounds: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.repairs)


def list_workspace_files(workspace_root: Path) -> tuple[str, ...] | None:
    """Return workspace-relative POSIX file paths, or ``None`` when incomplete.

    Git's own listing is preferred: it reports tracked and untracked files and
    honors the repository's ignore rules, so virtualenvs and build outputs are
    excluded by the project's own configuration. Outside a Git checkout a
    bounded walk is used that skips hidden directories.
    """
    listed = _git_listed_files(workspace_root)
    if listed is not None:
        return listed
    files: list[str] = []
    for current, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        relative_dir = Path(current).relative_to(workspace_root)
        for filename in filenames:
            files.append((relative_dir / filename).as_posix())
            if len(files) >= _MAX_LISTED_FILES:
                return None
    return tuple(files)


def _git_listed_files(workspace_root: Path) -> tuple[str, ...] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=workspace_root,
            capture_output=True,
            timeout=_GIT_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    entries = [
        entry
        for entry in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if entry
    ]
    if len(entries) >= _MAX_LISTED_FILES:
        return None
    return tuple(dict.fromkeys(entries))


def locate_program_candidates(
    subject: str, workspace_root: Path, listed_files: tuple[str, ...]
) -> tuple[str, ...]:
    """Return existing files whose relative path ends with ``subject``'s components."""
    missing = PurePosixPath(subject)
    if missing.is_absolute() or not missing.parts or ".." in missing.parts:
        return ()
    width = len(missing.parts)
    candidates = []
    for entry in listed_files:
        path = PurePosixPath(entry)
        if len(path.parts) <= width or path.parts[-width:] != missing.parts:
            continue
        if (workspace_root / entry).is_file():
            candidates.append(path.as_posix())
    return tuple(sorted(set(candidates)))


_SEGMENT_SEPARATORS = (";", "\n", "&", "|")


def _rewrite_command(command: str, subject: str, replacement: str) -> str:
    """Replace the verifier-program occurrences of ``subject`` with ``replacement``.

    Only a word that *becomes* its command segment's program is rewritten:
    the program set computed by preflight for the segment up to and including
    the word contains ``subject``, and the set for the segment before the word
    does not. Arguments and redirection targets that happen to spell the same
    path (``./runtests.py > runtests.py``) are left intact, so a rewrite can
    never point an output operand at the real script. A word written with a
    leading ``./`` keeps it so a direct invocation stays a direct invocation;
    a word that needs quoting is quoted.
    """
    pieces: list[str] = []
    cursor = 0
    for match in re.finditer(_STATIC_SHELL_WORD, command):
        decoded = _decode_static_shell_word(match.group(0))
        if decoded is None or _normalize_workspace_path(decoded) != subject:
            continue
        segment_start = (
            max(command.rfind(separator, 0, match.start()) for separator in _SEGMENT_SEPARATORS) + 1
        )
        before_word = command[segment_start : match.start()]
        through_word = command[segment_start : match.end()]
        if subject in _command_program_tokens(before_word):
            continue
        if subject not in _command_program_tokens(through_word):
            continue
        new_word = f"./{replacement}" if decoded.startswith("./") else replacement
        pieces.append(command[cursor : match.start()])
        pieces.append(shlex.quote(new_word))
        cursor = match.end()
    if not pieces:
        return command
    pieces.append(command[cursor:])
    return "".join(pieces)


def _seed_with_rewrite(seed: Seed, subject: str, replacement: str) -> tuple[Seed, PreflightRepair]:
    criteria = []
    before: list[str] = []
    after: list[str] = []
    for criterion in seed.acceptance_criteria:
        if isinstance(criterion, AcceptanceCriterionSpec) and criterion.verify_command:
            rewritten = _rewrite_command(criterion.verify_command, subject, replacement)
            if rewritten != criterion.verify_command:
                before.append(criterion.verify_command)
                after.append(rewritten)
                criterion = AcceptanceCriterionSpec.model_validate(
                    {**criterion.model_dump(), "verify_command": rewritten}
                )
        criteria.append(criterion)
    repaired = Seed.from_dict({**seed.to_dict(), "acceptance_criteria": tuple(criteria)})
    return repaired, PreflightRepair(
        code=REPAIRABLE_FINDING_CODE,
        subject=subject,
        evidence_path=replacement,
        commands_before=tuple(before),
        commands_after=tuple(after),
    )


def _runs_in_workspace_root(criterion: AcceptanceCriterionSpec) -> bool:
    """Preflight resolves verifier paths from the workspace root, ignoring ``verify_cwd``."""
    return criterion.verify_cwd is None or _normalize_workspace_path(criterion.verify_cwd) in {
        "",
        ".",
    }


def _seed_with_declared_test_file(seed: Seed, subject: str) -> tuple[Seed, PreflightRepair] | None:
    """Add ``subject`` to ``expected_artifacts`` of each criterion that runs it.

    "Runs it" means the criterion's command hands ``subject`` to
    ``node --test``. The caller passes only a ``subject`` no criterion declares yet (preflight
    reports none for a declared path). Returns ``None`` when no criterion runs
    it under ``node --test``, or when any criterion that does cannot take the
    artifact (its own ``verify_cwd``, or the success-contract limits); a
    partial declaration would leave a criterion that runs the file without it.
    """
    criteria = []
    commands: list[str] = []
    for criterion in seed.acceptance_criteria:
        if (
            isinstance(criterion, AcceptanceCriterionSpec)
            and criterion.verify_command
            and subject in _command_test_runner_operands(criterion.verify_command)
        ):
            if not _runs_in_workspace_root(criterion):
                return None
            commands.append(criterion.verify_command)
            try:
                # The schema rejects a path outside the workspace and a
                # contract over the execution capsule's limits.
                criterion = AcceptanceCriterionSpec.model_validate(
                    {
                        **criterion.model_dump(),
                        "expected_artifacts": (*criterion.expected_artifacts, subject),
                    }
                )
            except ValueError:
                return None
        criteria.append(criterion)
    if not commands:
        return None
    repaired = Seed.from_dict({**seed.to_dict(), "acceptance_criteria": tuple(criteria)})
    return repaired, PreflightRepair(
        code=REPAIRABLE_FINDING_CODE,
        subject=subject,
        evidence_path=None,
        commands_before=tuple(commands),
        commands_after=tuple(commands),
        action=DECLARED_ACTION,
    )


def _declare_test_files(
    seed: Seed,
    report: SeedPreflightReport,
    workspace_root: Path,
    listed_files: tuple[str, ...],
) -> tuple[Seed, SeedPreflightReport, tuple[PreflightRepair, ...]]:
    """Declare every missing ``node --test`` file no listed file can be.

    Each declaration is re-checked on its own and kept only when it leaves
    strictly fewer blocking findings, so one declaration that does not resolve
    its finding (a ``cd`` before the runner, say) is dropped without dropping
    the others.
    """
    declared: list[PreflightRepair] = []
    claimed = _claimed_dependency_files(seed)
    for finding in tuple(report.blocking_findings):
        subject = finding.subject
        if not _repairable(finding) or subject in claimed:
            continue
        if locate_program_candidates(subject, workspace_root, listed_files):
            continue
        trial = _seed_with_declared_test_file(seed, subject)
        if trial is None:
            continue
        trial_seed, repair = trial
        trial_report = run_seed_preflight(trial_seed, workspace_root=workspace_root)
        if len(trial_report.blocking_findings) >= len(report.blocking_findings):
            continue
        seed, report = trial_seed, trial_report
        declared.append(repair)
    return seed, report, tuple(declared)


def _repairable(finding: PreflightFinding) -> bool:
    return finding.blocking and finding.code == REPAIRABLE_FINDING_CODE


def repair_seed_preflight(
    seed: Seed, workspace_root: Path | None, *, max_rounds: int
) -> PreflightRepairOutcome:
    """Repair blocking preflight findings the workspace listing resolves.

    Each round tries every repairable blocking finding once and keeps the
    first relocation that helps; a round that relocates nothing declares the
    missing ``node --test`` files instead (:func:`_declare_test_files`). A
    rewrite is kept only when re-running preflight shows strictly fewer
    blocking findings. The loop stops when preflight passes, when a round
    keeps no rewrite, or after ``max_rounds`` rounds. The returned ``report``
    always describes the returned ``seed``.
    """
    report = run_seed_preflight(seed, workspace_root=workspace_root)
    repairs: list[PreflightRepair] = []
    rounds = 0
    kept_rounds = 0
    if workspace_root is None or report.passed or max_rounds <= 0:
        return PreflightRepairOutcome(seed, report, (), rounds)
    if not any(_repairable(finding) for finding in report.blocking_findings):
        return PreflightRepairOutcome(seed, report, (), rounds)
    listed_files = list_workspace_files(workspace_root)
    if listed_files is None:
        return PreflightRepairOutcome(seed, report, (), rounds)
    current = seed
    while rounds < max_rounds and not report.passed:
        rounds += 1
        kept_this_round = False
        for finding in report.blocking_findings:
            if not _repairable(finding):
                continue
            candidates = locate_program_candidates(finding.subject, workspace_root, listed_files)
            if len(candidates) != 1:
                continue
            trial, repair = _seed_with_rewrite(current, finding.subject, candidates[0])
            if not repair.commands_after:
                continue
            trial_report = run_seed_preflight(trial, workspace_root=workspace_root)
            if len(trial_report.blocking_findings) >= len(report.blocking_findings):
                continue
            current, report = trial, trial_report
            repairs.append(repair)
            kept_this_round = True
            break
        if not kept_this_round:
            current, report, declared = _declare_test_files(
                current, report, workspace_root, listed_files
            )
            repairs.extend(declared)
            kept_this_round = bool(declared)
        if not kept_this_round:
            break
        kept_rounds += 1
    if repairs:
        # A repaired Seed is a new Seed: give it its own identity and record
        # the Seed it was derived from, as the review repair loop does.
        current = Seed.from_dict(
            {
                **current.to_dict(),
                "metadata": seed.metadata.model_copy(
                    update={
                        "seed_id": f"seed_{uuid4().hex[:12]}",
                        "created_at": datetime.now(UTC),
                        "parent_seed_id": seed.metadata.seed_id,
                    }
                ).model_dump(mode="json"),
            }
        )
    return PreflightRepairOutcome(current, report, tuple(repairs), rounds, kept_rounds)
