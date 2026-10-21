"""The checks the product executed, as the advisory semantic review sees them.

Stage 1 runs the project's command checks and carries the check package
decision the controller recorded for the criterion. Stage 2 is an advisory
judge that may withhold approval but never grants it; it reads the artifact
and source through a read-only tool envelope, so whether it can run anything
itself depends on the backend. Without this section a judge that cannot
execute has no way to see evidence the product already produced, and the
same work gets a different verdict on a different backend.

The section lists only checks that actually ran, each with its scope: a
project command check covers the whole project, a check package decision
covers this criterion only. It states facts the judge may cite; it is not a
verdict and it does not tell the judge to approve.
"""

from __future__ import annotations

from ouroboros.evaluation.models import CheckResult, CheckType, MechanicalResult

_TAIL_CHARS = 600


def _command_line(check: CheckResult) -> str:
    details = check.details
    command = details.get("command")
    shown = " ".join(str(part) for part in command) if isinstance(command, list) else "?"
    if details.get("timed_out"):
        outcome = "timed out"
    else:
        outcome = f"exit code {details.get('return_code')}"
    line = f"- {check.check_type.value}: `{shown}` ran, {outcome}, {'passed' if check.passed else 'failed'}"
    tail = str(details.get("stdout_tail") or details.get("stderr_tail") or "").strip()
    if tail:
        line += f"\n  output tail:\n  ```\n  {tail[-_TAIL_CHARS:]}\n  ```"
    return line


def _package_line(check: CheckResult) -> str:
    return f"- {'pass' if check.passed else 'fail'}: {check.message}"


def render_executed_evidence(stage1: MechanicalResult | None) -> str:
    """The prompt section naming what the product executed, or ``""`` when nothing ran."""
    if stage1 is None:
        return ""
    executed = [check for check in stage1.checks if check.executed]
    commands = [c for c in executed if c.check_type is not CheckType.CHECK_PACKAGE]
    package = [c for c in executed if c.check_type is CheckType.CHECK_PACKAGE]
    if not commands and not package:
        return ""
    lines = [
        "\n## EXECUTED EVIDENCE (run by Ouroboros, not by you)",
        "Ouroboros executed these checks itself in the evaluated project after the worker "
        "stopped. They are observed facts you may cite as evidence; they are not a verdict. "
        "Where they disagree with run-status notes inside the artifact, these observations "
        "are the more recent record.",
    ]
    if commands:
        lines.append("Project command checks (scope: the whole project, not this criterion alone):")
        lines.extend(_command_line(check) for check in commands)
    if package:
        lines.append(
            "Check package decision for this criterion (scope: this criterion only; "
            "frozen checks built before the worker ran):"
        )
        lines.extend(_package_line(check) for check in package)
    return "\n".join(lines)
