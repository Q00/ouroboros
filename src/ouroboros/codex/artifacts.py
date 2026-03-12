"""Helpers for resolving and installing packaged Codex-native Ouroboros artifacts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import importlib.resources
from pathlib import Path
import shutil
from typing import Literal

CODEX_RULE_FILENAME = "ouroboros.md"
CODEX_SKILL_NAMESPACE = "ouroboros-"
_SKILL_ENTRYPOINT = "SKILL.md"
_RULE_NAMESPACE = Path(CODEX_RULE_FILENAME).stem
_RULE_SUFFIX = Path(CODEX_RULE_FILENAME).suffix


@dataclass(frozen=True, slots=True)
class CodexPackagedSkill:
    """A packaged self-contained Ouroboros skill ready for Codex install."""

    skill_name: str
    source_dir: Path
    install_dir_name: str

    @property
    def skill_md_path(self) -> Path:
        """Return the packaged `SKILL.md` entrypoint for this skill."""
        return self.source_dir / _SKILL_ENTRYPOINT


@dataclass(frozen=True, slots=True)
class CodexManagedArtifact:
    """A packaged Codex artifact managed by Ouroboros setup/update flows."""

    artifact_type: Literal["rule", "skill"]
    source_path: Path
    relative_install_path: Path


@dataclass(frozen=True, slots=True)
class CodexPackagedAssets:
    """Resolved packaged skills and the matching Codex rule assets."""

    skills: tuple[CodexPackagedSkill, ...]
    rules: tuple[Path, ...]

    @property
    def rules_path(self) -> Path:
        """Return the primary packaged rules file for legacy single-file consumers."""
        return _select_primary_packaged_codex_rule(self.rules)

    @property
    def managed_artifacts(self) -> tuple[CodexManagedArtifact, ...]:
        """Return the desired managed Codex artifacts derived from the packaged bundle."""
        return (
            *(
                CodexManagedArtifact(
                    artifact_type="rule",
                    source_path=rule_path,
                    relative_install_path=Path("rules") / rule_path.name,
                )
                for rule_path in self.rules
            ),
            *(
                CodexManagedArtifact(
                    artifact_type="skill",
                    source_path=skill.source_dir,
                    relative_install_path=Path("skills") / skill.install_dir_name,
                )
                for skill in self.skills
            ),
        )

    @property
    def managed_relative_install_paths(self) -> tuple[Path, ...]:
        """Return deterministic relative install paths for every managed Codex artifact."""
        return tuple(artifact.relative_install_path for artifact in self.managed_artifacts)


def _collect_packaged_codex_skills(source_root: Path) -> tuple[CodexPackagedSkill, ...]:
    """Enumerate packaged skill directories in a deterministic order."""
    skill_dirs = sorted(
        (
            source_dir
            for source_dir in source_root.iterdir()
            if source_dir.is_dir() and (source_dir / _SKILL_ENTRYPOINT).is_file()
        ),
        key=lambda source_dir: source_dir.name,
    )
    return tuple(
        CodexPackagedSkill(
            skill_name=source_dir.name,
            source_dir=source_dir,
            install_dir_name=f"{CODEX_SKILL_NAMESPACE}{source_dir.name}",
        )
        for source_dir in skill_dirs
    )


def _is_packaged_codex_rule_asset(path: Path) -> bool:
    """Return whether ``path`` is a packaged Ouroboros Codex rule asset."""
    return path.is_file() and _is_namespaced_rule_artifact(path)


def _collect_packaged_codex_rules(source_root: Path) -> tuple[Path, ...]:
    """Enumerate packaged rule assets in a deterministic order."""
    if not source_root.is_dir():
        return ()

    return tuple(
        sorted(
            (
                source_path
                for source_path in source_root.iterdir()
                if _is_packaged_codex_rule_asset(source_path)
            ),
            key=lambda source_path: (source_path.name != CODEX_RULE_FILENAME, source_path.name),
        )
    )


def _select_primary_packaged_codex_rule(rule_paths: tuple[Path, ...]) -> Path:
    """Return the primary packaged rule used by single-file consumers."""
    for rule_path in rule_paths:
        if rule_path.name == CODEX_RULE_FILENAME:
            return rule_path

    if not rule_paths:
        msg = "Packaged Ouroboros rules file could not be located"
        raise FileNotFoundError(msg)

    return rule_paths[0]


def _load_packaged_codex_skills(source_root: Path) -> tuple[CodexPackagedSkill, ...]:
    """Resolve packaged skills and fail fast when the bundle is empty."""
    skills = _collect_packaged_codex_skills(source_root)
    if skills:
        return skills

    msg = f"Packaged Ouroboros skills directory did not contain any `{_SKILL_ENTRYPOINT}` files"
    raise FileNotFoundError(msg)


@contextmanager
def _packaged_codex_skills_dir(*, skills_dir: str | Path | None = None) -> Iterator[Path]:
    """Resolve the packaged skills source directory for Codex skill installs."""
    if skills_dir is not None:
        yield Path(skills_dir).expanduser()
        return

    package_root = importlib.resources.files("ouroboros.codex")
    packaged_skills = package_root.joinpath("skills")
    if packaged_skills.is_dir():
        with importlib.resources.as_file(packaged_skills) as resolved_dir:
            yield resolved_dir
            return

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "skills"
        if candidate.is_dir():
            yield candidate
            return

    msg = "Packaged Ouroboros skills directory could not be located"
    raise FileNotFoundError(msg)


@contextmanager
def resolve_packaged_codex_skill_path(
    skill_name: str,
    *,
    skills_dir: str | Path | None = None,
) -> Iterator[Path]:
    """Resolve the packaged ``SKILL.md`` entrypoint for one Codex skill."""
    normalized_skill_name = skill_name.strip()
    if not normalized_skill_name:
        msg = "skill_name must be a non-empty string"
        raise ValueError(msg)

    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        skill_md_path = source_root / normalized_skill_name / _SKILL_ENTRYPOINT
        if not skill_md_path.is_file():
            msg = f"Packaged Ouroboros skill could not be located: {normalized_skill_name}"
            raise FileNotFoundError(msg)
        yield skill_md_path


@contextmanager
def _packaged_codex_rules(
    *,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
) -> Iterator[tuple[Path, ...]]:
    """Resolve packaged Codex rule assets."""
    if rules_path is not None and rules_dir is not None:
        msg = "Pass only one of `rules_path` or `rules_dir` when resolving Codex rules"
        raise ValueError(msg)

    if rules_path is not None:
        resolved_path = Path(rules_path).expanduser()
        if not resolved_path.is_file():
            msg = f"Packaged Ouroboros rules file could not be located: {resolved_path}"
            raise FileNotFoundError(msg)
        yield (resolved_path,)
        return

    if rules_dir is not None:
        resolved_dir = Path(rules_dir).expanduser()
        packaged_rules = _collect_packaged_codex_rules(resolved_dir)
        if packaged_rules:
            yield packaged_rules
            return

        msg = f"Packaged Ouroboros rules directory did not contain any managed rule assets: {resolved_dir}"
        raise FileNotFoundError(msg)

    package_root = importlib.resources.files("ouroboros.codex")
    with importlib.resources.as_file(package_root) as resolved_root:
        packaged_rules = _collect_packaged_codex_rules(resolved_root / "rules")
        if packaged_rules:
            yield packaged_rules
            return

        packaged_rules = _collect_packaged_codex_rules(resolved_root)
        if packaged_rules:
            yield packaged_rules
            return

    for parent in Path(__file__).resolve().parents:
        packaged_rules = _collect_packaged_codex_rules(parent / "rules")
        if packaged_rules:
            yield packaged_rules
            return

        packaged_rules = _collect_packaged_codex_rules(parent)
        if packaged_rules:
            yield packaged_rules
            return

    msg = "Packaged Ouroboros rules file could not be located"
    raise FileNotFoundError(msg)


@contextmanager
def _packaged_codex_rules_path(*, rules_path: str | Path | None = None) -> Iterator[Path]:
    """Resolve the primary packaged Codex rules markdown path."""
    with _packaged_codex_rules(rules_path=rules_path) as packaged_rules:
        yield _select_primary_packaged_codex_rule(packaged_rules)


@contextmanager
def resolve_packaged_codex_assets(
    *,
    skills_dir: str | Path | None = None,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
) -> Iterator[CodexPackagedAssets]:
    """Resolve packaged Codex skills and the matching rules for setup/update."""
    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        skills = _load_packaged_codex_skills(source_root)
        with _packaged_codex_rules(
            rules_path=rules_path,
            rules_dir=rules_dir,
        ) as packaged_rules:
            yield CodexPackagedAssets(
                skills=skills,
                rules=packaged_rules,
            )


def load_packaged_codex_rules() -> str:
    """Load the packaged Codex rules markdown."""
    with _packaged_codex_rules_path() as resolved_rules_path:
        return resolved_rules_path.read_text(encoding="utf-8")


def load_packaged_codex_skill(
    skill_name: str,
    *,
    skills_dir: str | Path | None = None,
) -> str:
    """Load the packaged ``SKILL.md`` markdown for one Codex skill."""
    with resolve_packaged_codex_skill_path(
        skill_name,
        skills_dir=skills_dir,
    ) as resolved_skill_path:
        return resolved_skill_path.read_text(encoding="utf-8")


def _remove_installed_artifact(path: Path) -> None:
    """Delete an installed Codex artifact regardless of whether it is a file or directory."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return

    path.unlink()


