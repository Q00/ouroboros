"""Shared rendering for ``ooo auto`` resume/retry/start-fresh hint lines.

The CLI (``cli/commands/auto.py``) and the MCP surface
(``mcp/tools/auto_handler.py``) both display a hint after every status or
result print. The exact wording depends on the persisted
``AutoResumeCapability`` for the session — :func:`render_resume_lines`
centralizes that matrix so all surfaces stay in lockstep.

Three call sites consume this helper:

* ``_print_status`` (CLI) — has full :class:`AutoPipelineState`; passes the
  goal so a ``Start fresh:`` hint can be produced when the capability is
  ``NONE`` and the phase is terminal.
* ``_print_result`` (CLI) — only has :class:`AutoPipelineResult`; cannot
  reconstruct ``goal``, so it omits the ``Start fresh:`` hint entirely
  for ``NONE``.
* ``_format_result`` (MCP) — same constraint as ``_print_result``.
"""

from __future__ import annotations

from ouroboros.auto.state import AutoResumeCapability


def render_resume_lines(
    capability: AutoResumeCapability,
    auto_session_id: str,
    *,
    goal: str | None = None,
    use_markup: bool = False,
) -> list[str]:
    """Return the hint lines to render for ``capability``.

    Args:
        capability: Classification of what ``--resume`` would actually do.
        auto_session_id: The persisted auto session id.
        goal: Optional original goal string. When provided and the capability
            is ``NONE``, a ``Start fresh:`` hint is emitted. Pass ``None``
            (the default) when the goal is not available — for example
            from result-only surfaces.
        use_markup: When ``True`` wrap the command portion in Rich
            ``[bold]...[/]`` markup. CLI surfaces use this for inline
            highlighting; MCP plain-text rendering must leave it ``False``.

    Returns:
        A list of strings, one per console line. May be empty (e.g. when
        capability is ``NONE`` and no goal is supplied — the COMPLETE
        phase falls into this bucket).
    """

    def cmd(text: str) -> str:
        return f"[bold]{text}[/]" if use_markup else text

    if capability is AutoResumeCapability.RESUME:
        return [f"Resume: {cmd(f'ooo auto --resume {auto_session_id}')}"]
    if capability is AutoResumeCapability.PARTIAL_RESUME:
        return [
            f"Resume (partial): {cmd(f'ooo auto --resume {auto_session_id}')}",
            "  Note: some progress preserved but the exact pick-up point may be approximate",
        ]
    if capability is AutoResumeCapability.RETRY:
        return [
            f"Retry: {cmd(f'ooo auto --resume {auto_session_id}')}",
            "  Note: no prior session context — this re-runs the failed step from scratch",
        ]
    # AutoResumeCapability.NONE
    if goal is not None and goal.strip():
        return [f"Start fresh: {cmd(f'ooo auto "{goal}"')}"]
    return []


__all__ = ["render_resume_lines"]
