"""Keep internal LLM calls on backends that support completions."""

from collections.abc import Callable

from ouroboros.backends import get_backend_capability
from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ConfigError


def _supports_llm(name: str | None) -> bool:
    """Whether a backend can serve LLM completions rather than runtime calls."""
    if not name:
        return False
    capability = get_backend_capability(name)
    return capability is not None and capability.supports_llm


def guard_completion_backend(
    resolved: str,
    *,
    explicit_override: Callable[[], str | None],
    get_llm_backend: Callable[[], str],
    load_config: Callable[[], OuroborosConfig],
) -> str:
    """Resolve a completion-capable backend using the loader's fallback order.

    A runtime-only environment override can be returned by ``get_llm_backend``
    even after rejecting a runtime-only explicit backend. In that case prefer
    the configured LLM backend, then the documented ``claude_code`` default.
    """
    if _supports_llm(resolved):
        return resolved
    override = explicit_override()
    if override and _supports_llm(override):
        return override
    fallback = get_llm_backend()
    if _supports_llm(fallback):
        return fallback
    try:
        configured_backend = load_config().llm.backend
    except ConfigError:
        configured_backend = None
    if _supports_llm(configured_backend):
        return configured_backend
    return "claude_code"
