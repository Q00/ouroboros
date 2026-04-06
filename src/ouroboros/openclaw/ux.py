"""Discord/OpenClaw command parsing and UX helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedChannelCommand:
    """Parsed control command from a channel message."""

    action: str
    message: str | None = None
    repo: str | None = None
    mode: str | None = None
    usage: str | None = None


def parse_channel_command(message: str) -> ParsedChannelCommand | None:
    """Parse explicit `/ouro ...` commands.

    Supported:
    - `/ouro repo set <repo>`
    - `/ouro status`
    - `/ouro queue`
    - `/ouro poll`
    - `/ouro wait`
    - `/ouro new <message>`
    - `/ouro answer <message>`
    """
    normalized = message.strip()
    if not normalized.startswith("/ouro"):
        return None

    body = normalized[len("/ouro") :].strip()
    if not body:
        return ParsedChannelCommand(action="status")

    if body.startswith("repo set"):
        repo = body[len("repo set") :].strip()
        return ParsedChannelCommand(action="set_repo", repo=repo or None)

    if body == "status":
        return ParsedChannelCommand(action="status")
    if body.startswith("status "):
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro status")

    if body == "queue":
        return ParsedChannelCommand(action="status")
    if body.startswith("queue "):
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro queue")

    if body == "poll":
        return ParsedChannelCommand(action="poll")
    if body.startswith("poll "):
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro poll")

    if body == "wait":
        return ParsedChannelCommand(action="wait")
    if body.startswith("wait "):
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro wait")

    if body.startswith("new "):
        payload = body[len("new ") :].strip()
        return ParsedChannelCommand(
            action="message",
            message=payload or None,
            mode="new",
        )
    if body == "new":
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro new <message>")

    if body.startswith("answer "):
        payload = body[len("answer ") :].strip()
        return ParsedChannelCommand(
            action="message",
            message=payload or None,
            mode="answer",
        )
    if body == "answer":
        return ParsedChannelCommand(action="invalid", usage="Usage: /ouro answer <message>")

    if body.startswith("/"):
        return ParsedChannelCommand(action="invalid", usage="Unknown /ouro command")
    if body.split():
        return ParsedChannelCommand(action="invalid", usage="Unknown /ouro command")
    return ParsedChannelCommand(action="message", message=normalized, mode="auto")
