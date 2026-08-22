"""Prompt fragments for structured Seed extraction."""

from __future__ import annotations

TASK_TYPE_RULE = (
    "TASK_TYPE rule: choose exactly one of code, research, analysis, artifact, "
    "document, documentation, or presentation.\n\n"
    "GOAL: <clear goal statement>\n"
    "TASK_TYPE: <task type>"
)


def project_type_template(*, is_brownfield: bool) -> str:
    """Return the project-context trailer for the extraction format."""
    if not is_brownfield:
        return "PROJECT_TYPE: greenfield"
    return (
        "PROJECT_TYPE: brownfield\n"
        'CONTEXT_REFERENCES: [{{"path": "<path>", "role": "<primary|reference>", "summary": "<summary>"}}, ...]\n'
        'EXISTING_PATTERNS: ["<pattern 1>", "<pattern 2>", ...]\n'
        'EXISTING_DEPENDENCIES: ["<dependency 1>", "<dependency 2>", ...]\n'
        "CONTEXT_REFERENCES rule: respond with one single-line JSON array of objects. "
        "Path, role, and summary values may contain any characters, including literal "
        ": colons and | pipes; never use a bare pipe as the list separator.\n"
        "EXISTING_PATTERNS rule: respond with one single-line JSON array of strings. "
        "Pattern values may contain any characters, including literal | pipes; "
        "never use a bare pipe as the list separator.\n"
        "EXISTING_DEPENDENCIES rule: respond with one single-line JSON array of strings. "
        "Dependency values may contain any characters, including literal | pipes; "
        "never use a bare pipe as the list separator."
    )
