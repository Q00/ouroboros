"""Backend capability registry for Ouroboros runtimes and providers."""

from ouroboros.backends.capabilities import (
    BackendCapability,
    get_backend_capability,
    interview_driver_backend_choices,
    llm_backend_choices,
    resolve_backend_alias,
    resolve_interview_driver_backend,
    resolve_llm_backend_name,
    resolve_runtime_backend_name,
    runtime_backend_choices,
    soft_tool_enforcement_backends,
)

__all__ = [
    "BackendCapability",
    "get_backend_capability",
    "interview_driver_backend_choices",
    "llm_backend_choices",
    "resolve_backend_alias",
    "resolve_interview_driver_backend",
    "resolve_llm_backend_name",
    "resolve_runtime_backend_name",
    "runtime_backend_choices",
    "soft_tool_enforcement_backends",
]
