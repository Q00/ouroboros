"""Async ACP stdio client for DeepSeek Harness (``dsh-acp-demo``).

DeepSeek Harness (https://github.com/deepseek-ai/deepseek-harness) ships an
automation-only Agent Client Protocol server as the published npm bin
``dsh-acp-demo`` (package ``@deepseek-ai/dsh-acp-demo``). The wire protocol is
the same newline-delimited JSON-RPC 2.0 handshake ourocode speaks —
``initialize`` → ``session/new`` → ``session/prompt`` with streamed
``session/update``/``agent_message_chunk`` notifications — so this client is a
thin :class:`~ouroboros.providers.ourocode_acp_client.OurocodeAcpClient`
subclass that only changes how the server process is launched.

dsh-specific launch facts (verified against deepseek-harness@0.1.0-rc.5):

- The bin takes ``--config <cordis.yml>`` and defaults to ``./cordis.yml`` in
  its cwd. The composition file decides everything else (provider, sandbox,
  tools), so this client forwards an optional ``config_path`` and otherwise
  leaves composition to the deployment.
- ``session/new`` **requires** ``mcpServers`` to be present (the server indexes
  ``params.mcpServers.length`` unconditionally and rejects a non-empty list).
  An empty list is therefore always sent.
- Model/provider selection has no per-session ACP parameter; it lives in the
  composition file. There is no ``OUROCODE_MODEL``-style env selector.
- Stdout is protocol-pure JSON-RPC; diagnostics arrive on stderr (surfaced via
  the inherited stderr-tail capture when the process dies).

Like the parent, one process runs exactly one turn: dsh's ACP contract is
"fresh sessions only" (no resume/load), which matches the per-call spawn model
here by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import shutil
from typing import Any

from ouroboros.providers.ourocode_acp_client import (
    AcpClientError,
    AcpTurnResult,
    OurocodeAcpClient,
)

__all__ = ["AcpClientError", "AcpTurnResult", "DshAcpClient"]

_DEFAULT_BIN = "dsh-acp-demo"


class DshAcpClient(OurocodeAcpClient):
    """Run a single completion turn through DeepSeek Harness's ACP server.

    Args:
        cli_path: Path to the ``dsh-acp-demo`` executable. Defaults to
            ``dsh-acp-demo`` on PATH (``npm install -g @deepseek-ai/dsh-acp-demo``).
        cwd: Working directory for the ACP session (``session/new`` requires an
            absolute path; resolved against the process cwd when relative).
        config_path: Optional dsh Cordis composition file passed as
            ``--config``. When ``None`` the bin resolves ``./cordis.yml``
            relative to ``cwd``.
        startup_timeout: Seconds to wait for the initialize/session-new frames.
        turn_timeout: Seconds to wait for the ``session/prompt`` result.
    """

    _TOOL_LABEL = "dsh-acp-demo"

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        config_path: str | Path | None = None,
        startup_timeout: float | None = 30.0,
        turn_timeout: float | None = 600.0,
    ) -> None:
        # The parent's ``model`` is an ourocode env selector with no dsh
        # equivalent (dsh reads the model from its composition file), so it is
        # pinned to a sentinel and never exported (see ``_build_env``).
        super().__init__(
            cli_path=cli_path,
            cwd=cwd,
            model="composition",
            startup_timeout=startup_timeout,
            turn_timeout=turn_timeout,
        )
        self._config_path = str(Path(config_path).expanduser()) if config_path is not None else None

    @staticmethod
    def _resolve_cli_path(cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = Path(cli_path).expanduser()
            return str(candidate) if candidate.exists() else str(cli_path)
        return shutil.which(_DEFAULT_BIN) or _DEFAULT_BIN

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # The dsh process must not inherit Ouroboros runtime/backend selectors
        # (they would confuse a nested ``ooo`` invocation inside the child).
        # Everything else passes through — dsh reads DEEPSEEK_API_KEY (or the
        # credentials its composition names) from the environment.
        for key in (
            "OUROBOROS_AGENT_RUNTIME",
            "OUROBOROS_LLM_BACKEND",
            "OUROBOROS_RUNTIME",
        ):
            env.pop(key, None)
        return env

    def _spawn_argv(self) -> list[str]:
        argv = [self._cli_path]
        if self._config_path is not None:
            argv.extend(["--config", self._config_path])
        return argv

    def _spawn_failure_hint(self) -> str:
        return (
            "Install DeepSeek Harness's ACP server "
            "(`npm install -g @deepseek-ai/dsh-acp-demo`, needs Node.js >= 22) "
            "and ensure `dsh-acp-demo` is on PATH, or set "
            "OUROBOROS_DSH_CLI_PATH."
        )

    def _session_new_params(self) -> dict[str, Any]:
        # ``mcpServers`` is mandatory on dsh's ACP server: it validates
        # ``params.mcpServers.length`` unconditionally and rejects any
        # non-empty list (`mcpServers is not supported`).
        return {"cwd": self._cwd, "mcpServers": []}

    def _error_from_rpc(self, method: str, err: Mapping[str, Any]) -> AcpClientError:
        # The parent maps ourocode's "not signed in" marker to a dedicated
        # error type; dsh has no equivalent marker (a missing API key surfaces
        # as an ordinary RPC/model error), so every RPC error stays generic.
        message = err.get("message")
        message = message if isinstance(message, str) else "unknown ACP error"
        code = err.get("code")
        code = code if isinstance(code, int) else None
        return AcpClientError(
            f"{self._TOOL_LABEL} ACP {method} failed: {message}",
            error_type="rpc_error",
            code=code,
            details={"method": method},
        )
