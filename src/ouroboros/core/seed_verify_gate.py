"""Deterministic gate over acceptance criteria that nothing can verify.

An AC carrying a ``verify_command`` is judged by the orchestrator running that
command itself (``_run_ac_verify_gate``): immune both to a worker grading its
own homework and to a lost transcript. An AC without one falls back to matching
the worker's typed evidence against its runtime transcript — the only path that
can be gamed, and the only path that collapses when transcript collection
fails.

So the gate pushes criteria toward ``verify_command`` and asks for an explicit
per-AC reason when that is genuinely not feasible. It is deliberately staged:
``warn`` surfaces violations without changing behavior, ``block`` refuses the
run. Nothing here inspects or rewrites the command — it only asks whether a
deterministic judgment exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

_MAX_RENDERED_DESCRIPTION_CHARS = 80


@dataclass(frozen=True, slots=True)
class UnverifiableCriterion:
    """One AC without a deterministic verify command or exemption."""

    ac_index: int
    description: str

    @property
    def display_index(self) -> int:
        """1-based position as operators see it in reports."""
        return self.ac_index + 1


def unverifiable_criteria(seed: Seed) -> tuple[UnverifiableCriterion, ...]:
    """Return criteria that declare neither a command nor an exemption reason."""
    findings: list[UnverifiableCriterion] = []
    for index, criterion in enumerate(seed.acceptance_criteria):
        if isinstance(criterion, AcceptanceCriterionSpec):
            if criterion.verify_command or criterion.verify_exemption_reason:
                continue
            description = criterion.description
        else:
            description = str(criterion).strip()
        findings.append(UnverifiableCriterion(ac_index=index, description=description))
    return tuple(findings)


def render_verify_command_gate_warning(
    findings: tuple[UnverifiableCriterion, ...],
) -> str:
    """Render the operator-facing warning for a warn-stage gate result."""
    if not findings:
        return ""

    def render(finding: UnverifiableCriterion) -> str:
        description = finding.description
        if len(description) > _MAX_RENDERED_DESCRIPTION_CHARS:
            description = description[: _MAX_RENDERED_DESCRIPTION_CHARS - 1] + "…"
        return f"  - AC {finding.display_index}: {description}"

    lines = [
        f"{len(findings)} acceptance criteria declare no verify_command, so machine "
        "verification may be unavailable:",
        *(render(finding) for finding in findings),
        "Add a verify_command, or a verify_exemption_reason saying why one is not feasible.",
    ]
    return "\n".join(lines)


def verify_command_gate_mode() -> str:
    """Return the configured gate mode, defaulting to the non-blocking stage.

    A missing or unreadable config must not turn into a hard run refusal, so
    resolution failures fall back to ``warn``.
    """
    try:
        from ouroboros.config.loader import load_config

        return load_config().seed.verify_command_gate
    except Exception:
        return "warn"
