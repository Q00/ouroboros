"""Strict glossary manifest schema and packaged-resource YAML loader."""

from __future__ import annotations

from importlib import resources
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
import yaml

_APPROVED_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "domain",
        "schema_version",
        "glossary_terms",
        "disambiguation_prompts",
        "applies_when",
        "does_not_apply_when",
    }
)
_RESERVED_KEY_PATTERN = re.compile(
    r"(requirement|requirements|acceptance|criteria|criterion|\bac\b|default|defaults)",
    re.IGNORECASE,
)
_PACK_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class ManifestError(ValueError):
    """Raised when a glossary manifest cannot be loaded or validated."""


class _StrictFrozenModel(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")


class GlossaryTerm(_StrictFrozenModel):
    """One glossary term. It must not contain requirement-like fields."""

    term: str = Field(min_length=1, max_length=80)
    explanation: str = Field(min_length=1, max_length=600)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("term", "explanation")
    @classmethod
    def _strip_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for alias in value:
            alias = alias.strip()
            if not alias:
                raise ValueError("aliases must not contain blank strings")
            if len(alias) > 80:
                raise ValueError("aliases must be at most 80 chars")
            key = alias.casefold()
            if key in seen:
                raise ValueError("aliases must not contain duplicates")
            seen.add(key)
            normalized.append(alias)
        return tuple(normalized)


class GlossaryManifest(_StrictFrozenModel):
    """Approved v1 glossary manifest shape."""

    name: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=120)
    schema_version: Literal[1]
    glossary_terms: tuple[GlossaryTerm, ...] = Field(min_length=1, max_length=64)
    disambiguation_prompts: tuple[str, ...] = Field(max_length=16)
    applies_when: tuple[str, ...] = Field(max_length=16)
    does_not_apply_when: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="before")
    @classmethod
    def _reject_unapproved_and_reserved_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = set(data)
        unknown = keys - _APPROVED_MANIFEST_KEYS
        reserved = {key for key in keys if _RESERVED_KEY_PATTERN.search(str(key))}
        if unknown or reserved:
            bad = sorted(unknown | reserved)
            raise ValueError(f"manifest contains unapproved keys: {', '.join(bad)}")
        return data

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _PACK_NAME_PATTERN.fullmatch(value):
            raise ValueError("name must be a lowercase pack slug")
        return value

    @field_validator("domain")
    @classmethod
    def _strip_domain(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("domain must not be blank")
        return value

    @field_validator("disambiguation_prompts", "applies_when", "does_not_apply_when")
    @classmethod
    def _validate_string_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("string lists must not contain blank strings")
            normalized.append(item)
        return tuple(normalized)

    @model_validator(mode="after")
    def _reject_duplicate_terms(self) -> GlossaryManifest:
        names: list[str] = []
        for glossary_term in self.glossary_terms:
            names.append(glossary_term.term.casefold())
            names.extend(alias.casefold() for alias in glossary_term.aliases)
        if len(names) != len(set(names)):
            raise ValueError("glossary_terms and aliases must be unique")
        return self


def _parse_manifest_text(text: str, *, source: str) -> GlossaryManifest:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{source} must be a YAML mapping")
    try:
        return GlossaryManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(f"{source} failed glossary manifest validation: {exc}") from exc


def load_manifest_resource(package: str, resource_name: str) -> GlossaryManifest:
    """Load a glossary manifest via ``importlib.resources``."""

    if "/" in resource_name or "\\" in resource_name or resource_name.startswith("."):
        raise ManifestError(f"invalid resource name: {resource_name!r}")
    try:
        resource = resources.files(package).joinpath(resource_name)
        with resource.open("r", encoding="utf-8") as handle:
            text = handle.read()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ManifestError(f"manifest resource not found: {package}:{resource_name}") from exc
    return _parse_manifest_text(text, source=f"{package}:{resource_name}")


def load_builtin_manifest(name: str) -> GlossaryManifest:
    """Load one built-in glossary pack by slug."""

    if not _PACK_NAME_PATTERN.fullmatch(name):
        raise ManifestError(f"invalid built-in pack name: {name!r}")
    manifest = load_manifest_resource("ouroboros.interview_adapters.packs", f"{name}.yaml")
    if manifest.name != name:
        raise ManifestError(
            f"pack name mismatch: requested {name!r}, manifest declares {manifest.name!r}"
        )
    return manifest


__all__ = [
    "GlossaryManifest",
    "GlossaryTerm",
    "ManifestError",
    "load_builtin_manifest",
    "load_manifest_resource",
]
