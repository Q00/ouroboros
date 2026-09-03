"""Deterministic working-directory resolution for verification commands."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import stat

from ouroboros.core.filesystem_capability import (
    NoFollowDirectoryChain,
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)
from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.orchestrator.evidence.shell_parsing import (
    _single_command_after_safe_shell_preamble,
    _strip_env_prefix,
)

_IGNORED_MANIFEST_DIRECTORIES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "node_modules"}
)
_NODE_PACKAGE_RUNNERS = frozenset({"npm", "npx", "yarn", "pnpm"})


@dataclass(frozen=True, slots=True)
class BoundVerifyCommandCwd:
    cwd: str
    error: str | None = None
    capability: NoFollowDirectoryChain | None = None

    def close(self) -> None:
        if self.capability is not None:
            self.capability.close()


def _sole_node_manifest_directory(root: Path) -> Path | None:
    if (root / "package.json").is_file():
        return None
    candidates: list[Path] = []
    for manifest in root.rglob("package.json"):
        if (
            any(part in _IGNORED_MANIFEST_DIRECTORIES for part in manifest.relative_to(root).parts)
            or not manifest.is_file()
        ):
            continue
        candidates.append(manifest.parent)
        if len(candidates) > 1:
            return None
    return candidates[0] if candidates else None


def _verify_command_executable(command: str) -> str:
    try:
        parts = _strip_env_prefix(shlex.split(command))
    except ValueError:
        return ""
    if parts and Path(parts[0]).name in {"command", "exec"}:
        parts = parts[1:]
        if parts and parts[0] == "--":
            parts = parts[1:]
        parts = _strip_env_prefix(parts)
    inner_command = _single_command_after_safe_shell_preamble(shlex.join(parts))
    if inner_command is not None:
        try:
            parts = _strip_env_prefix(shlex.split(inner_command))
        except ValueError:
            return ""
        if parts and Path(parts[0]).name in {"command", "exec"}:
            parts = parts[1:]
            if parts and parts[0] == "--":
                parts = parts[1:]
            parts = _strip_env_prefix(parts)
    return Path(parts[0]).name if parts else ""


def _open_bound_directory(
    root: Path,
    target: Path,
    *,
    source: str,
) -> tuple[NoFollowDirectoryChain | None, str | None]:
    if not nofollow_directory_capabilities_available():
        return None, "secure verify_cwd directory binding is unavailable on this platform"
    try:
        relative = target.relative_to(root)
    except ValueError:
        return None, f"{source} escapes the workspace"
    if relative.parts == ():
        return None, None
    chain: NoFollowDirectoryChain | None = None
    try:
        chain = open_nofollow_directory_chain(str(root), relative_components=relative.parts)
        if source == "inferred verify_cwd":
            package_json_stat = os.stat(
                "package.json",
                dir_fd=chain.leaf_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(package_json_stat.st_mode):
                chain.close()
                return None, "inferred verify_cwd package.json is not a regular file"
        if not chain.postvalidate():
            chain.close()
            return None, f"{source} changed during secure directory binding"
        return chain, None
    except FileNotFoundError as exc:
        return None, f"{source} does not exist in the workspace: {exc}"
    except OSError as exc:
        if chain is not None:
            chain.close()
        return None, f"{source} could not be securely bound: {exc}"


def resolve_verify_command_cwd(
    root_cwd: str, spec: AcceptanceCriterionSpec
) -> tuple[str, str | None]:
    """Resolve explicit verify_cwd or the sole nested Node manifest directory."""
    root = Path(root_cwd).expanduser().resolve(strict=False)
    if spec.verify_cwd:
        target = (root / spec.verify_cwd).resolve(strict=False)
        if not target.is_relative_to(root):
            return root_cwd, f"verify_cwd escapes the workspace: {spec.verify_cwd!r}"
        if not target.is_dir():
            return root_cwd, f"verify_cwd does not exist in the workspace: {spec.verify_cwd!r}"
        return str(target), None
    if _verify_command_executable(spec.verify_command or "") in _NODE_PACKAGE_RUNNERS:
        try:
            manifest_dir = _sole_node_manifest_directory(root)
        except OSError:
            manifest_dir = None
        if manifest_dir is not None:
            return str(manifest_dir), None
    return root_cwd, None


def bind_verify_command_cwd(root_cwd: str, spec: AcceptanceCriterionSpec) -> BoundVerifyCommandCwd:
    resolved_cwd, error = resolve_verify_command_cwd(root_cwd, spec)
    if error is not None:
        return BoundVerifyCommandCwd(cwd=root_cwd, error=error)
    if resolved_cwd == root_cwd:
        return BoundVerifyCommandCwd(cwd=root_cwd)
    root = Path(root_cwd).expanduser().resolve(strict=False)
    if spec.verify_cwd:
        target = root.joinpath(*Path(spec.verify_cwd).parts)
    else:
        target = Path(resolved_cwd).expanduser()
    if target == root:
        return BoundVerifyCommandCwd(cwd=root_cwd)
    source = "explicit verify_cwd" if spec.verify_cwd else "inferred verify_cwd"
    capability, bind_error = _open_bound_directory(root, target, source=source)
    if bind_error is not None:
        return BoundVerifyCommandCwd(cwd=root_cwd, error=bind_error)
    return BoundVerifyCommandCwd(cwd=str(target), capability=capability)
