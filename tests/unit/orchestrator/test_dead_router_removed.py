"""Tombstone for the removed tier router (RFC v2 H5 / `orchestrator/routing.py`).

The effort-first decision (merged RFCs #1404/#1405) is binding: the unwired,
tier-first `ModelTier`/`decide_route` router is to be *removed*, not wired. It had
no non-test caller and contradicted the effort-first actuator. This test fails if
the module ever regrows, so a future change cannot silently resurrect the organ.
"""

from __future__ import annotations

import importlib

import pytest


def test_orchestrator_routing_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ouroboros.orchestrator.routing")
