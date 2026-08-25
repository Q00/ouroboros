"""Tests for GJC-native Ouroboros skill projection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ouroboros.gjc import artifacts as gjc_artifacts
from ouroboros.gjc import install_gjc_skills, remove_gjc_skills


def _skill(root: Path, name: str, *, body: str = "# Skill\n") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{name} description"\n---\n\n{body}',
        encoding="utf-8",
    )


def _frontmatter(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    closing = raw.find("\n---\n", 4)
    parsed = yaml.safe_load(raw[4:closing])
    assert isinstance(parsed, dict)
    return parsed


def test_installs_namespaced_skills_and_rewrites_cross_skill_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(
        source,
        "ooo",
        body="Read `../welcome/SKILL.md` or invoke /ouroboros:welcome.\n",
    )
    _skill(source, "welcome")

    result = install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    projected = agent_dir / "skills" / "ouroboros-ooo" / "SKILL.md"
    assert result.skill_paths == (
        agent_dir / "skills" / "ouroboros-ooo",
        agent_dir / "skills" / "ouroboros-welcome",
    )
    assert _frontmatter(projected)["name"] == "ouroboros-ooo"
    assert "bare `ooo`" in str(_frontmatter(projected)["description"])
    content = projected.read_text(encoding="utf-8")
    assert "../ouroboros-welcome/SKILL.md" in content
    assert "/skill:ouroboros-welcome" in content
    assert "/ouroboros:welcome" not in content


def test_refresh_is_idempotent_prunes_only_managed_namespace_and_preserves_user_skills(
    tmp_path: Path,
) -> None:
    old_source = tmp_path / "old-source"
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(old_source, "stale")
    _skill(source, "interview")
    user_skill = agent_dir / "skills" / "my-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("user", encoding="utf-8")
    install_gjc_skills(agent_dir=agent_dir, skills_dir=old_source)
    stale = agent_dir / "skills" / "ouroboros-stale"
    custom_namespaced = agent_dir / "skills" / "ouroboros-custom"
    custom_namespaced.mkdir()
    (custom_namespaced / "SKILL.md").write_text(
        "---\nname: ouroboros-custom\ndescription: user-owned\n---\n",
        encoding="utf-8",
    )

    first = install_gjc_skills(agent_dir=agent_dir, skills_dir=source)
    second = install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert first.skill_paths == second.skill_paths
    assert user_skill.exists()
    assert not stale.exists()
    assert custom_namespaced.exists()


def test_remove_deletes_only_intact_generated_skills(tmp_path: Path) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    managed = install_gjc_skills(agent_dir=agent_dir, skills_dir=source).skill_paths[0]
    custom_namespaced = agent_dir / "skills" / "ouroboros-custom"
    custom_namespaced.mkdir()
    (custom_namespaced / "SKILL.md").write_text(
        "---\nname: ouroboros-custom\ndescription: user-owned\n---\n",
        encoding="utf-8",
    )
    user_skill = agent_dir / "skills" / "interview"
    user_skill.mkdir()

    removed = remove_gjc_skills(agent_dir=agent_dir)

    assert removed == (managed,)
    assert not managed.exists()
    assert user_skill.exists()
    assert custom_namespaced.exists()


def test_refresh_and_remove_preserve_modified_generated_skill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    projected = install_gjc_skills(agent_dir=agent_dir, skills_dir=source).skill_paths[0]
    skill_md = projected / "SKILL.md"
    modified = skill_md.read_text(encoding="utf-8") + "\nOperator notes.\n"
    skill_md.write_text(modified, encoding="utf-8")

    with pytest.raises(OSError, match="non-Ouroboros GJC skill"):
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert remove_gjc_skills(agent_dir=agent_dir) == ()
    assert skill_md.read_text(encoding="utf-8") == modified


def test_install_refuses_to_replace_user_owned_namespaced_skill(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    collision = agent_dir / "skills" / "ouroboros-interview"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: ouroboros-interview\ndescription: user-owned\n---\n",
        encoding="utf-8",
    )

    try:
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)
    except OSError as exc:
        assert "non-Ouroboros GJC skill" in str(exc)
    else:
        raise AssertionError("expected user-owned skill collision to fail closed")


def _stale_true_for(path: Path):
    """Ownership predicate whose observation of *path* is stale (always True).

    Any other path — in particular the claimed generation, which lives under a
    different name — is judged by the real check. This deterministically
    simulates an operator replacing the artifact between the initial
    ownership check and the destructive filesystem operation.
    """
    real = gjc_artifacts._is_managed_skill

    def _check(candidate: Path) -> bool:
        if candidate == path:
            return True
        return real(candidate)

    return _check


def test_publish_preserves_operator_skill_swapped_in_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    target = agent_dir / "skills" / "ouroboros-interview"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("operator content\n", encoding="utf-8")
    monkeypatch.setattr(gjc_artifacts, "_is_managed_skill", _stale_true_for(target))

    with pytest.raises(OSError, match="non-Ouroboros GJC skill"):
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "operator content\n"


def test_remove_preserves_operator_skill_swapped_in_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / "agent"
    operator = agent_dir / "skills" / "ouroboros-custom"
    operator.mkdir(parents=True)
    (operator / "SKILL.md").write_text("operator content\n", encoding="utf-8")
    monkeypatch.setattr(gjc_artifacts, "_is_managed_skill", _stale_true_for(operator))

    removed = remove_gjc_skills(agent_dir=agent_dir)

    assert removed == ()
    assert (operator / "SKILL.md").read_text(encoding="utf-8") == "operator content\n"


def test_prune_preserves_operator_skill_swapped_in_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    stale_named = agent_dir / "skills" / "ouroboros-stale"
    stale_named.mkdir(parents=True)
    (stale_named / "SKILL.md").write_text("operator content\n", encoding="utf-8")
    monkeypatch.setattr(gjc_artifacts, "_is_managed_skill", _stale_true_for(stale_named))

    install_gjc_skills(agent_dir=agent_dir, skills_dir=source, prune=True)

    assert (stale_named / "SKILL.md").read_text(encoding="utf-8") == "operator content\n"


def test_publish_preserves_generation_recreated_while_backup_claim_was_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusal restoration must not clobber a canonical skill recreated after the claim."""
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    target = agent_dir / "skills" / "ouroboros-interview"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("first operator generation\n", encoding="utf-8")
    real = gjc_artifacts._is_managed_skill

    def stale_then_recreate(candidate: Path) -> bool:
        if candidate == target:
            return True
        if candidate.name.startswith(f".{target.name}."):
            recreated = target / "SKILL.md"
            recreated.parent.mkdir(parents=True, exist_ok=True)
            recreated.write_text("second operator generation\n", encoding="utf-8")
            return False
        return real(candidate)

    monkeypatch.setattr(gjc_artifacts, "_is_managed_skill", stale_then_recreate)

    with pytest.raises(OSError, match="non-Ouroboros GJC skill"):
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "second operator generation\n"
    preserved = sorted(target.parent.glob(f".{target.name}.*.replacing"))
    assert len(preserved) == 1
    assert (preserved[0] / "SKILL.md").read_text(encoding="utf-8") == "first operator generation\n"


