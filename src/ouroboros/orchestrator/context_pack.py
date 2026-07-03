"""Deterministic repo context pack for priming run workers (brownfield).

Run workers start blind: the worker system prompt carries strategy, seed
contract, AC tracking, and recovery protocol, but no repo map, no
build/test commands, and no conventions. This module produces a small,
**deterministic** context pack — parsed facts only, never an LLM call and
never LLM-generated advice — that the runner appends to the worker system
prompt so the worker knows the project's stack, real verify commands, and
top-level layout before it touches a file.

Design constraints (non-negotiable):

* Deterministic facts only: manifest-parsed tech stack + versions,
  ``.ouroboros/mechanical.toml`` verify commands (reused from the evaluation
  detector), and a compact directory tree. No prose, no LLM-generated
  guidance (ETH Zurich 2026 evidence: LLM-authored context files reduce
  success and add cost).
* Best-effort: any scanner failure yields ``None`` (an empty pack). The
  scanner must never raise into a run or block it.
* Bounded: the rendered pack is capped at :data:`MAX_PACK_CHARS`; the repo
  map is truncated first when over budget.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tomllib
from typing import Any

from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# Hard budget on the rendered pack (~1.5k tokens). The repo map is trimmed
# first when the pack would exceed this.
MAX_PACK_CHARS = 6000

_CACHE_RELATIVE_PATH = Path(".ouroboros") / "context_pack.json"

# Working-tree files the scanner parses. The cache key must cover these in
# addition to git HEAD: HEAD only moves on commit, while the pack is built
# from mutable working-tree state — editing pyproject.toml (or the detector's
# mechanical.toml) without committing must invalidate the cache.
_FINGERPRINT_SOURCES: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    ".ouroboros/mechanical.toml",
)

# Sentinel recorded for a fingerprint source that does not exist, so a file
# appearing or disappearing also changes the fingerprint.
_FINGERPRINT_ABSENT = "absent"

# Directories that never carry useful layout signal and would bloat the map.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        ".next",
        ".turbo",
        ".cache",
        "coverage",
        ".gradle",
        ".ouroboros",
        "site-packages",
    }
)

# Well-known entry-point / manifest files worth surfacing in the layout.
_ENTRY_POINT_FILES: frozenset[str] = frozenset(
    {
        "main.py",
        "__main__.py",
        "app.py",
        "cli.py",
        "manage.py",
        "index.js",
        "index.ts",
        "main.js",
        "main.ts",
        "server.js",
        "server.ts",
        "main.go",
        "main.rs",
        "Makefile",
        "justfile",
        "Dockerfile",
        "pyproject.toml",
        "setup.py",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "README.md",
    }
)

# Ceilings so a wide monorepo cannot explode the map before budget trimming.
_MAX_TOP_LEVEL_ENTRIES = 40
_MAX_CHILDREN_PER_DIR = 12


@dataclass(frozen=True)
class ContextPack:
    """Deterministic, parsed facts about a repository.

    Attributes:
        stack: Rendered tech-stack lines (e.g. ``"Python (pyproject): ouroboros 0.44.0"``).
        verify_commands: Rendered ``kind: command`` verify lines from
            ``.ouroboros/mechanical.toml`` (e.g. ``"test: uv run pytest"``).
        repo_map: Compact top-2-level directory tree lines.
        head: The git ``HEAD`` SHA the pack was built against, or ``None`` for
            a non-git directory (in which case no cache is written).
    """

    stack: tuple[str, ...]
    verify_commands: tuple[str, ...]
    repo_map: tuple[str, ...]
    head: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "stack": list(self.stack),
            "verify_commands": list(self.verify_commands),
            "repo_map": list(self.repo_map),
        }

    @classmethod
    def from_json(cls, data: Any) -> ContextPack | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                stack=tuple(str(x) for x in data.get("stack", [])),
                verify_commands=tuple(str(x) for x in data.get("verify_commands", [])),
                repo_map=tuple(str(x) for x in data.get("repo_map", [])),
                head=data.get("head") if isinstance(data.get("head"), str) else None,
            )
        except (TypeError, ValueError):
            return None


def build_context_pack(repo_root: Path | str) -> ContextPack | None:
    """Return a deterministic context pack for ``repo_root``, or ``None``.

    Best-effort and side-effect-tolerant: reads a sidecar cache at
    ``.ouroboros/context_pack.json`` keyed by git ``HEAD`` **plus** a
    fingerprint of every working-tree file the scanner parses (HEAD alone is
    insufficient — the sources are mutable without a commit), otherwise scans
    and (for git dirs) refreshes the cache. Any failure yields ``None`` so a
    caller can safely fall through to a pack-free prompt.
    """
    try:
        root = Path(repo_root)
        if not root.is_dir():
            return None
        head = _git_head(root)
        fingerprint = _source_fingerprint(root)
        cached = _read_cache(root, head, fingerprint)
        if cached is not None:
            return cached
        pack = _scan(root, head)
        if pack is None:
            return None
        if head is not None:
            _write_cache(root, pack, fingerprint)
        return pack
    except Exception as exc:  # never fail a run on a scanner defect
        log.warning("context_pack.build_failed", repo_root=str(repo_root), error=str(exc))
        return None


def detected_verify_commands(repo_root: Path | str) -> tuple[str, ...]:
    """Return rendered ``kind: command`` verify lines for ``repo_root``.

    Reuses the evaluation detector's deterministic
    ``.ouroboros/mechanical.toml`` resolution. Empty when no toml exists or no
    command is configured. Best-effort: any failure yields an empty tuple.
    """
    try:
        root = Path(repo_root)
        if not root.is_dir():
            return ()
        from ouroboros.evaluation.detector import has_mechanical_toml

        if not has_mechanical_toml(root):
            return ()
        from ouroboros.evaluation.languages import build_mechanical_config

        config = build_mechanical_config(root)
        lines: list[str] = []
        for kind in ("test", "lint", "build", "static", "coverage"):
            command = getattr(config, f"{kind}_command", None)
            if command:
                lines.append(f"{kind}: {' '.join(command)}")
        return tuple(lines)
    except Exception as exc:
        log.warning("context_pack.verify_commands_failed", repo_root=str(repo_root), error=str(exc))
        return ()


def render_context_pack(pack: ContextPack) -> str:
    """Render a fenced ``## Project Context (auto-detected facts)`` block.

    Enforces :data:`MAX_PACK_CHARS`, trimming the repo map first when over.
    """
    repo_map = list(pack.repo_map)
    while True:
        rendered = _render(pack.stack, pack.verify_commands, tuple(repo_map))
        if len(rendered) <= MAX_PACK_CHARS or not repo_map:
            return rendered
        # Drop the last map line and retry; the stack/verify facts are the
        # highest-value signal and are never trimmed.
        repo_map.pop()


def _render(
    stack: tuple[str, ...],
    verify_commands: tuple[str, ...],
    repo_map: tuple[str, ...],
) -> str:
    parts: list[str] = ["## Project Context (auto-detected facts)"]
    if stack:
        parts.append("Stack:\n" + "\n".join(f"- {line}" for line in stack))
    if verify_commands:
        parts.append("Verify commands:\n" + "\n".join(f"- {line}" for line in verify_commands))
    if repo_map:
        parts.append("Layout:\n```\n" + "\n".join(repo_map) + "\n```")
    return "\n\n".join(parts)


# =============================================================================
# Scanning
# =============================================================================


def _scan(root: Path, head: str | None) -> ContextPack | None:
    stack = _detect_stack(root)
    verify_commands = detected_verify_commands(root)
    repo_map = _build_repo_map(root)
    # Deterministic facts only: with neither a parsed stack nor verify commands
    # there is nothing worth priming a worker with, so the pack stays absent
    # (this is also what keeps empty/non-project dirs from producing a pack).
    if not stack and not verify_commands:
        return None
    return ContextPack(
        stack=stack,
        verify_commands=verify_commands,
        repo_map=repo_map,
        head=head,
    )


def _detect_stack(root: Path) -> tuple[str, ...]:
    """Parse manifests for tech stack + versions. Parsing only, no execution."""
    lines: list[str] = []
    lines.extend(_python_stack(root))
    lines.extend(_node_stack(root))
    lines.extend(_go_stack(root))
    lines.extend(_rust_stack(root))
    return tuple(lines)


def _python_stack(root: Path) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return []
    project = data.get("project") if isinstance(data, dict) else None
    name = ""
    version = ""
    requires_python = ""
    if isinstance(project, dict):
        name = str(project.get("name") or "").strip()
        version = str(project.get("version") or "").strip()
        requires_python = str(project.get("requires-python") or "").strip()
    label = "Python (pyproject)"
    detail = " ".join(part for part in (name, version) if part) or "project"
    line = f"{label}: {detail}"
    if requires_python:
        line += f" (requires-python {requires_python})"
    return [line]


def _node_stack(root: Path) -> list[str]:
    path = root / "package.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    name = str(data.get("name") or "").strip()
    version = str(data.get("version") or "").strip()
    detail = " ".join(part for part in (name, version) if part) or "project"
    engines = data.get("engines")
    line = f"Node (package.json): {detail}"
    if isinstance(engines, dict) and engines.get("node"):
        line += f" (node {str(engines['node']).strip()})"
    return [line]


def _go_stack(root: Path) -> list[str]:
    path = root / "go.mod"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    module = ""
    go_version = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("module ") and not module:
            module = line[len("module ") :].strip()
        elif line.startswith("go ") and not go_version:
            go_version = line[len("go ") :].strip()
    detail = module or "module"
    result = f"Go (go.mod): {detail}"
    if go_version:
        result += f" (go {go_version})"
    return [result]


def _rust_stack(root: Path) -> list[str]:
    path = root / "Cargo.toml"
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return []
    package = data.get("package") if isinstance(data, dict) else None
    name = ""
    version = ""
    edition = ""
    if isinstance(package, dict):
        name = str(package.get("name") or "").strip()
        version = str(package.get("version") or "").strip()
        edition = str(package.get("edition") or "").strip()
    detail = " ".join(part for part in (name, version) if part) or "crate"
    line = f"Rust (Cargo.toml): {detail}"
    if edition:
        line += f" (edition {edition})"
    return [line]


def _build_repo_map(root: Path) -> tuple[str, ...]:
    """Build a compact top-2-level directory tree + entry-point files."""
    try:
        children = sorted(
            (c for c in root.iterdir() if not _skip_entry(c)),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
    except OSError:
        return ()

    lines: list[str] = [f"{root.name}/"]
    for count, child in enumerate(children):
        if count >= _MAX_TOP_LEVEL_ENTRIES:
            lines.append("  …")
            break
        if child.is_dir():
            lines.append(f"  {child.name}/")
            lines.extend(_render_subdir(child))
        elif child.name in _ENTRY_POINT_FILES:
            lines.append(f"  {child.name}")
    return tuple(lines)


def _render_subdir(directory: Path) -> list[str]:
    try:
        grandchildren = sorted(
            (c for c in directory.iterdir() if not _skip_entry(c)),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
    except OSError:
        return []
    out: list[str] = []
    shown = 0
    for gc in grandchildren:
        if shown >= _MAX_CHILDREN_PER_DIR:
            out.append("    …")
            break
        if gc.is_dir():
            out.append(f"    {gc.name}/")
            shown += 1
        elif gc.name in _ENTRY_POINT_FILES:
            out.append(f"    {gc.name}")
            shown += 1
    return out


def _skip_entry(path: Path) -> bool:
    name = path.name
    if name in _SKIP_DIRS:
        return True
    # Hidden dirs (dotfiles) other than surfaced entry points add noise.
    return name.startswith(".") and path.is_dir()


# =============================================================================
# Cache + git
# =============================================================================


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _source_fingerprint(root: Path) -> dict[str, Any]:
    """Return a deterministic signature of the scanner's working-tree sources.

    Each source maps to ``[mtime_ns, size]`` (or :data:`_FINGERPRINT_ABSENT`
    when missing), so any edit, creation, or deletion of a parsed file changes
    the fingerprint even while git ``HEAD`` stays fixed.
    """
    fingerprint: dict[str, Any] = {}
    for rel in _FINGERPRINT_SOURCES:
        try:
            stat = (root / rel).stat()
        except OSError:
            fingerprint[rel] = _FINGERPRINT_ABSENT
            continue
        fingerprint[rel] = [stat.st_mtime_ns, stat.st_size]
    return fingerprint


def _read_cache(
    root: Path,
    head: str | None,
    fingerprint: dict[str, Any],
) -> ContextPack | None:
    if head is None:
        return None
    cache_path = root / _CACHE_RELATIVE_PATH
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    pack = ContextPack.from_json(data)
    if pack is None or pack.head != head:
        return None
    return pack


def _write_cache(root: Path, pack: ContextPack, fingerprint: dict[str, Any]) -> None:
    cache_path = root / _CACHE_RELATIVE_PATH
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {**pack.to_json(), "fingerprint": fingerprint},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("context_pack.cache_write_failed", path=str(cache_path), error=str(exc))
