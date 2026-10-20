"""Release-bound Codex MCP launchers and setup-owned package pins."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import metadata
import os
from pathlib import Path
import re
import subprocess
from typing import Any

_RELEASE_PACKAGE_PIN = re.compile(
    r"^ouroboros-ai\[mcp\]==[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?$"
)


def release_codex_mcp_launcher(
    *,
    which: Callable[[str], str | None],
    run: Callable[..., Any],
    executable: str,
    find_spec: Callable[[str], Any],
    uvx_args: Sequence[str],
    direct_args: Sequence[str],
    module_args: Sequence[str],
) -> tuple[str, list[str]] | None:
    """Pin uvx to this installation; retain direct current-environment fallbacks."""
    uvx_path = which("uvx")
    if uvx_path:
        try:
            version = metadata.version("ouroboros-ai")
        except metadata.PackageNotFoundError:
            version = None
        package = f"ouroboros-ai[mcp]=={version}"
        if _RELEASE_PACKAGE_PIN.fullmatch(package):
            return uvx_path, [package if arg == "ouroboros-ai[mcp]" else arg for arg in uvx_args]

    ouroboros_path = which("ouroboros")
    if ouroboros_path:
        try:
            result = run(
                [ouroboros_path, "mcp", "serve", "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            result = None
        if result is not None and result.returncode == 0:
            return ouroboros_path, list(direct_args)

    if Path(executable).is_file() and os.access(executable, os.X_OK) and find_spec("mcp"):
        return executable, list(module_args)
    return None


def is_setup_owned_codex_uvx_args(
    args: Sequence[str],
    legacy_args: Sequence[tuple[str, ...]],
    *,
    has_managed_comment: bool,
) -> bool:
    """Accept historical launchers and marked canonical exact release pins."""
    values = tuple(args)
    if values in legacy_args:
        return True
    if not has_managed_comment:
        return False
    normalized = tuple(
        "ouroboros-ai[mcp]" if _RELEASE_PACKAGE_PIN.fullmatch(arg) else arg for arg in values
    )
    return normalized in legacy_args
