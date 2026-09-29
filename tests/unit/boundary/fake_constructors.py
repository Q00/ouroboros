"""Fake check constructors: queued construction outcomes, no model call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.core.seed import Seed

from .calc_fixtures import (
    INPUT_DIGEST,
    _package,
)


class FakeConstructor:
    """Returns queued outcomes; records every call."""

    def __init__(self, *outcomes: ConstructionOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, ...]] = []

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        self.calls.append(tuple(feedback))
        return self.outcomes.pop(0)


def _ok(package) -> ConstructionOutcome:
    return ConstructionOutcome(package, None, package.input_digest, "fake")


def _constructor_factory(script: str, calls: list[dict[str, Any]]) -> Any:
    def factory(**kwargs: Any) -> Any:
        calls.append(kwargs)

        class _Constructor:
            async def construct(self, seed: Seed, base: Path, *, feedback=()):
                return _ok(_package(seed, "repro_add", script))

        return _Constructor()

    return factory


def _failing_constructor_factory(reason: str) -> Any:
    def factory(**_kwargs: Any) -> Any:
        class _Constructor:
            async def construct(self, seed: Seed, base: Path, *, feedback=()):
                return ConstructionOutcome(None, reason, INPUT_DIGEST, "fake")

        return _Constructor()

    return factory
