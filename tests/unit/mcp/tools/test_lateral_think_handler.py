"""Regression tests for :class:`LateralThinkHandler`.

Verifies the multi-persona fan-out path honours the shared
``should_dispatch_via_plugin`` contract:

* Plugin-gated (OpenCode runtime + ``opencode_mode="plugin"`` explicitly) →
  emits a ``_subagents`` envelope for the bridge plugin to consume.
* Non-plugin (``opencode_mode="subprocess"``, unset/None, or non-OpenCode
  runtime) → falls back to inline concatenation of persona prompts so the
  caller gets a useful text response instead of a dead envelope.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler


@pytest.mark.asyncio
async def test_multi_persona_plugin_mode_emits_subagents_envelope() -> None:
    """Plugin mode → the ``_subagents`` envelope is produced for the bridge."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # Envelope is present on meta and as JSON text.
    assert "_subagents" in payload.meta
    assert len(payload.meta["_subagents"]) == 2
    text = payload.content[0].text
    decoded = json.loads(text)
    assert "_subagents" in decoded
    assert len(decoded["_subagents"]) == 2


@pytest.mark.asyncio
async def test_multi_persona_subprocess_mode_falls_back_inline() -> None:
    """Subprocess mode → no envelope, inline concatenated prompt text."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta is not None
    # No envelope in the subprocess fallback path.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "inline_fallback"
    assert payload.meta.get("persona_count") == 2
    text = payload.content[0].text
    # Each persona section is separated by the canonical delimiter.
    assert text.count("\n\n---\n\n") == 1
    assert "Lateral Thinking" in text


@pytest.mark.asyncio
async def test_multi_persona_non_opencode_runtime_falls_back_inline() -> None:
    """Non-OpenCode runtime → inline fallback regardless of ``opencode_mode``."""
    handler = LateralThinkHandler(
        agent_runtime_backend="claude_code",
        opencode_mode="plugin",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "all",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("dispatch_mode") == "inline_fallback"
    # persona='all' expands to every ThinkingPersona (5).
    assert payload.meta.get("persona_count") == 5


@pytest.mark.asyncio
async def test_single_persona_path_unchanged() -> None:
    """Single-persona (default) path does not touch the dispatch gate."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "contrarian",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    # Single-persona path returns inline text unconditionally.
    assert "_subagents" not in (payload.meta or {})
    assert payload.meta.get("persona") == "contrarian"


@pytest.mark.asyncio
async def test_stagnation_pattern_suggests_persona_when_persona_omitted() -> None:
    """stagnation_pattern selects an affinity persona when persona is omitted."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "rerun the same checks",
            "stagnation_pattern": "no_drift",
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("persona") == "researcher"


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["", "   "])
async def test_blank_persona_is_invalid(persona: str) -> None:
    """Blank persona values are invalid rather than treated as omitted."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "rerun the same checks",
            "stagnation_pattern": "no_drift",
            "persona": persona,
        }
    )

    assert result.is_err
    assert "persona cannot be blank" in str(result.error)


