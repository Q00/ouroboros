"""MCP surface for interview-less Seed crystallization (RFC D6).

Split out of the grandfathered ``authoring_handlers.py`` (#1797 module-size
ratchet). Fully deterministic: the supplied material is anchored verbatim,
the gate is structural completeness, and an incomplete submission returns
the specific gap questions (status ``gap_questions_required``) for the host
to relay — never the blocked/force binary of the interview path.
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml

from ouroboros.bigbang.session_seed import build_session_context_seed
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

log = structlog.get_logger(__name__)


def handle_session_context_seed(
    session_context: Any,
) -> Result[MCPToolResult, MCPServerError]:
    """Crystallize a Seed from host-settled context — no interview, no LLM."""
    if not isinstance(session_context, dict):
        return Result.err(
            MCPToolError(
                "session_context must be an object with goal and acceptance_criteria",
                tool_name="ouroboros_generate_seed",
            )
        )

    outcome = build_session_context_seed(session_context)
    if outcome.gap_questions:
        log.info(
            "mcp.tool.generate_seed.session_context.gaps",
            gap_count=len(outcome.gap_questions),
        )
        questions_text = "\n".join(f"- {q}" for q in outcome.gap_questions)
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            "Seed not generated yet — the session context "
                            "is missing pieces. Ask the user exactly these "
                            "questions, then call ouroboros_generate_seed "
                            "again with the answers merged into "
                            "session_context:\n" + questions_text
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "status": "gap_questions_required",
                    "gap_questions": list(outcome.gap_questions),
                    "source": "session_context",
                },
            )
        )

    seed = outcome.seed
    assert seed is not None  # gap-free outcome always carries a seed
    seed_yaml = yaml.dump(
        seed.to_dict(),
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    log.info(
        "mcp.tool.generate_seed.session_context",
        seed_id=seed.metadata.seed_id,
        criteria_count=len(seed.acceptance_criteria),
    )
    result_text = (
        "Seed Generated Successfully (interview-less)\n"
        "============================================\n"
        f"Seed ID: {seed.metadata.seed_id}\n"
        f"Ambiguity Score: {seed.metadata.ambiguity_score:.2f} "
        "(conservative ceiling — gate was structural, not scored)\n"
        f"Goal: {seed.goal}\n\n"
        "--- Seed YAML ---\n"
        f"{seed_yaml}"
    )
    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
            is_error=False,
            meta={
                "seed_id": seed.metadata.seed_id,
                "interview_id": seed.metadata.interview_id,
                "ambiguity_score": seed.metadata.ambiguity_score,
                "source": "session_context",
                "status": "seed_generated",
            },
        )
    )


__all__ = ["handle_session_context_seed"]
