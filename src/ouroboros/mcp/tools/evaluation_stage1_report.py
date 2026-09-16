"""Present shared mechanical results without rerunning or reinterpreting checks."""

from typing import Any

from ouroboros.evaluation.models import MechanicalResult

_OUTPUT_TAIL_CHARS = 500
_DETAIL_FIELDS = (
    "command",
    "executed_command",
    "working_dir",
    "return_code",
    "timed_out",
    "skipped",
)


def serialize_stage1_result(result: MechanicalResult | None) -> dict[str, Any] | None:
    """Retain known diagnostics in JSON-safe job metadata, not arbitrary output."""
    if result is None:
        return None
    checks = []
    for check in result.checks:
        details = {key: check.details[key] for key in _DETAIL_FIELDS if key in check.details}
        for key in ("stdout_tail", "stderr_tail"):
            if key in check.details:
                details[key] = str(check.details[key] or "")[-_OUTPUT_TAIL_CHARS:]
        checks.append(
            {
                "check_type": check.check_type.value,
                "passed": check.passed,
                "message": check.message,
                "details": details,
            }
        )
    return {"passed": result.passed, "coverage_score": result.coverage_score, "checks": checks}


def format_stage1_result(
    result: MechanicalResult | None, *, include_exit_status: bool = False
) -> list[str]:
    """Keep the existing single-AC text; add explicit exit/timeout for shared results."""
    if result is None:
        return []
    lines = [
        "Stage 1: Mechanical Verification",
        "-" * 40,
        f"Status: {'PASSED' if result.passed else 'FAILED'}",
        f"Coverage: {result.coverage_score:.1%}" if result.coverage_score else "Coverage: N/A",
    ]
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{status}] {check.check_type}: {check.message}")
        if not check.passed:
            details = check.details
            command = details.get("command")
            if isinstance(command, list) and command:
                lines.append(f"    command: {' '.join(str(part) for part in command)}")
            working_dir = details.get("working_dir")
            if working_dir:
                lines.append(f"    cwd: {working_dir}")
            if include_exit_status:
                executed = details.get("executed_command")
                if isinstance(executed, list) and executed and executed != command:
                    lines.append(
                        f"    executed command: {' '.join(str(part) for part in executed)}"
                    )
                if "return_code" in details:
                    lines.append(f"    exit code: {details['return_code']}")
                if "timed_out" in details:
                    lines.append(f"    timed out: {details['timed_out']}")
            for key, label in (("stdout_tail", "stdout tail"), ("stderr_tail", "stderr tail")):
                tail = str(details.get(key) or "")[-_OUTPUT_TAIL_CHARS:].strip()
                if tail:
                    lines.append(f"    {label}:")
                    lines.extend(f"      {line}" for line in tail.splitlines())
    lines.append("")
    return lines
