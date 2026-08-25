"""Rendering and ownership judgment for GJC's always-applied routing guide."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ouroboros.backends.capabilities import render_backend_skill_capability_guide
from ouroboros.gjc.artifacts import GJC_SKILL_NAMESPACE

_OWNERSHIP_PREFIX = "<!-- ouroboros:gjc-guide-sha256:"


def _render_gjc_guide_body() -> str:
    """Render GJC's always-applied exact-command routing and capability guide."""
    from ouroboros.router import packaged_skill_dispatch_registry

    with packaged_skill_dispatch_registry() as registry:
        routes = sorted(
            (
                identifier,
                f"{GJC_SKILL_NAMESPACE}{target.skill_name}",
            )
            for identifier, target in registry.mapping.items()
            if identifier != "ooo"
        )
    lines = [
        "---",
        "alwaysApply: true",
        "description: Deterministic Ouroboros command routing for GJC",
        "---",
        "",
        "## Ouroboros command routing",
        "",
        "Exact `ooo` commands are explicit skill invocations, not ordinary natural-language requests.",
        "They MUST be routed before generic planning, interview, search, or implementation skills:",
        "",
        "- Bare `ooo` → invoke `/skill:ouroboros-ooo`.",
    ]
    lines.extend(
        f"- `ooo {identifier} [arguments]` → invoke `/skill:{skill_name} [arguments]`."
        for identifier, skill_name in routes
    )
    lines.extend(
        (
            "",
            "Preserve every argument after the command prefix verbatim. Do not inspect the repository,",
            "infer another workflow, or route to GJC's bundled `deep-interview` when an exact route above matches.",
            "",
            render_backend_skill_capability_guide("gjc").rstrip(),
            "",
        )
    )
    return "\n".join(lines)


def render_gjc_guide() -> str:
    """Render one complete guide generation with its ownership digest."""
    body = _render_gjc_guide_body()
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{body}{_OWNERSHIP_PREFIX}{digest} -->\n"


def is_setup_managed_gjc_instruction(path: str | Path) -> bool:
    """Return whether *path* is a complete routing guide emitted by setup."""
    candidate = Path(path)
    try:
        source = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if candidate.is_symlink():
        return False
    if source == _render_gjc_guide_body():
        return True
    body, separator, digest_suffix = source.rpartition(_OWNERSHIP_PREFIX)
    if not separator or not digest_suffix.endswith(" -->\n"):
        return False
    digest = digest_suffix.removesuffix(" -->\n")
    return (
        len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and hashlib.sha256(body.encode("utf-8")).hexdigest() == digest
    )
