"""The interview's own half of its advisory turn.

The fan-out itself lives in ``question_advisory``, which serves every tool that
declares a catalog — one implementation, so the interview and the PM interview
cannot drift apart where being identical matters. What lives here is the part
that is only the interview's: the code-fact investigation request, which exists
because in this interview a code fact may become the answer.

Extracted from ``authoring_handlers`` so that module can shrink rather than
grow, which is the #1797 ratchet's standing instruction for it.
"""

from __future__ import annotations

from typing import Any

from ouroboros.bigbang.ambiguity import AmbiguityScore, get_milestone
from ouroboros.mcp.tools.fanout import FanoutRegistry
from ouroboros.mcp.tools.question_advisory import attach_question_advisory
from ouroboros.mcp.tools.subagent import SubagentDispatchMode
from ouroboros.orchestrator.capabilities import (
    ouroboros_tool_capability_metadata,
    stable_code_investigation_question_identity,
)
from ouroboros.orchestrator.capabilities.interview_schemas import (
    interview_code_investigation_answer_contract,
)


def _milestone_for_score(score: AmbiguityScore | None) -> str | None:
    """Return the milestone label for an ambiguity score, or None."""
    if score is None:
        return None
    milestone, _ = get_milestone(score.overall_score)
    return milestone.value


def _build_code_investigation_request(
    *,
    session_id: str,
    question: str,
    last_question: str | None = None,
) -> dict[str, Any]:
    """Build stable metadata for caller-side code-fact investigation.

    The interview MCP tool is only the question generator; repo inspection runs
    in the parent runtime. This metadata gives that runtime a concrete request
    envelope keyed to the exact question text, so investigation results can be
    correlated even when the user-facing display has an ambiguity prefix.
    """
    mcp_tool_capability = ouroboros_tool_capability_metadata("ouroboros_interview")
    code_investigation = mcp_tool_capability["orchestration"]["code_investigation"]
    request: dict[str, Any] = {
        "session_id": session_id,
        "question_identity": stable_code_investigation_question_identity(question),
        "question": question,
        "investigation_goal": "describe_current_state_from_code",
        "investigation_targets": [{"target_type": "workspace", "scope": "active"}],
        "fact_categories": [
            "tech_stack",
            "frameworks",
            "dependencies",
            "current_patterns",
            "architecture",
            "file_structure",
            "configuration",
        ],
        "allowed_capabilities": ["inspect_code"],
        "repo_inspection_tool_capabilities": list(
            code_investigation["repo_inspection_tool_capabilities"]
        ),
        "confidence_policy": {
            "auto_confirm_when": ["exact manifest or config match with a single clear answer"],
            "confirmation_required_when": [
                "multiple code paths or configuration sources disagree",
                "the answer implies a product or architecture decision",
            ],
            "human_judgment_when": [
                "desired future behavior",
                "priority, scope, ownership, rollout, or acceptance criteria",
            ],
        },
        "answer_prefixes": ["[from-code]", "[from-code][auto-confirmed]"],
        "answer_contract": interview_code_investigation_answer_contract(),
        "mcp_tool_capability": mcp_tool_capability,
    }
    if last_question:
        request["last_question"] = last_question
    return request


def _attach_question_assist_requests(
    meta: dict[str, Any],
    *,
    session_id: str,
    question: str,
    phase: str,
    score: AmbiguityScore | None,
    last_question: str | None = None,
    research_subject: str | None = None,
    dispatch_mode: SubagentDispatchMode = SubagentDispatchMode.SEQUENTIAL,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
    fanout_registry: FanoutRegistry | None = None,
    findings_store: Any | None = None,
) -> None:
    """Attach the interview's code-fact request and its advisory fan-out.

    The fan-out itself lives in ``question_advisory``, which serves every tool
    that declares a catalog — one implementation, so the interview and the PM
    interview cannot drift apart in the places where being identical matters.
    What stays here is the part that is only the interview's: the code-fact
    investigation request, which exists because in this interview a code fact
    may become the answer.
    """
    code_request = _build_code_investigation_request(
        session_id=session_id,
        question=question,
        last_question=last_question,
    )
    attach_question_advisory(
        meta,
        tool_name="ouroboros_interview",
        session_id=session_id,
        question=question,
        phase=phase,
        ambiguity_score=score.overall_score if score is not None else None,
        milestone=_milestone_for_score(score),
        code_investigation_request=code_request,
        last_question=last_question,
        research_subject=(research_subject[:3500] if research_subject else None),
        dispatch_mode=dispatch_mode,
        runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
        fanout_registry=fanout_registry,
        findings_store=findings_store,
    )
