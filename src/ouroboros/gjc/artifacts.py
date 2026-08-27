"""Install packaged Ouroboros skills into GJC's native skill registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil

import yaml

from ouroboros.core.fs_ownership import (
    UnownedArtifactError,
    claim_and_remove_owned,
    find_orphaned_claims,
    publish_owned_tree,
    recover_owned_claims,
)
from ouroboros.skills.artifacts import collect_skill_bundle_dirs, resolve_packaged_skills_dir

GJC_SKILL_NAMESPACE = "ouroboros-"
_SKILL_REFERENCE_PATTERN = re.compile(r"\.\./([a-z0-9_-]+)/SKILL\.md")
_SLASH_SKILL_PATTERN = re.compile(r"/ouroboros:([a-z0-9_-]+)")
_MANAGED_FIELD = "ouroboros_projection"
_MANAGED_VALUE = "gjc-v1"
_MANAGED_DIGEST_FIELD = "ouroboros_projection_sha256"
_DIGEST_PLACEHOLDER = "sha256-" + "x" * 64


@dataclass(frozen=True, slots=True)
class GjcSkillInstallResult:
    """Installed GJC skill projection paths."""

    target_root: Path
    skill_paths: tuple[Path, ...]


def gjc_skills_root(agent_dir: str | Path) -> Path:
    """Return the native GJC user-skill directory beneath an agent profile."""
    return Path(agent_dir).expanduser() / "skills"


def _split_skill_document(source: str, source_path: Path) -> tuple[dict[str, object], str]:
    if not source.startswith("---\n"):
        raise ValueError(f"Skill lacks YAML frontmatter: {source_path}")
    closing = source.find("\n---\n", 4)
    if closing == -1:
        raise ValueError(f"Skill frontmatter is not closed: {source_path}")
    parsed = yaml.safe_load(source[4:closing]) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"Skill frontmatter is not a mapping: {source_path}")
    return parsed, source[closing + 5 :]


def _gjc_mcp_wire_name(mcp_tool: object) -> str | None:
    """Map an Ouroboros MCP tool id to GJC's deterministic MCP wire name."""
    if not isinstance(mcp_tool, str):
        return None
    normalized = mcp_tool.strip()
    if not normalized.startswith("ouroboros_"):
        return None
    return f"mcp__ouroboros_{normalized.removeprefix('ouroboros_')}"


def _render_gjc_skill(source_dir: Path) -> str:
    source_path = source_dir / "SKILL.md"
    frontmatter, body = _split_skill_document(source_path.read_text(encoding="utf-8"), source_path)
    command = source_dir.name
    projected_name = f"{GJC_SKILL_NAMESPACE}{command}"
    raw_description = frontmatter.get("description")
    description = str(raw_description).strip() if raw_description is not None else ""
    trigger = (
        "Use when the user sends bare `ooo`."
        if command == "ooo"
        else f"Use when the user explicitly invokes `ooo {command}`."
    )
    frontmatter["name"] = projected_name
    frontmatter[_MANAGED_FIELD] = _MANAGED_VALUE
    frontmatter[_MANAGED_DIGEST_FIELD] = _DIGEST_PLACEHOLDER
    frontmatter["description"] = (
        f"{description.rstrip('.')} — {trigger}" if description else trigger
    )
    wire_name = _gjc_mcp_wire_name(frontmatter.get("mcp_tool"))
    if wire_name is not None:
        body = (
            "## GJC runtime dispatch\n\n"
            f"The Ouroboros MCP tool for this skill is `{wire_name}`. In GJC it is "
            "autoloaded and may already be active even when a tool-discovery search returns "
            "zero matches. Skip deferred tool discovery, `gjc tools` shell probes, and "
            "repository searches. At the skill body's first MCP step, call this exact tool "
            "directly with the documented arguments. Use the non-MCP fallback only if that "
            "direct call itself reports that the tool is unavailable.\n\n" + body
        )
    projected_body = _SKILL_REFERENCE_PATTERN.sub(
        lambda match: f"../{GJC_SKILL_NAMESPACE}{match.group(1)}/SKILL.md",
        body,
    )
    projected_body = _SLASH_SKILL_PATTERN.sub(
        lambda match: (
            f"/skill:{match.group(1)}"
            if match.group(1).startswith(GJC_SKILL_NAMESPACE)
            else f"/skill:{GJC_SKILL_NAMESPACE}{match.group(1)}"
        ),
        projected_body,
    )
    rendered_frontmatter = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{rendered_frontmatter}\n---\n{projected_body}"


