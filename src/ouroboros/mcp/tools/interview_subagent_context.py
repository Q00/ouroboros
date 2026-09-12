"""Prompt context rendering for delegated interview workers."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

FACTUAL_RESEARCH_SNAPSHOT = """
## Factual Research Snapshot
1. Show the interview question first.
2. On start, build code_context and source-backed web_context once.
3. Reuse scoped artifacts on later turns; emit no ordinary per-question reasoning panel.
4. Milestone lateral review and closure checks remain separate fresh gates."""


def render_adapter_section(*, action: str, adapter_question: str | None) -> str:
    if action == "start" or not adapter_question:
        return ""
    return (
        "\n## Required Reference/Glossary Adapter Turn\n"
        "Ask the following question exactly before any general Socratic question. "
        "Treat glossary/reference material as vocabulary or contrast only, never "
        "as a requirement or acceptance criterion.\n\n"
        f"{adapter_question}\n"
    )


def render_interview_subagent_context(
    *,
    action: str,
    initial_context: str,
    question_advisory: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Return server-authored factual snapshot and original-subject sections."""
    visible = {
        key: question_advisory[key]
        for key in (
            "question_advisory_request",
            "question_advisory_fanout_id",
            "question_advisory_result_correlation_key",
            "question_advisory_cached_lanes",
        )
        if question_advisory and key in question_advisory
    }
    contract = ""
    if visible:
        contract = (
            "\n## Server-authored Factual Snapshot Contract\n"
            "The parent bridge owns the stamped factual lane dispatch and result "
            "submission. Do not invent, replace, or self-attest lane output. Treat "
            "cached artifact references as evidence only after the parent fetches them.\n"
            "```json\n" + json.dumps(visible, ensure_ascii=False, sort_keys=True) + "\n```\n"
        )
    subject = (
        f"\n## Original Research Subject\n{initial_context}\n"
        if action != "start" and initial_context
        else ""
    )
    return contract, subject
