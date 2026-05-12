"""Built-in DomainProfile implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.auto.domain_profile import DomainProfileRegistry

__all__ = ["CODING_PROFILE", "RESEARCH_PROFILE", "register_default_profiles"]


def __getattr__(name: str):
    if name == "CODING_PROFILE":
        from .coding import CODING_PROFILE

        return CODING_PROFILE
    if name == "RESEARCH_PROFILE":
        from .research import RESEARCH_PROFILE

        return RESEARCH_PROFILE
    raise AttributeError(name)


def register_default_profiles(registry: DomainProfileRegistry) -> None:
    """Register built-in profiles into *registry* on demand."""
    from .coding import CODING_PROFILE
    from .research import RESEARCH_PROFILE

    for profile in (CODING_PROFILE, RESEARCH_PROFILE):
        try:
            registry.register(profile)
        except ValueError:
            # Keep repeated lazy-load/importlib scenarios idempotent and let
            # callers intentionally pre-register a replacement of the same name.
            pass
