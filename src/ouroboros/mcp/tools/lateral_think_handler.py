"""Handler for the ``ouroboros_lateral_think`` tool.

Moved wholesale out of the grandfathered ``evaluation_handlers.py`` (#1797
module-size ratchet) when the decision-advisory mode landed (grounded-lateral
RFC D2). ``evaluation_handlers`` re-exports :class:`LateralThinkHandler` so
every existing import path keeps working.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
from typing import Any

import structlog

from ouroboros.backends import build_runtime_subagent_orchestration_contract
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.host_context import (
    render_lateral_host_banner,
    resolve_request_subagent_dispatch,
)
from ouroboros.mcp.telemetry_boundary import record_subagent_dispatch_emitted
from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin
from ouroboros.mcp.tools.fanout import FanoutRegistry
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)

log = structlog.get_logger(__name__)


def _lateral_mode_parameters() -> tuple[MCPToolParameter, ...]:
    from ouroboros.mcp.tools.lateral_decision import LATERAL_MODE_PARAMETERS

    return LATERAL_MODE_PARAMETERS


@dataclass
class LateralThinkHandler(BridgeAwareMixin):
    """Handler for the lateral_think tool.

    Generates alternative thinking approaches using lateral thinking personas
    to break through stagnation in problem-solving.

    Inherits :class:`BridgeAwareMixin` (#475) so the composition root's
    loop-injection populates ``mcp_manager`` and ``mcp_tool_prefix``
    automatically when an MCP bridge is configured. The bridge fields
    are not consumed by this PR — a follow-up slice forwards them into
    the lateral-think dispatch path so dynamic external MCP servers
    reach the unstuck pipeline.

    The multi-persona fan-out path resolves a 3-way dispatch mode via
    ``resolve_subagent_dispatch(agent_runtime_backend, opencode_mode)``:

    - ``PLUGIN_PASSIVE`` (OpenCode + ``opencode_mode=plugin``): emit a
      ``_subagents`` envelope for the bridge plugin to consume.
    - ``HOST_DRIVEN`` (e.g. Codex): no passive bridge, but the host model can
      spawn subagents itself, so emit the inline result stamped with
      ``dispatch_mode=host_driven`` / ``host_action=spawn_subagents`` so the
      host fans out via its native primitive.
    - ``SEQUENTIAL`` (subprocess / runtimes without a parallel primitive): fall
      back to a plain inline multi-persona ``sequential`` text response
      (`inline_fallback` is preserved as a legacy alias in metadata).

    Attributes:
        agent_runtime_backend: Configured runtime (e.g. ``"opencode"``).
        opencode_mode: Configured ``orchestrator.opencode_mode`` value
            (``"plugin"`` or ``"subprocess"``). ``None`` falls through as
            non-plugin (safe default — see ``resolve_subagent_dispatch``).
    """

    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    fanout_registry: FanoutRegistry | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lateral_think",
            description=(
                "Generate alternative thinking approaches using lateral thinking personas. "
                "Use when: stuck on a problem, going in circles, the same fix has "
                "failed twice, progress has stalled, OR the user faces a "
                "consequential choice with no clear winner (architecture, library, "
                "trade-off) — call proactively on these "
                "signals; do not wait for the user to ask for 'lateral' or 'unstuck'. "
                "Result: fresh perspectives from "
                "different thinking modes: hacker (unconventional workarounds), "
                "researcher (seeks information), simplifier (reduces complexity), "
                "architect (restructures approach), or contrarian (challenges assumptions). "
                "For decisions, pass mode='decision': personas advise on the choice "
                "and the synthesis must converge on ONE recommendation with flip "
                "conditions. "
                "Do not use when: work is progressing normally with no stagnation "
                "signal and no open decision. "
                "Set persona='all' (or pass personas=['hacker','architect',...]) to "
                "fan out to MULTIPLE personas in parallel — each runs in its own "
                "Task pane with an independent LLM context (no cross-contamination)."
            ),
            parameters=(
                MCPToolParameter(
                    name="problem_context",
                    type=ToolInputType.STRING,
                    description="Description of the stuck situation or problem",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_approach",
                    type=ToolInputType.STRING,
                    description="What has been tried so far that isn't working",
                    required=True,
                ),
                MCPToolParameter(
                    name="persona",
                    type=ToolInputType.STRING,
                    description=(
                        "Single persona (hacker, researcher, simplifier, architect, "
                        "contrarian) OR 'all' to dispatch ALL 5 personas in parallel "
                        "as separate Task panes."
                    ),
                    required=False,
                    enum=(
                        "hacker",
                        "researcher",
                        "simplifier",
                        "architect",
                        "contrarian",
                        "all",
                    ),
                ),
                MCPToolParameter(
                    name="stagnation_pattern",
                    type=ToolInputType.STRING,
                    description=(
                        "Detected stagnation pattern used to suggest a persona when "
                        "persona is omitted."
                    ),
                    required=False,
                    enum=(
                        "spinning",
                        "oscillation",
                        "no_drift",
                        "diminishing_returns",
                    ),
                ),
                MCPToolParameter(
                    name="personas",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Explicit list of personas to dispatch in parallel. "
                        "Takes precedence over 'persona' arg. Example: "
                        "['hacker','contrarian','architect']. Each runs in its "
                        "own parallel Task pane."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="failed_attempts",
                    type=ToolInputType.ARRAY,
                    description="Previous failed approaches to avoid repeating",
                    required=False,
                ),
                *_lateral_mode_parameters(),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lateral thinking request.

        Two modes:
        - Single persona (default): return one prompt directly as text.
        - Multi-persona parallel: when ``persona='all'`` or ``personas=[...]``
          is passed, dispatch N subagents in parallel (one per persona) via
          the ``_subagents`` bridge payload. Each runs in its own Task pane
          with an independent LLM context.

        Args:
            arguments: Tool arguments including problem_context and current_approach.

        Returns:
            Result containing lateral thinking prompt(s) or error.
        """
        from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona
        from ouroboros.resilience.stagnation import StagnationPattern

        problem_context = arguments.get("problem_context")
        if not problem_context:
            return Result.err(
                MCPToolError(
                    "problem_context is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        current_approach = arguments.get("current_approach")
        if not current_approach:
            return Result.err(
                MCPToolError(
                    "current_approach is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        failed_attempts_raw = arguments.get("failed_attempts") or []
        failed_attempts = tuple(str(a) for a in failed_attempts_raw if a)

        from ouroboros.mcp.tools.lateral_decision import (
            LATERAL_MODES,
            stamp_decision_meta,
        )

        mode = str(arguments.get("mode") or "unstuck").strip().lower()
        if mode not in LATERAL_MODES:
            return Result.err(
                MCPToolError(
                    f"Invalid mode: {mode}. Must be 'unstuck' or 'decision'.",
                    tool_name="ouroboros_lateral_think",
                )
            )
        research = bool(arguments.get("research"))

        # --- Parallel multi-persona dispatch path ---
        explicit_list = arguments.get("personas")
        raw_persona_arg = arguments.get("persona")
        if explicit_list or raw_persona_arg is None:
            persona_arg = ""
        else:
            persona_arg = str(raw_persona_arg).strip()
            if not persona_arg:
                return Result.err(
                    MCPToolError(
                        "persona cannot be blank",
                        tool_name="ouroboros_lateral_think",
                    )
                )
        dispatch_all = persona_arg == "all"
        if mode == "decision" and not explicit_list and not persona_arg:
            # A decision advisory is a debate by construction — full fan-out.
            dispatch_all = True

        if explicit_list or dispatch_all:
            from ouroboros.mcp.tools.subagent import (
                SubagentDispatchMode,
                build_lateral_multi_subagent,
                build_multi_subagent_result,
                lateral_persona_panel_metadata_from_capability_definitions,
                stamp_fanout_meta,
                stamp_lateral_persona_fanout,
            )

            if explicit_list:
                # Coerce each item to str, drop blanks/nulls, dedupe preserving order.
                seen_p: set[str] = set()
                personas_list: list[str] = []
                for item in explicit_list:
                    s = str(item).strip() if item is not None else ""
                    if s and s not in seen_p:
                        seen_p.add(s)
                        personas_list.append(s)
                if not personas_list:
                    return Result.err(
                        MCPToolError(
                            "personas list is empty or contains only blank/null items",
                            tool_name="ouroboros_lateral_think",
                        )
                    )
            else:
                # persona="all" → use every persona
                personas_list = [p.value for p in ThinkingPersona]

            try:
                payloads = build_lateral_multi_subagent(
                    personas=personas_list,
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                    mode=mode,
                    research=research,
                )
            except ValueError as e:
                return Result.err(
                    MCPToolError(
                        str(e),
                        tool_name="ouroboros_lateral_think",
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.error("mcp.tool.lateral_think.multi.error", error=str(e))
                return Result.err(
                    MCPToolError(
                        f"Unexpected error building multi-persona dispatch: {e}",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            log.info(
                "mcp.tool.lateral_think.multi",
                persona_count=len(payloads),
                context_length=len(str(problem_context)),
                failed_count=len(failed_attempts),
                mode=mode,
                research=research,
            )

            # Resolve the 3-way dispatch mode (the production source of truth).
            #   - PLUGIN_PASSIVE: a bridge plugin will consume the ``_subagents``
            #     envelope, so emit it and skip the inline work.
            #   - HOST_DRIVEN: no passive receiver, but the host model can spawn
            #     from inline payloads via its own primitive (e.g. Codex). Emit
            #     the inline result stamped with ``host_action=spawn_subagents``.
            #   - SEQUENTIAL: no parallel surface at all → plain inline fallback.
            dispatch = resolve_request_subagent_dispatch(
                self.agent_runtime_backend,
                self.opencode_mode,
            )
            if dispatch is SubagentDispatchMode.PLUGIN_PASSIVE:
                # Preserve public response shape (#442): ouroboros_lateral_think
                # natural response documents alternative-thinking metadata.
                # Expose persona_count + dispatch status at top level so callers
                # can branch on delegation without parsing the envelope.
                record_subagent_dispatch_emitted(
                    fanout_kind="lateral_persona_panel",
                    payload_count=len(payloads),
                    dispatch_mode=dispatch,
                    worker_backend=self.agent_runtime_backend,
                    fanout_reentry_available=False,
                )
                return build_multi_subagent_result(
                    payloads,
                    response_shape=stamp_decision_meta(
                        {
                            "status": "delegated_to_subagent",
                            "dispatch_mode": "plugin",
                            "persona_count": len(payloads),
                        },
                        mode,
                    ),
                )

            # --- Inline/sequential fallback: concatenate persona prompts ---
            thinker = LateralThinker()
            sections: list[str] = []
            for p_str in personas_list:
                try:
                    p_enum = ThinkingPersona(p_str)
                except ValueError:
                    continue
                lateral_res = thinker.generate_alternative(
                    persona=p_enum,
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                )
                if lateral_res.is_err:
                    continue
                lr = lateral_res.unwrap()
                sections.append(f"# Lateral Thinking: {lr.approach_summary}\n\n{lr.prompt}")

            if not sections:
                return Result.err(
                    MCPToolError(
                        "No valid personas produced output for inline fallback",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            combined = "\n\n---\n\n".join(sections)
            # Expose the canonical per-persona payloads on inline responses
            # too, so non-plugin runtimes (Claude Code, Codex CLI, OpenCode
            # subprocess) can drive their own sub-agent fan-out from the
            # same structured prompts that plugin mode dispatches via
            # `_subagents`. The MCP SDK adapter preserves `meta`, but
            # older bridge consumers still read only `text_content`, so the
            # dispatch payload continues to ride inside `content`.
            #
            # Format: a hidden HTML-comment block with a versioned sentinel,
            # carrying the dispatch JSON base64-encoded inside the comment.
            # Two reasons for base64:
            #   1. Base64's alphabet is [A-Za-z0-9+/=]. It cannot contain
            #      `-->`, so a user-supplied `problem_context` like an
            #      HTML/JS debugging snippet that itself includes `-->`
            #      cannot prematurely close the comment and leak the
            #      payload into the visible markdown.
            #   2. Base64 has no significant whitespace, so line wrapping
            #      and trimming can't corrupt the encoded body.
            # HOST_DRIVEN runtimes (e.g. Codex) have no passive bridge but can
            # spawn subagents themselves. SEQUENTIAL runtimes now get the same
            # machine-readable contract vocabulary, while preserving
            # ``inline_fallback`` as a legacy alias for older skill prose.
            payload_dicts = [p.to_dict() for p in payloads]
            panel_metadata = lateral_persona_panel_metadata_from_capability_definitions()
            contract = build_runtime_subagent_orchestration_contract(
                self.agent_runtime_backend or "unknown",
                directive_metadata=panel_metadata,
                opencode_mode=self.opencode_mode,
                dispatch_mode=dispatch,
            )
            dispatch_record: dict[str, Any] = {
                "persona_count": len(sections),
                "payloads": payload_dicts,
                "subagent_orchestration_instruction": contract.runtime_instruction_handling,
            }
            stamp_decision_meta(dispatch_record, mode)
            # Stamp the PR-C-standardized 3-mode contract (dispatch_mode /
            # host_action / result_correlation_key) via the shared helper. Only
            # HOST_DRIVEN and SEQUENTIAL reach here (PLUGIN_PASSIVE returned the
            # ``_subagents`` envelope above), so a cue is always stamped.
            stamp_fanout_meta(
                dispatch_record,
                prefix="",
                dispatch_mode=dispatch,
                payloads=payloads,
                correlation_key="context.persona",
            )
            if dispatch is SubagentDispatchMode.SEQUENTIAL:
                dispatch_record["legacy_dispatch_mode"] = "inline_fallback"
            stamp_lateral_persona_fanout(
                dispatch_record,
                self.fanout_registry,
                session_id=str(arguments.get("session_id") or ""),
                payloads=payloads,
            )
            record_subagent_dispatch_emitted(
                fanout_kind="lateral_persona_panel",
                payload_count=len(payloads),
                dispatch_mode=dispatch,
                worker_backend=self.agent_runtime_backend,
                fanout_reentry_available="fanout_id" in dispatch_record,
            )
            dispatch_blob = json.dumps(dispatch_record)
            dispatch_b64 = base64.b64encode(dispatch_blob.encode("utf-8")).decode("ascii")
            host_banner = render_lateral_host_banner(dispatch, len(sections))
            content_text = (
                f"{host_banner}{combined}\n\n"
                "<!-- ouroboros-lateral-inline-dispatch-v1 base64\n"
                f"{dispatch_b64}\n"
                "-->"
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=content_text),),
                    is_error=False,
                    meta=dispatch_record,
                )
            )

        # --- Single-persona path ---
        if not persona_arg:
            stagnation_pattern_arg = arguments.get("stagnation_pattern")
            if stagnation_pattern_arg:
                try:
                    stagnation_pattern = StagnationPattern(str(stagnation_pattern_arg))
                except ValueError:
                    return Result.err(
                        MCPToolError(
                            (
                                f"Invalid stagnation_pattern: {stagnation_pattern_arg}. "
                                "Must be one of: spinning, oscillation, no_drift, "
                                "diminishing_returns"
                            ),
                            tool_name="ouroboros_lateral_think",
                        )
                    )

                from ouroboros.resilience.recovery import suggest_lateral_persona_for_pattern

                suggested = suggest_lateral_persona_for_pattern(
                    stagnation_pattern,
                    failed_attempts=failed_attempts,
                )
                if suggested is None:
                    return Result.err(
                        MCPToolError(
                            (
                                "No available lateral thinking persona remains after "
                                "applying failed_attempts exclusions"
                            ),
                            tool_name="ouroboros_lateral_think",
                        )
                    )
                persona_arg = suggested.value
            else:
                persona_arg = ThinkingPersona.CONTRARIAN.value

        try:
            persona = ThinkingPersona(persona_arg)
        except ValueError:
            return Result.err(
                MCPToolError(
                    f"Invalid persona: {persona_arg}. Must be one of: "
                    f"hacker, researcher, simplifier, architect, contrarian, all",
                    tool_name="ouroboros_lateral_think",
                )
            )

        log.info(
            "mcp.tool.lateral_think",
            persona=persona.value,
            context_length=len(str(problem_context)),
            failed_count=len(failed_attempts),
        )

        # Plugin mode: dispatch even a single persona as a subagent so the
        # LLM in the child Task pane does the actual thinking — the parent
        # session stays responsive and gets the result asynchronously.
        #
        # ``should_dispatch_via_plugin`` is also imported locally in the
        # multi-persona branch above, which makes Python treat it as a
        # function-local name throughout this method — so it must be
        # (re-)imported on this branch too before use, even though it is
        # available at module scope. ``build_subagent_result`` is module
        # scope; importing it here as well keeps the original binding intact.
        from ouroboros.mcp.tools.subagent import (  # noqa: F811
            build_subagent_result,
            should_dispatch_via_plugin,
        )

        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            from ouroboros.mcp.tools.subagent import build_lateral_multi_subagent

            try:
                payloads = build_lateral_multi_subagent(
                    personas=[persona.value],
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                    mode=mode,
                    research=research,
                )
            except (ValueError, Exception) as e:  # noqa: BLE001
                log.error("mcp.tool.lateral_think.single_dispatch.error", error=str(e))
                return Result.err(
                    MCPToolError(
                        f"Failed to build single-persona subagent: {e}",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            # Single payload → single _subagent envelope (not _subagents array)
            return build_subagent_result(
                payloads[0],
                response_shape=stamp_decision_meta(
                    {
                        "status": "delegated_to_subagent",
                        "dispatch_mode": "plugin",
                        "persona": persona.value,
                    },
                    mode,
                ),
            )

        # Inline fallback for subprocess / non-OpenCode runtimes.
        try:
            thinker = LateralThinker()
            result = thinker.generate_alternative(
                persona=persona,
                problem_context=str(problem_context),
                current_approach=str(current_approach),
                failed_attempts=failed_attempts,
            )

            if result.is_err:
                return Result.err(
                    MCPToolError(
                        result.error,
                        tool_name="ouroboros_lateral_think",
                    )
                )

            lateral_result = result.unwrap()

            # Build the response
            response_text = (
                f"# Lateral Thinking: {lateral_result.approach_summary}\n\n"
                f"{lateral_result.prompt}\n\n"
                "## Questions to Consider\n"
            )
            for question in lateral_result.questions:
                response_text += f"- {question}\n"
            if mode == "decision":
                from ouroboros.mcp.tools.lateral_decision import (
                    DECISION_INLINE_CONTRACT_TEXT,
                )

                response_text += DECISION_INLINE_CONTRACT_TEXT

            single_meta = stamp_decision_meta(
                {
                    "persona": lateral_result.persona.value,
                    "approach_summary": lateral_result.approach_summary,
                    "questions_count": len(lateral_result.questions),
                },
                mode,
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=response_text),),
                    is_error=False,
                    meta=single_meta,
                )
            )
        except Exception as e:
            log.error("mcp.tool.lateral_think.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Lateral thinking failed: {e}",
                    tool_name="ouroboros_lateral_think",
                )
            )


__all__ = ["LateralThinkHandler"]
