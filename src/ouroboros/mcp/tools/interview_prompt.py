"""Bounded prompt helpers for interview subagent dispatch."""

INTERVIEW_SYSTEM_PROMPT_MAX_CHARS = 3_200


def bounded_system_prompt(text: str) -> str:
    """Preserve opening and closing rules while bounding long instructions."""
    if len(text) <= INTERVIEW_SYSTEM_PROMPT_MAX_CHARS:
        return text
    # Brownfield facts are already supplied in the caller's transcript and
    # tagged context. Remove that redundant explanatory section as a unit so
    # the execution-critical closure contract is never split or truncated.
    compacted = text
    for section, following in (
        ("BROWNFIELD CONTEXT", "QUESTIONING STRATEGY"),
        ("CONTEXT BOUNDARIES", "RESPONSE FORMAT"),
    ):
        start = compacted.find(f"\n## {section}\n")
        end = compacted.find(f"\n## {following}\n", start + 1)
        if start >= 0 and end > start:
            compacted = compacted[:start] + compacted[end:]
        if len(compacted) <= INTERVIEW_SYSTEM_PROMPT_MAX_CHARS:
            return compacted
    marker = "\n[truncated]\n"
    available = INTERVIEW_SYSTEM_PROMPT_MAX_CHARS - len(marker)
    head_chars = available * 3 // 4
    tail_chars = available - head_chars
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}"
