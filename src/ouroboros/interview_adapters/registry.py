"""Built-in glossary registry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ouroboros.interview_adapters.manifest import (
    GlossaryManifest,
    ManifestError,
    load_builtin_manifest,
)

BUILTIN_PACK_NAMES: tuple[str, ...] = ("ui_ux_basics",)


@dataclass(frozen=True)
class BuiltinGlossaryRegistry:
    """Immutable registry for built-in glossary packs."""

    manifests: tuple[GlossaryManifest, ...]

    @classmethod
    def load(cls, names: Iterable[str] = BUILTIN_PACK_NAMES) -> BuiltinGlossaryRegistry:
        manifests = tuple(load_builtin_manifest(name) for name in names)
        seen: set[str] = set()
        for manifest in manifests:
            if manifest.name in seen:
                raise ManifestError(f"duplicate glossary pack name: {manifest.name}")
            seen.add(manifest.name)
        return cls(manifests=manifests)

    def get(self, name: str) -> GlossaryManifest:
        for manifest in self.manifests:
            if manifest.name == name:
                return manifest
        raise KeyError(name)


def builtin_registry() -> BuiltinGlossaryRegistry:
    """Return a freshly loaded registry of packaged built-in packs."""

    return BuiltinGlossaryRegistry.load()


__all__ = ["BUILTIN_PACK_NAMES", "BuiltinGlossaryRegistry", "builtin_registry"]