def _is_namespaced_rule_artifact(path: Path) -> bool:
    """Return whether a rules entry is managed by Ouroboros."""
    if path.name == CODEX_RULE_FILENAME:
        return True

    return path.name.startswith(f"{_RULE_NAMESPACE}-") and path.name.endswith(_RULE_SUFFIX)


def install_codex_rules(
    *,
    codex_dir: str | Path | None = None,
    rules_path: str | Path | None = None,
    rules_dir: str | Path | None = None,
    prune: bool = False,
) -> Path:
    """Install or refresh packaged Ouroboros rules into ``~/.codex/rules``."""
    resolved_codex_dir = (
        Path(codex_dir).expanduser() if codex_dir is not None else Path.home() / ".codex"
    )
    target_root = resolved_codex_dir / "rules"
    target_root.mkdir(parents=True, exist_ok=True)

    installed_names: set[str] = set()
    primary_target_path: Path | None = None
    with _packaged_codex_rules(
        rules_path=rules_path,
        rules_dir=rules_dir,
    ) as packaged_rules:
        primary_source_path = _select_primary_packaged_codex_rule(packaged_rules)
        for source_path in packaged_rules:
            target_path = target_root / source_path.name
            if target_path.exists():
                _remove_installed_artifact(target_path)

            shutil.copy2(source_path, target_path)
            installed_names.add(target_path.name)
            if source_path == primary_source_path:
                primary_target_path = target_path

    if prune:
        for installed_path in tuple(target_root.iterdir()):
            if installed_path.name in installed_names:
                continue
            if _is_namespaced_rule_artifact(installed_path):
                _remove_installed_artifact(installed_path)

    if primary_target_path is None:
        msg = "Packaged Ouroboros rules file could not be located"
        raise FileNotFoundError(msg)

    return primary_target_path


