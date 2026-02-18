"""Ouroboros evolution module - evolutionary loop for ontology evolution.

Transforms the linear pipeline (Interview → Seed → Execute → Evaluate → DONE)
into a closed evolutionary loop where ontology evolves across generations:

    Gen 1: Interview → Seed(O₁) → Execute → Evaluate
                                                  │
    Gen 2: Wonder → Reflect → Seed(O₂) → Execute → Evaluate
                                                        │
    Gen 3: Wonder → Reflect → Seed(O₃) → Execute → Evaluate
                                                        │
                                              [convergence check]
"""

from ouroboros.evolution.convergence import ConvergenceCriteria, ConvergenceSignal
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    EvolutionaryResult,
    GenerationResult,
    StepAction,
    StepResult,
)
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import OntologyMutation, ReflectEngine, ReflectOutput
from ouroboros.evolution.wonder import WonderEngine, WonderOutput

__all__ = [
    # Loop
    "EvolutionaryLoop",
    "EvolutionaryLoopConfig",
    "EvolutionaryResult",
    "GenerationResult",
    "StepAction",
    "StepResult",
    # Engines
    "WonderEngine",
    "ReflectEngine",
    # Outputs
    "WonderOutput",
    "ReflectOutput",
    "OntologyMutation",
    # Convergence
    "ConvergenceCriteria",
    "ConvergenceSignal",
    # Projection
    "LineageProjector",
]
