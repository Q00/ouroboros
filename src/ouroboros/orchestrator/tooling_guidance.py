"""Tooling guidance for runtimes that receive tool names as prompt text.

Runtimes such as Codex take no native tool parameter, so the orchestrator's
tool envelope reaches the model as a ``<tooling-guidance>`` block. The
envelope uses Claude tool names (``DEFAULT_TOOLS``); a runtime that does not
have those tools must not be told to use them.
"""

from __future__ import annotations

from ouroboros.orchestrator.adapter import DEFAULT_TOOLS


def render_tooling_guidance(tools: list[str], *, claude_tool_names: bool) -> str:
    """Render the ``<tooling-guidance>`` block for a tool envelope.

    Args:
        tools: The orchestrator's tool envelope for this call.
        claude_tool_names: Whether the runtime has the Claude tools named in
            ``DEFAULT_TOOLS``. When it does not, those names are dropped and
            the runtime is pointed at its own shell and file tools.
    """
    guidance = "Do not use any tools for this task. Respond using reasoning only."
    if tools and not claude_tool_names:
        own_tools = [tool for tool in tools if tool not in DEFAULT_TOOLS]
        guidance = "Use your own shell and file tools to solve the task."
        if own_tools:
            tool_list = "\n".join(f"- {tool}" for tool in own_tools)
            guidance += f" You may also use the following tools:\n{tool_list}"
    elif tools:
        tool_list = "\n".join(f"- {tool}" for tool in tools)
        guidance = (
            f"Prefer to solve the task using the following tool set when possible:\n{tool_list}"
        )
    return f"<tooling-guidance>\n{guidance}\n</tooling-guidance>"


__all__ = ["render_tooling_guidance"]