def install_codex_skills(
    *,
    codex_dir: str | Path | None = None,
    skills_dir: str | Path | None = None,
    prune: bool = False,
) -> tuple[Path, ...]:
    """Install or refresh packaged Ouroboros skills into ``~/.codex/skills/ouroboros-*``."""
    resolved_codex_dir = (
        Path(codex_dir).expanduser() if codex_dir is not None else Path.home() / ".codex"
    )
    target_root = resolved_codex_dir / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    installed_paths: list[Path] = []
    with _packaged_codex_skills_dir(skills_dir=skills_dir) as source_root:
        packaged_skills = _load_packaged_codex_skills(source_root)
        installed_names = {packaged_skill.install_dir_name for packaged_skill in packaged_skills}

        for packaged_skill in packaged_skills:
            target_path = target_root / packaged_skill.install_dir_name
            if target_path.exists():
                _remove_installed_artifact(target_path)

            shutil.copytree(packaged_skill.source_dir, target_path)
            installed_paths.append(target_path)

        if prune:
            for installed_path in target_root.iterdir():
                if (
                    installed_path.name.startswith(CODEX_SKILL_NAMESPACE)
                    and installed_path.name not in installed_names
                ):
                    _remove_installed_artifact(installed_path)

    return tuple(installed_paths)


__all__ = [
    "CodexManagedArtifact",
    "CodexPackagedAssets",
    "CodexPackagedSkill",
    "CODEX_RULE_FILENAME",
    "CODEX_SKILL_NAMESPACE",
    "install_codex_rules",
    "install_codex_skills",
    "load_packaged_codex_skill",
    "load_packaged_codex_rules",
    "resolve_packaged_codex_assets",
    "resolve_packaged_codex_skill_path",
]