@pytest.mark.asyncio
async def test_personas_list_takes_precedence_over_blank_persona() -> None:
    """Explicit personas list is honored even when persona is blank."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "persona": "",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("dispatch_mode") == "inline_fallback"
    assert payload.meta.get("persona_count") == 2


@pytest.mark.asyncio
async def test_stagnation_pattern_excludes_known_failed_personas() -> None:
    """failed_attempts persona names are excluded and unknown values are skipped."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "same failure repeats",
            "current_approach": "retry the same edit",
            "stagnation_pattern": "spinning",
            "failed_attempts": ["hacker", "not-a-persona"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    assert payload.meta.get("persona") == "contrarian"


@pytest.mark.asyncio
async def test_stagnation_pattern_errors_when_all_personas_excluded() -> None:
    """When every persona is excluded, the handler does not repeat one."""
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    result = await handler.handle(
        {
            "problem_context": "progress is flat",
            "current_approach": "tried every persona",
            "stagnation_pattern": "no_drift",
            "failed_attempts": [
                "hacker",
                "researcher",
                "simplifier",
                "architect",
                "contrarian",
            ],
        }
    )

    assert result.is_err
    assert "No available lateral thinking persona remains" in str(result.error)


# ---------------------------------------------------------------------------
# Regression tests for the inline-fallback content-side dispatch contract.
#
# FastMCP's adapter only forwards ``payload.content[0].text`` to the wire
# (``adapter.py:923`` — ``meta`` is dropped, see also
# ``subagent.py:141-144``). The non-plugin debate fan-out therefore depends
# on the canonical per-persona payloads being recoverable from the rendered
# ``content`` text alone. The block below verifies that contract directly.
# ---------------------------------------------------------------------------


_INLINE_DISPATCH_OPEN = "<!-- ouroboros-lateral-inline-dispatch-v1 base64\n"
_INLINE_DISPATCH_CLOSE = "\n-->"


def _extract_inline_dispatch(content_text: str) -> dict:
    """Recover the structured dispatch struct from the rendered content text.

    Mirrors what a SKILL implementer must do on the wire side: locate the
    versioned sentinel block at the end of ``content`` and decode the
    base64-wrapped JSON it carries. Tests use this helper so an escaping or
    formatting regression in the handler is caught before it ships.
    """
    import base64
    import json as _json

    open_idx = content_text.rfind(_INLINE_DISPATCH_OPEN)
    assert open_idx != -1, "inline dispatch sentinel block missing from content"
    close_idx = content_text.rfind(_INLINE_DISPATCH_CLOSE)
    assert close_idx > open_idx, "inline dispatch closing marker missing or misplaced"
    encoded = content_text[open_idx + len(_INLINE_DISPATCH_OPEN) : close_idx]
    decoded = base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    return _json.loads(decoded)


@pytest.mark.asyncio
async def test_inline_fallback_carries_dispatch_block_in_content() -> None:
    """Non-plugin debate response embeds canonical payloads in ``content``."""
    handler = LateralThinkHandler(
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )

    result = await handler.handle(
        {
            "problem_context": "stuck on X",
            "current_approach": "tried Y",
            "personas": ["hacker", "contrarian"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    text = payload.content[0].text

    # Visible markdown sections still survive transport.
    assert text.count("\n\n---\n\n") == 1
    assert "Lateral Thinking" in text

    # The dispatch block survives transport and decodes to canonical
    # structured payloads (one per requested persona).
    dispatch = _extract_inline_dispatch(text)
    assert dispatch["dispatch_mode"] == "inline_fallback"
    assert dispatch["persona_count"] == 2
    payloads = dispatch["payloads"]
    assert len(payloads) == 2

    for persona_name, persona_payload in zip(["hacker", "contrarian"], payloads, strict=True):
        assert persona_payload["title"] == f"Lateral ({persona_name})"
        # The canonical prompt carries the "Task for you (subagent)" wrapper
        # that plugin mode also dispatches — same builder, same prompt.
        assert "Task for you (subagent)" in persona_payload["prompt"]
        assert persona_payload["context"]["persona"] == persona_name
        assert persona_payload["context"]["problem_context"] == "stuck on X"
        assert persona_payload["context"]["current_approach"] == "tried Y"


@pytest.mark.asyncio
async def test_inline_fallback_dispatch_survives_html_close_in_user_context() -> None:
    """User context containing ``-->`` cannot prematurely close the comment.

    The dispatch JSON is base64-encoded inside the HTML comment exactly so
    that an HTML/JS debugging snippet supplied as ``problem_context`` or
    ``current_approach`` cannot leak the structured payload into the
    visible markdown by closing the comment early. Base64's alphabet is
    ``[A-Za-z0-9+/=]`` — ``-->`` cannot occur inside the encoded body.
    """
    handler = LateralThinkHandler(
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )

    adversarial_context = (
        "I'm debugging an HTML template that has `<!-- foo -->` and "
        "JS like `<!--[if IE]>...<![endif]-->` everywhere; "
        "the closing `-->` keeps tripping me up."
    )

    result = await handler.handle(
        {
            "problem_context": adversarial_context,
            "current_approach": "looked for `-->` in the source",
            "personas": ["hacker", "architect"],
        }
    )

    assert result.is_ok, result
    payload = result.unwrap()
    text = payload.content[0].text

    # The visible markdown faithfully echoes the user's content, so `-->`
    # may legitimately appear before the dispatch sentinel — that is a
    # display concern, not a transport one. What matters is that *inside
    # the comment block* the body is base64, so its `[A-Za-z0-9+/=]`
    # alphabet cannot produce a literal `-->` that would prematurely
    # terminate the wrapper. Verify by isolating the comment block and
    # checking it contains exactly one closing `-->` (the legitimate one).
    open_idx = text.rfind(_INLINE_DISPATCH_OPEN)
    assert open_idx != -1
    comment_region = text[open_idx:]
    assert comment_region.count("-->") == 1, (
        "base64 body must not contain a literal `-->` that could close "
        f"the wrapper early: {comment_region!r}"
    )

    # The dispatch block still decodes cleanly and round-trips the
    # adversarial `problem_context`/`current_approach` verbatim — no
    # corruption from the encoding round-trip.
    dispatch = _extract_inline_dispatch(text)
    assert dispatch["dispatch_mode"] == "inline_fallback"
    assert dispatch["persona_count"] == 2
    for persona_payload in dispatch["payloads"]:
        ctx = persona_payload["context"]
        assert ctx["problem_context"] == adversarial_context
        assert ctx["current_approach"] == "looked for `-->` in the source"
