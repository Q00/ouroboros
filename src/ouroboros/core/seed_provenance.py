"""Trusted provenance receipts for Seed generation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SeedGenerationReceipt:
    """Server-owned ambiguity-gate decision bound to one generated Seed."""

    seed_id: str
    gate_forced: bool | None
