"""Typed self-contained subagent assignment contract.

Subagent prompts have historically been free-form f-strings: each builder
concatenates ``## Your Task`` / ``## Seed Specification`` / notes ad hoc, so what
the child is *told to do* and what the harness later *validates* can silently
drift. This module introduces a small, frozen ``AssignmentMessage`` that frames
a dispatch as the four things a self-contained worker needs:

- **TASK** — what to do.
- **DELIVERABLE** — the concrete artifact / outcome that counts as "done".
- **SCOPE** — boundaries: which files, session, limits, and constraints apply.
- **VERIFY** — the evidence the child must produce before reporting completion.

Rendering wraps the contract in an explicit ``<assignment>`` authority delimiter
so the child treats it as binding direction rather than reference prose — the
same fencing rationale as the Codex ``<system-directive>`` block.

The dataclass is intentionally provider-neutral and additive: builders adopt it
where it strengthens the contract, and the ``VERIFY`` lines are the natural seam
to later generate from ``evidence_schema`` so the prompt's required-evidence and
the harness's ``validate_evidence`` gate stay in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AssignmentMessage:
    """A self-contained TASK / DELIVERABLE / SCOPE / VERIFY work order.

    Attributes:
        task: One or two sentences stating what the child must do.
        deliverable: The artifact or end-state that constitutes completion.
        scope: Ordered boundary lines (session id, limits, working dir, files,
            constraints). Empty entries are dropped.
        verify: Ordered evidence lines the child must satisfy before claiming
            done (e.g. "All acceptance criteria pass", "Tests run green").
            Empty entries are dropped.
        body: Optional trailing free-form block (e.g. the seed specification or
            a recursion guard) appended verbatim after the contract. Kept
            outside the four labelled sections so large payloads do not dilute
            the directive.
    """

    task: str
    deliverable: str
    scope: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    body: str | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("AssignmentMessage.task must not be empty")
        if not self.deliverable.strip():
            raise ValueError("AssignmentMessage.deliverable must not be empty")

    @staticmethod
    def _bullets(lines: tuple[str, ...]) -> str:
        return "\n".join(f"- {line.strip()}" for line in lines if line.strip())

    def render(self) -> str:
        """Render the assignment as an authority-delimited prompt block."""
        sections: list[str] = [
            "## Task",
            self.task.strip(),
            "",
            "## Deliverable",
            self.deliverable.strip(),
        ]

        scope_bullets = self._bullets(self.scope)
        if scope_bullets:
            sections.extend(["", "## Scope", scope_bullets])

        verify_bullets = self._bullets(self.verify)
        if verify_bullets:
            sections.extend(
                [
                    "",
                    "## Verify (produce this evidence before reporting done)",
                    verify_bullets,
                ]
            )

        block = "<assignment>\n" + "\n".join(sections) + "\n</assignment>"

        if self.body and self.body.strip():
            return f"{block}\n\n{self.body.strip()}"
        return block


__all__ = ["AssignmentMessage"]