def _normalized_skill_bytes(source: str, expected_digest: str) -> bytes | None:
    pattern = re.compile(
        rf"(?m)^{re.escape(_MANAGED_DIGEST_FIELD)}: (sha256-(?:[0-9a-f]{{64}}|x{{64}}))$"
    )
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1 or matches[0].group(1) != expected_digest:
        return None
    return pattern.sub(f"{_MANAGED_DIGEST_FIELD}: {_DIGEST_PLACEHOLDER}", source, count=1).encode(
        "utf-8"
    )


def _skill_tree_digest(path: Path, *, expected_digest: str) -> str | None:
    digest = hashlib.sha256()
    try:
        candidates = sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix())
        for candidate in candidates:
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            if candidate.is_symlink():
                return None
            if candidate.is_dir():
                digest.update(b"directory\0" + relative + b"\0")
                continue
            if not candidate.is_file():
                return None
            content = candidate.read_bytes()
            if relative == b"SKILL.md":
                normalized = _normalized_skill_bytes(content.decode("utf-8"), expected_digest)
                if normalized is None:
                    return None
                content = normalized
            digest.update(b"file\0" + relative + b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, UnicodeDecodeError):
        return None
    return f"sha256-{digest.hexdigest()}"


def _is_managed_skill(path: Path) -> bool:
    try:
        source = (path / "SKILL.md").read_text(encoding="utf-8")
        frontmatter, _ = _split_skill_document(source, path / "SKILL.md")
    except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError):
        return False
    expected_digest = frontmatter.get(_MANAGED_DIGEST_FIELD)
    return (
        frontmatter.get(_MANAGED_FIELD) == _MANAGED_VALUE
        and isinstance(expected_digest, str)
        and _skill_tree_digest(path, expected_digest=expected_digest) == expected_digest
    )


def _publish_skill(source_dir: Path, target_path: Path, agent_root: Path) -> None:
    if target_path.is_symlink():
        raise OSError(f"Refusing to replace symlinked GJC skill: {target_path}")
    if target_path.exists() and not _is_managed_skill(target_path):
        raise OSError(f"Refusing to replace non-Ouroboros GJC skill: {target_path}")

    def _build(staging: Path) -> None:
        shutil.copytree(source_dir, staging, dirs_exist_ok=True, symlinks=False)
        rendered = _render_gjc_skill(source_dir)
        (staging / "SKILL.md").write_text(rendered, encoding="utf-8")
        generation_digest = _skill_tree_digest(staging, expected_digest=_DIGEST_PLACEHOLDER)
        if generation_digest is None:
            raise OSError(f"Could not fingerprint projected GJC skill: {target_path}")
        (staging / "SKILL.md").write_text(
            rendered.replace(
                f"{_MANAGED_DIGEST_FIELD}: {_DIGEST_PLACEHOLDER}",
                f"{_MANAGED_DIGEST_FIELD}: {generation_digest}",
                1,
            ),
            encoding="utf-8",
        )

    try:
        publish_owned_tree(
            target_path,
            _build,
            is_owned=_is_managed_skill,
            trusted_ancestor=agent_root,
        )
    except UnownedArtifactError as exc:
        raise OSError(f"Refusing to replace non-Ouroboros GJC skill: {target_path}") from exc


@contextmanager
def _packaged_skills(skills_dir: str | Path | None = None) -> Iterator[Path]:
    with resolve_packaged_skills_dir(skills_dir=skills_dir, anchor_file=__file__) as source_root:
        yield source_root


def install_gjc_skills(
    *,
    agent_dir: str | Path,
    skills_dir: str | Path | None = None,
    prune: bool = True,
) -> GjcSkillInstallResult:
    """Install or refresh namespaced Ouroboros skills for one GJC profile."""
    # The skills/ parent is created by publish_owned_tree's pinned no-follow
    # walk from the trusted agent root — never by a pathname mkdir here, so a
    # symlinked or hostile root produces no side effects before validation.
    target_root = gjc_skills_root(agent_dir)
    agent_root = Path(agent_dir).expanduser()
    installed: list[Path] = []
    with _packaged_skills(skills_dir) as source_root:
        source_dirs = collect_skill_bundle_dirs(source_root)
        if not source_dirs:
            raise FileNotFoundError("Packaged Ouroboros skills directory is empty")
        expected_names = {f"{GJC_SKILL_NAMESPACE}{source_dir.name}" for source_dir in source_dirs}
        for source_dir in source_dirs:
            target_path = target_root / f"{GJC_SKILL_NAMESPACE}{source_dir.name}"
            _publish_skill(source_dir, target_path, agent_root)
            installed.append(target_path)

    if prune:
        for candidate in target_root.iterdir():
            if (
                candidate.name.startswith(GJC_SKILL_NAMESPACE)
                and candidate.name not in expected_names
                and not candidate.is_symlink()
            ):
                claim_and_remove_owned(
                    candidate, is_owned=_is_managed_skill, trusted_ancestor=agent_root
                )
    return GjcSkillInstallResult(target_root=target_root, skill_paths=tuple(installed))