def test_publish_fails_when_skill_recreated_after_backup_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final tree rename is no-replace: even an approved backup cannot
    justify overwriting a directory recreated at the canonical path."""
    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    target = agent_dir / "skills" / "ouroboros-interview"
    install_gjc_skills(agent_dir=agent_dir, skills_dir=source)
    real = gjc_artifacts._is_managed_skill

    def approve_then_recreate(candidate: Path) -> bool:
        if candidate.name.startswith(f".{target.name}."):
            target.mkdir(exist_ok=True)
            return True
        return real(candidate)

    monkeypatch.setattr(gjc_artifacts, "_is_managed_skill", approve_then_recreate)

    with pytest.raises(OSError, match="non-Ouroboros GJC skill"):
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    preserved = sorted(target.parent.glob(f".{target.name}.*.replacing"))
    assert len(preserved) == 1
    assert (preserved[0] / "SKILL.md").exists()


def test_install_rejects_a_symlinked_agent_root(tmp_path: Path) -> None:
    """A symlinked configured profile root must not redirect skill projection
    into its target."""
    source = tmp_path / "source"
    _skill(source, "interview")
    external = tmp_path / "external"
    external.mkdir()
    agent_dir = tmp_path / "agent"
    try:
        agent_dir.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(OSError, match="symlinked trusted root"):
        install_gjc_skills(agent_dir=agent_dir, skills_dir=source)

    assert not any(external.rglob("SKILL.md"))


def test_remove_recovers_an_interrupted_skill_claim(tmp_path: Path) -> None:
    """A skill stranded under a crashed transaction's claim name is restored
    and then removed like any other managed generation."""
    from ouroboros.core.fs_ownership import _claim_name

    source = tmp_path / "source"
    agent_dir = tmp_path / "agent"
    _skill(source, "interview")
    managed = install_gjc_skills(agent_dir=agent_dir, skills_dir=source).skill_paths[0]
    claim = managed.with_name(_claim_name(managed.name, "removing"))
    managed.rename(claim)  # simulate a crash between the claim and the delete
    assert not managed.exists()

    removed = remove_gjc_skills(agent_dir=agent_dir)

    assert removed == (managed,)
    assert not managed.exists()
    assert not claim.exists()
