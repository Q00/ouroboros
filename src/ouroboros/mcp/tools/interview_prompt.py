"""Bounded prompt helpers for interview subagent dispatch."""

INTERVIEW_SYSTEM_PROMPT_MAX_CHARS = 3_150


def bounded_system_prompt(text: str) -> str:
    """Preserve opening and closing rules while bounding long instructions."""
    if len(text) <= INTERVIEW_SYSTEM_PROMPT_MAX_CHARS:
        return text
    marker = "\n[truncated]\n"
    available = INTERVIEW_SYSTEM_PROMPT_MAX_CHARS - len(marker)
    head_chars = available * 3 // 4
    tail_chars = available - head_chars
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}"
