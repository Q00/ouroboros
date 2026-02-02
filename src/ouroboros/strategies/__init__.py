"""Ontological Analysis Strategies.

This module contains strategy implementations for the AOP-based
ontological framework. Each strategy is designed for a specific
join point (phase) in Ouroboros.

Available Strategies:
- DevilAdvocateStrategy: Consensus phase (Phase 4)
- InterviewOntologyStrategy: Interview phase (Phase 0) [planned]
- ContrarianStrategy: Resilience phase (Phase 3) [planned]

Reference: docs/ontological-framework/aop-design.md
"""

from ouroboros.strategies.devil_advocate import (
    ConsensusContext,
    DevilAdvocateStrategy,
)

__all__ = [
    "ConsensusContext",
    "DevilAdvocateStrategy",
]
