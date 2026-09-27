from __future__ import annotations

from typing import Any

from ouroboros.bigbang.ambiguity import AmbiguityScore
from ouroboros.mcp.host_context import resolve_request_subagent_dispatch
from ouroboros.mcp.tools.interview_advisory import _attach_question_assist_requests
from ouroboros.mcp.tools.subagent import FanoutRegistry


def plugin_factual_question(
    *,
    last_question: str | None,
    fallback_question: str,
    research_subject: str,
) -> str:
    candidate = str(last_question or "").strip()
    if candidate:
        return candidate
    if fallback_question:
        return fallback_question
    return research_subject


def build_plugin_question_advisory_meta(
    *,
    session_id: str,
    action: str,
    last_question: str | None,
    fallback_question: str,
    research_subject: str,
    score: AmbiguityScore | None,
    runtime_backend: str | None,
    opencode_mode: str | None,
    fanout_registry: FanoutRegistry | None,
    findings_store: Any | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    _attach_question_assist_requests(
        meta,
        session_id=session_id,
        question=plugin_factual_question(
            last_question=last_question,
            fallback_question=fallback_question,
            research_subject=research_subject,
        ),
        phase="resume_pending" if action == "resume" else action,
        score=score,
        last_question=(str(last_question) if last_question else None),
        research_subject=research_subject,
        dispatch_mode=resolve_request_subagent_dispatch(runtime_backend, opencode_mode),
        runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
        fanout_registry=fanout_registry,
        findings_store=findings_store,
    )
    return meta
