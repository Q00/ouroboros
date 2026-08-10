"""Confinement policy for the directories seed execution may run in.

``execute_seed`` takes a working directory from its caller and hands it to a
real agent runtime with file and shell authority. Locally that is the point --
the caller is the developer. On a shared or network-reachable server it is a
trust boundary, and the directory needs to be one the operator chose rather
than one the request did.

The policy is process-global on purpose. It describes the deployment -- which
directories this server was started to work in -- not any single request, and a
server process owns exactly one bind. Threading it through every handler
construction site would let a caller-supplied value reach the check, which is
the thing being prevented.

An empty policy permits everything, preserving the local stdio behaviour where
confinement would only get in the developer's way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import threading

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """The set of directory trees seed execution is confined to."""

    allowed_roots: tuple[Path, ...] = ()

    @classmethod
    def from_paths(cls, raw_paths: Sequence[str | Path]) -> WorkspacePolicy:
        """Build a policy from operator-supplied paths.

        Roots are resolved here so that the comparison in :meth:`permits` is
        between two fully resolved paths -- otherwise a symlink inside an
        allowed root could point anywhere and still compare as contained.

        Args:
            raw_paths: Directory paths as written by the operator.

        Returns:
            The policy. An empty input yields the permit-everything policy.
        """
        roots = tuple(Path(path).expanduser().resolve() for path in raw_paths)
        return cls(allowed_roots=roots)

    @property
    def restricted(self) -> bool:
        """Return True when this policy actually confines anything."""
        return bool(self.allowed_roots)

    def permits(self, candidate: Path) -> bool:
        """Return True when ``candidate`` lies inside an allowed root.

        Args:
            candidate: An already-resolved directory path.

        Returns:
            True when unrestricted, or when the candidate is one of the roots
            or lives beneath one.
        """
        if not self.allowed_roots:
            return True

        resolved = candidate.resolve()
        return any(resolved == root or root in resolved.parents for root in self.allowed_roots)

    def describe(self) -> str:
        """Return a human-readable summary for errors and startup output."""
        if not self.allowed_roots:
            return "unrestricted"
        return ", ".join(str(root) for root in self.allowed_roots)


_UNRESTRICTED = WorkspacePolicy()
_lock = threading.Lock()
_current_policy = _UNRESTRICTED


def set_workspace_policy(policy: WorkspacePolicy) -> None:
    """Install the policy for this server process.

    Args:
        policy: The policy to enforce for the lifetime of the process.
    """
    global _current_policy
    with _lock:
        _current_policy = policy
    log.info("mcp.workspace.policy_set", roots=policy.describe())


def get_workspace_policy() -> WorkspacePolicy:
    """Return the policy currently installed for this process."""
    with _lock:
        return _current_policy


def reset_workspace_policy() -> None:
    """Restore the permit-everything default.

    Exists so a test that installs a policy can undo it; production code sets
    the policy once at startup and never clears it.
    """
    set_workspace_policy(_UNRESTRICTED)
