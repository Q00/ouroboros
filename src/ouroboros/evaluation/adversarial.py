"""Adversarial verification classes — concrete attack surfaces for QA probing.

A verifier that is told to "test edge cases" tests nothing in particular. The
fix is a CHECKLIST of named, concrete adversarial classes, each with a trigger
(when it applies) and a probe (what to actually try). The QA judge selects the
classes whose trigger matches the artifact and reports a finding per class —
turning verification from a vibe into an auditable contract.

This is ouroboros's adaptation of the "UltraQA" adversarial-class discipline from
oh-my-openagent / lazycodex (https://github.com/code-yeongyu/lazycodex), credited
with thanks. Their insight — independent verification probing explicit attack
surfaces — fits ouroboros's "verify, don't bluff" ethos; here it is a typed,
versioned registry the evaluator/qa-judge can render into a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

ADVERSARIAL_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class AdversarialClass:
    """One named adversarial verification class.

    Attributes:
        id: Stable machine id (snake_case) — used in structured findings.
        name: Human-readable label.
        trigger: The condition under which this class APPLIES to an artifact.
        probe: The concrete thing to try / inspect to falsify a "done" claim.
    """

    id: str
    name: str
    trigger: str
    probe: str


# The canonical registry. Order is the suggested probing order (cheap structural
# checks first, behavioural/timing checks later).
ADVERSARIAL_CLASSES: tuple[AdversarialClass, ...] = (
    AdversarialClass(
        id="malformed_input",
        name="Malformed / boundary input",
        trigger="the artifact parses or accepts new input",
        probe="feed empty, oversized, wrong-type, and boundary values; confirm it "
        "rejects cleanly (no crash, no silent wrong result)",
    ),
    AdversarialClass(
        id="prompt_injection",
        name="Prompt / instruction injection",
        trigger="the artifact incorporates untrusted external text (web, files, tool output)",
        probe="embed adversarial instructions in that text; confirm they are treated "
        "as data, not obeyed as commands",
    ),
    AdversarialClass(
        id="cancel_resume",
        name="Cancel / resume",
        trigger="the artifact drives a resumable or long-running flow",
        probe="interrupt mid-flight and resume; confirm no duplicate work, lost state, "
        "or corrupted checkpoint",
    ),
    AdversarialClass(
        id="stale_state",
        name="Stale state",
        trigger="the artifact reads generated, cached, or derived state",
        probe="run against pre-existing/outdated artifacts; confirm it regenerates or "
        "invalidates rather than trusting stale data",
    ),
    AdversarialClass(
        id="dirty_worktree",
        name="Dirty worktree",
        trigger="the artifact touches files in a working tree",
        probe="run with uncommitted user changes present; confirm it never clobbers or "
        "loses unrelated edits",
    ),
    AdversarialClass(
        id="hung_command",
        name="Hung / long command",
        trigger="the artifact shells out or calls a long external command",
        probe="simulate a command that hangs or runs long; confirm a bounded timeout "
        "and a clear failure, not an indefinite block",
    ),
    AdversarialClass(
        id="flaky_test",
        name="Flaky / timing-sensitive test",
        trigger="the artifact adds new or timing-sensitive tests",
        probe="run repeatedly; confirm deterministic results (no order/timing/network dependence)",
    ),
    AdversarialClass(
        id="misleading_output",
        name="Misleading success output",
        trigger="success is claimed from logs or exit text",
        probe="check that the claimed success matches the ACTUAL observable effect "
        "(file written, endpoint responding) — not just a hopeful log line",
    ),
    AdversarialClass(
        id="repeated_interrupt",
        name="Repeated interruption",
        trigger="the artifact performs a multi-step mutating operation",
        probe="interrupt repeatedly at different steps; confirm idempotence / safe "
        "rollback with no partial corruption",
    ),
)

_CLASS_BY_ID = {c.id: c for c in ADVERSARIAL_CLASSES}


def get_class(class_id: str) -> AdversarialClass | None:
    """Return the class with ``class_id`` or ``None``."""
    return _CLASS_BY_ID.get(class_id)


def render_checklist(classes: tuple[AdversarialClass, ...] = ADVERSARIAL_CLASSES) -> str:
    """Render the classes as a compact, prompt-ready checklist."""
    lines = [
        "Probe each adversarial class whose TRIGGER matches this artifact "
        "(skip the ones that do not apply):",
    ]
    for cls in classes:
        lines.append(f"- **{cls.id}** ({cls.name}) — if {cls.trigger}: {cls.probe}.")
    return "\n".join(lines)


_EXECUTABLE_EVIDENCE_CONTRACT = (
    'A "done" claim is only as strong as the probes it survives. Use supplied '
    "artifact/reference/history/seed evidence only; if an applicable probe requires "
    "execution or observation that is not present, report that as an evidence gap in "
    "``differences`` (with a matching ``suggestion``) instead of implying you ran it. "
    "Let any real failure or unverified applicable probe lower ``correctness``."
)

# A document (e.g. a seed specification) describes FUTURE behaviour and cannot be
# executed; treating that inability as an evidence gap would penalize every
# behavioural spec regardless of substance.
_DOCUMENT_EVIDENCE_CONTRACT = (
    "This artifact is a document describing future behaviour — it cannot be executed, "
    "and that is not a defect. Never report an unrunnable probe as an evidence gap and "
    "never lower any dimension because a probe could not be executed. Instead, use the "
    "applicable classes as a completeness lens over the document's substance: if it "
    "clearly triggers a class yet fails to address it (e.g. it shells out but never "
    "bounds the command), report that omission as a difference with a matching "
    "``suggestion``. Judge the document's substance only."
)


def render_adversarial_section(artifact_type: str = "code") -> str:
    """Render the full prompt section: header, checklist, and evidence contract.

    The evidence contract is artifact-kind-aware: executable artifacts are judged
    on whether the evidence shows they survive the probes; ``document`` artifacts
    get the completeness-lens contract instead.
    """
    contract = (
        _DOCUMENT_EVIDENCE_CONTRACT
        if artifact_type == "document"
        else _EXECUTABLE_EVIDENCE_CONTRACT
    )
    return (
        f"## Adversarial Probes (schema v{ADVERSARIAL_SCHEMA_VERSION})\n"
        f"{render_checklist()}\n{contract}\n"
    )


__all__ = [
    "ADVERSARIAL_CLASSES",
    "ADVERSARIAL_SCHEMA_VERSION",
    "AdversarialClass",
    "get_class",
    "render_adversarial_section",
    "render_checklist",
]