def has_setup_owned_gjc_skills(*, agent_dir: str | Path) -> bool:
    """Return whether a GJC profile contains an intact setup-generated skill."""
    target_root = gjc_skills_root(agent_dir)
    return (
        target_root.is_dir()
        and not target_root.is_symlink()
        and any(
            candidate.name.startswith(GJC_SKILL_NAMESPACE)
            and not candidate.is_symlink()
            and _is_managed_skill(candidate)
            for candidate in target_root.iterdir()
        )
    )


def has_orphaned_gjc_claims() -> bool:
    """Return whether an *authenticated* interrupted GJC claim exists.

    Claim-name syntax is discovery metadata, not ownership evidence: only a
    claim sibling of a known GJC artifact path whose content passes that
    artifact's exact ownership predicate counts as installed state. A forged
    or unrelated claim-shaped file must never cause refresh to activate a
    runtime that setup did not previously configure.
    """
    from ouroboros.core.fs_ownership import find_orphaned_claims, has_recoverable_claim
    from ouroboros.gjc.bridge import is_setup_managed_gjc_bridge
    from ouroboros.gjc.guide import is_setup_managed_gjc_instruction
    from ouroboros.gjc.mcp import is_setup_managed_gjc_mcp_bridge_config
    from ouroboros.gjc.paths import (
        gjc_agent_dir,
        gjc_bridge_path,
        gjc_instruction_path,
        gjc_mcp_bridge_config_path,
    )

    agent_dir = gjc_agent_dir()
    known_artifacts: tuple[tuple[Path, Callable[[Path], bool]], ...] = (
        (gjc_instruction_path(), is_setup_managed_gjc_instruction),
        (gjc_mcp_bridge_config_path(), is_setup_managed_gjc_mcp_bridge_config),
        (gjc_bridge_path(), is_setup_managed_gjc_bridge),
    )
    if any(
        has_recoverable_claim(artifact, is_owned=judge, trusted_ancestor=agent_dir)
        for artifact, judge in known_artifacts
    ):
        return True
    skills_root = gjc_skills_root(agent_dir)
    if not skills_root.is_dir() or skills_root.is_symlink():
        return False
    return any(
        name.startswith(GJC_SKILL_NAMESPACE)
        and has_recoverable_claim(
            skills_root / name, is_owned=_is_managed_skill, trusted_ancestor=agent_dir
        )
        for name in find_orphaned_claims(skills_root)
    )


def recover_gjc_skill_claims(*, agent_dir: str | Path) -> bool:
    """Reconcile interrupted skill-claim state left by a crashed transaction."""
    target_root = gjc_skills_root(agent_dir)
    if not target_root.is_dir() or target_root.is_symlink():
        return False
    agent_root = Path(agent_dir).expanduser()
    changed = False
    for name in find_orphaned_claims(target_root):
        if not name.startswith(GJC_SKILL_NAMESPACE):
            continue
        with suppress(OSError):
            changed = (
                recover_owned_claims(
                    target_root / name, is_owned=_is_managed_skill, trusted_ancestor=agent_root
                )
                or changed
            )
    return changed


def remove_gjc_skills(*, agent_dir: str | Path, dry_run: bool = False) -> tuple[Path, ...]:
    """Remove only the namespaced skill projections managed by Ouroboros."""
    target_root = gjc_skills_root(agent_dir)
    if not target_root.is_dir() or target_root.is_symlink():
        return ()
    recover_gjc_skill_claims(agent_dir=agent_dir)
    targets = tuple(
        candidate
        for candidate in sorted(target_root.iterdir(), key=lambda path: path.name)
        if candidate.name.startswith(GJC_SKILL_NAMESPACE)
        and not candidate.is_symlink()
        and _is_managed_skill(candidate)
    )
    if dry_run:
        return targets
    agent_root = Path(agent_dir).expanduser()
    return tuple(
        target
        for target in targets
        if claim_and_remove_owned(target, is_owned=_is_managed_skill, trusted_ancestor=agent_root)
    )


__all__ = [
    "GJC_SKILL_NAMESPACE",
    "GjcSkillInstallResult",
    "gjc_skills_root",
    "has_orphaned_gjc_claims",
    "has_setup_owned_gjc_skills",
    "install_gjc_skills",
    "recover_gjc_skill_claims",
    "remove_gjc_skills",
]
