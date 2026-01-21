"""Resilience module for stagnation detection and recovery.

This module implements Epic 4: Resilience & Stagnation Recovery.

Components:
- StagnationDetector: Detects 4 stagnation patterns
- StagnationPattern: Enum of pattern types
- ExecutionHistory: Tracks execution state for detection
- LateralThinker: Generates alternative approaches via personas
- ThinkingPersona: 5 personas for lateral thinking
- Events: Stagnation and lateral thinking event types

Story 4.1: Stagnation Detection (4 Patterns)
- Spinning: Same output repeated
- Oscillation: A→B→A→B alternating pattern
- No Drift: No progress toward goal
- Diminishing Returns: Progress slowing

Story 4.2: Lateral Thinking Personas
- Hacker: Unconventional workarounds
- Researcher: Seeks additional information
- Simplifier: Reduces complexity
- Architect: Restructures the approach
- Contrarian: Challenges assumptions
"""

from ouroboros.resilience.lateral import (
    AllPersonasExhaustedEvent,
    LateralThinker,
    LateralThinkingActivatedEvent,
    LateralThinkingFailedEvent,
    LateralThinkingResult,
    LateralThinkingSucceededEvent,
    PersonaStrategy,
    ThinkingPersona,
)
from ouroboros.resilience.stagnation import (
    DiminishingReturnsDetectedEvent,
    ExecutionHistory,
    NoDriftDetectedEvent,
    OscillationDetectedEvent,
    SpinningDetectedEvent,
    StagnationDetection,
    StagnationDetector,
    StagnationPattern,
)

__all__ = [
    # Story 4.1: Stagnation Detection
    "StagnationDetector",
    "StagnationPattern",
    "StagnationDetection",
    "ExecutionHistory",
    "SpinningDetectedEvent",
    "OscillationDetectedEvent",
    "NoDriftDetectedEvent",
    "DiminishingReturnsDetectedEvent",
    # Story 4.2: Lateral Thinking
    "LateralThinker",
    "ThinkingPersona",
    "PersonaStrategy",
    "LateralThinkingResult",
    "LateralThinkingActivatedEvent",
    "LateralThinkingSucceededEvent",
    "LateralThinkingFailedEvent",
    "AllPersonasExhaustedEvent",
]
