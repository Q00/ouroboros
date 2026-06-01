"""Pi CLI runtime for Ouroboros orchestrator execution."""

from __future__ import annotations

from pathlib import Path

from ouroboros.config import get_pi_cli_path
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime


class PiRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Pi CLI."""

    _runtime_handle_backend = "pi"
    _runtime_backend = "pi"
    _provider_name = "pi_cli"
    _runtime_error_type = "PiRuntimeError"
    _log_namespace = "pi_runtime"
    _display_name = "Pi"
    _default_cli_name = "pi"
    _default_llm_backend = "pi"
    _tempfile_prefix = "ouroboros-pi-"
    _skills_package_uri = "packaged://ouroboros.pi/skills"

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit Pi CLI path from config helpers."""
        return get_pi_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        candidate = cli_path or self._get_configured_cli_path()
        if candidate:
            return str(Path(candidate).expanduser())
        return self._default_cli_name
