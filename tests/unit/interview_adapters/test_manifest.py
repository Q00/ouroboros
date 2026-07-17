from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.interview_adapters.manifest import (
    GlossaryManifest,
    ManifestError,
    load_builtin_manifest,
    load_manifest_resource,
)
from ouroboros.interview_adapters.registry import BuiltinGlossaryRegistry


def _manifest(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "custom",
        "domain": "test",
        "schema_version": 1,
        "glossary_terms": [
            {
                "term": "affordance",
                "explanation": "A clue that suggests what action is possible.",
                "aliases": ["affordances"],
            }
        ],
        "disambiguation_prompts": ["Explain only explicit confusion."],
        "applies_when": ["The user asks what a term means."],
        "does_not_apply_when": ["The user asks for a product decision."],
    }
    data.update(overrides)
    return data


def test_manifest_requires_approved_top_level_fields() -> None:
    data = _manifest()
    del data["applies_when"]

    with pytest.raises(ValueError):
        GlossaryManifest.model_validate(data)


def test_manifest_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unapproved keys"):
        GlossaryManifest.model_validate(_manifest(notes="not allowed"))


@pytest.mark.parametrize(
    "key",
    ["requirements", "default_requirements", "acceptance_criteria", "ac_defaults"],
)
def test_manifest_rejects_requirement_default_and_ac_like_keys(key: str) -> None:
    with pytest.raises(ValueError, match="unapproved keys"):
        GlossaryManifest.model_validate(_manifest(**{key: ["bad"]}))


def test_terms_reject_extra_requirement_like_keys() -> None:
    data = _manifest(
        glossary_terms=[
            {
                "term": "affordance",
                "explanation": "A clue that suggests what action is possible.",
                "requirement": "must use affordances",
            }
        ]
    )

    with pytest.raises(ValueError):
        GlossaryManifest.model_validate(data)


def test_duplicate_terms_and_aliases_fail() -> None:
    data = _manifest(
        glossary_terms=[
            {"term": "affordance", "explanation": "One.", "aliases": []},
            {"term": "Affordance", "explanation": "Two.", "aliases": []},
        ]
    )

    with pytest.raises(ValueError, match="unique"):
        GlossaryManifest.model_validate(data)


def test_pack_name_path_traversal_is_rejected() -> None:
    with pytest.raises(ManifestError, match="invalid built-in pack name"):
        load_builtin_manifest("../ui_ux_basics")
    with pytest.raises(ManifestError, match="invalid resource name"):
        load_manifest_resource("ouroboros.interview_adapters.packs", "../ui_ux_basics.yaml")


def test_ui_ux_basics_loads_from_builtin_registry() -> None:
    registry = BuiltinGlossaryRegistry.load()

    manifest = registry.get("ui_ux_basics")

    assert manifest.name == "ui_ux_basics"
    assert {term.term for term in manifest.glossary_terms} >= {"affordance", "hierarchy"}


def test_loader_uses_packaged_resources_not_source_tree_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_path_reads(*args: object, **kwargs: object) -> None:
        raise AssertionError("source-tree Path.read_text must not be used")

    monkeypatch.setattr(Path, "read_text", fail_path_reads)

    manifest = load_manifest_resource("ouroboros.interview_adapters.packs", "ui_ux_basics.yaml")

    assert manifest.name == "ui_ux_basics"


def test_duplicate_pack_names_fail_deterministically() -> None:
    with pytest.raises(ManifestError, match="duplicate glossary pack name"):
        BuiltinGlossaryRegistry.load(names=("ui_ux_basics", "ui_ux_basics"))
