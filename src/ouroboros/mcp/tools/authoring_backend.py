"""Backend resolution shared by MCP authoring handlers."""

from ouroboros.config import get_llm_backend_for_role


def resolve_backend(
    configured_backend: str | None,
    role: str,
    fallback_runtime_backend: str | None = None,
) -> str:
    """Resolve an authoring backend with the selected runtime as fallback."""
    return get_llm_backend_for_role(
        role,
        explicit_backend=configured_backend,
        fallback_runtime_backend=fallback_runtime_backend,
    )
