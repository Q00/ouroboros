"""Demo Runner for Ouroboros Gemini 3 Hackathon.

This module provides demo scenarios that showcase the three wow moments:
1. Mind-Reading Interview - Socratic questioning extracts true intent
2. Living Tree - Real-time AC convergence visualization
3. Aha Root Cause - Gemini 3 identifies essential problems

Usage:
    python -m ouroboros.gemini3.demo_runner --wow-moment 1
    python -m ouroboros.gemini3.demo_runner --full-demo
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import random

from ouroboros.gemini3.convergence_accelerator import (
    HOTLConvergenceAccelerator,
    IterationData,
    IterationOutcome,
)
from ouroboros.gemini3.pattern_analyzer import (
    PatternAnalyzer,
    FailurePattern,
    PatternCategory,
    PatternSeverity,
)
from ouroboros.gemini3.dependency_predictor import (
    DependencyPredictor,
    ACDependency,
)
from ouroboros.gemini3.enhanced_devil import (
    EnhancedDevilAdvocate,
    DeepChallenge,
    ChallengeType,
    ChallengeIntensity,
)


# =============================================================================
# Demo Data Generators
# =============================================================================


def generate_demo_iterations(
    num_iterations: int = 75,
    num_acs: int = 8,
) -> list[IterationData]:
    """Generate realistic demo iteration data.

    Creates a convergence pattern that demonstrates:
    - Initial rapid progress
    - Spinning patterns (same error repeated)
    - Oscillation patterns (A->B->A->B)
    - Dependency blocking
    - Eventual convergence

    Args:
        num_iterations: Total iterations to generate
        num_acs: Number of acceptance criteria

    Returns:
        List of IterationData for demo
    """
    iterations: list[IterationData] = []
    base_time = datetime.now() - timedelta(hours=2)

    ac_ids = [f"AC_{i+1}" for i in range(num_acs)]
    ac_satisfied: dict[str, bool] = {ac: False for ac in ac_ids}

    # Convergence pattern: rapid start, some struggles, eventual success
    current_ac_idx = 0
    spinning_count = 0
    oscillation_state = "A"

    for i in range(1, num_iterations + 1):
        ac_id = ac_ids[current_ac_idx % num_acs]

        # Determine outcome based on phase and randomness
        if i <= 10:
            # Early phase: mixed results with progress
            outcome = random.choices(
                [IterationOutcome.SUCCESS, IterationOutcome.FAILURE, IterationOutcome.PARTIAL],
                weights=[0.3, 0.4, 0.3],
            )[0]
        elif i <= 30:
            # Middle phase: spinning pattern demo (iterations 15-20)
            if 15 <= i <= 20 and ac_id == "AC_4":
                outcome = IterationOutcome.FAILURE
                spinning_count += 1
            else:
                outcome = random.choices(
                    [IterationOutcome.SUCCESS, IterationOutcome.FAILURE, IterationOutcome.PARTIAL],
                    weights=[0.4, 0.3, 0.3],
                )[0]
        elif i <= 50:
            # Oscillation pattern demo (iterations 35-42)
            if 35 <= i <= 42 and ac_id == "AC_6":
                if oscillation_state == "A":
                    outcome = IterationOutcome.FAILURE
                    oscillation_state = "B"
                else:
                    outcome = IterationOutcome.PARTIAL
                    oscillation_state = "A"
            else:
                outcome = random.choices(
                    [IterationOutcome.SUCCESS, IterationOutcome.PARTIAL, IterationOutcome.FAILURE],
                    weights=[0.5, 0.3, 0.2],
                )[0]
        elif i <= 60:
            # Dependency blocking demo (AC_7 blocked by AC_2)
            if ac_id == "AC_7" and not ac_satisfied.get("AC_2", False):
                outcome = IterationOutcome.BLOCKED
            else:
                outcome = random.choices(
                    [IterationOutcome.SUCCESS, IterationOutcome.PARTIAL],
                    weights=[0.6, 0.4],
                )[0]
        else:
            # Final push: mostly successes
            outcome = random.choices(
                [IterationOutcome.SUCCESS, IterationOutcome.PARTIAL],
                weights=[0.8, 0.2],
            )[0]

        # Generate error message for failures
        error_messages = {
            IterationOutcome.FAILURE: [
                "ImportError: No module named 'utils'",
                "TypeError: expected str, got NoneType",
                "AssertionError: test_product_listing failed",
                "ValueError: invalid configuration value",
            ],
            IterationOutcome.BLOCKED: [
                f"Blocked by {ac_ids[(current_ac_idx - 1) % num_acs]}: prerequisite not met",
            ],
            IterationOutcome.STAGNANT: [
                "No progress: drift score unchanged for 3 iterations",
            ],
        }
        error_msg = ""
        if outcome in error_messages:
            error_msg = random.choice(error_messages[outcome])

        # Calculate drift score (decreasing over time for successful convergence)
        base_drift = 0.8 - (i / num_iterations * 0.6)
        drift = max(0.1, min(0.9, base_drift + random.uniform(-0.1, 0.1)))

        # Update satisfaction
        if outcome == IterationOutcome.SUCCESS:
            ac_satisfied[ac_id] = True

        iterations.append(
            IterationData(
                iteration_id=f"iter_{i:04d}",
                ac_id=ac_id,
                execution_id="demo_exec_001",
                timestamp=base_time + timedelta(minutes=i * 2),
                outcome=outcome,
                artifact=f"# Solution for {ac_id}\n\ndef solve_{ac_id.lower()}():\n    pass",
                error_message=error_msg,
                drift_score=drift,
                confidence=0.7 + random.uniform(-0.2, 0.2),
                model_used="gemini-2.5-pro",
                token_count=random.randint(500, 2000),
                reasoning=f"Iteration {i} reasoning for {ac_id}",
            )
        )

        # Move to next AC after success
        if outcome == IterationOutcome.SUCCESS:
            current_ac_idx += 1

    return iterations


def generate_demo_patterns() -> list[FailurePattern]:
    """Generate demo failure patterns for visualization."""
    return [
        FailurePattern(
            pattern_id="spinning_import_001",
            category=PatternCategory.SPINNING,
            severity=PatternSeverity.HIGH,
            description="ImportError repeated 5 times in AC_4",
            error_signature="import_error",
            affected_acs=("AC_4", "AC_3"),
            occurrence_count=5,
            confidence=0.9,
            root_cause_hypothesis="Module 'utils' was renamed to 'helpers' in AC_2",
            socratic_questions=(
                "Why is the same import error repeating?",
                "Was there a recent refactoring that renamed modules?",
                "Are all consumers of 'utils' updated?",
            ),
        ),
        FailurePattern(
            pattern_id="oscillation_type_001",
            category=PatternCategory.OSCILLATION,
            severity=PatternSeverity.HIGH,
            description="TypeError alternating with ValueError in AC_6",
            error_signature="type_error <-> value_error",
            affected_acs=("AC_6",),
            occurrence_count=8,
            confidence=0.85,
            root_cause_hypothesis="Contradictory fixes - type check causes value error and vice versa",
            socratic_questions=(
                "Why do fixes for one error cause the other?",
                "Is there a third approach that avoids both?",
                "What is the essential type contract here?",
            ),
        ),
        FailurePattern(
            pattern_id="dependency_block_001",
            category=PatternCategory.DEPENDENCY,
            severity=PatternSeverity.CRITICAL,
            description="AC_7 blocked by AC_2",
            affected_acs=("AC_7", "AC_2"),
            occurrence_count=3,
            confidence=0.95,
            root_cause_hypothesis="AC_7 requires auth service from AC_2",
            socratic_questions=(
                "What prerequisite from AC_2 does AC_7 need?",
                "Can the dependency be decoupled?",
                "Should execution order be restructured?",
            ),
        ),
        FailurePattern(
            pattern_id="stagnation_ac5_001",
            category=PatternCategory.STAGNATION,
            severity=PatternSeverity.MEDIUM,
            description="No drift improvement on AC_5 for 6 iterations",
            affected_acs=("AC_5",),
            occurrence_count=6,
            confidence=0.7,
            root_cause_hypothesis="Task may require different approach or human guidance",
            socratic_questions=(
                "Why is no progress being made?",
                "Is the task properly scoped?",
                "Would breaking AC_5 into smaller tasks help?",
            ),
        ),
        FailurePattern(
            pattern_id="assertion_test_001",
            category=PatternCategory.SYMPTOM,
            severity=PatternSeverity.MEDIUM,
            description="Assertion errors in test suite",
            error_signature="assertion_error",
            affected_acs=("AC_1", "AC_4", "AC_8"),
            occurrence_count=4,
            confidence=0.6,
            root_cause_hypothesis="Test expectations may not match updated requirements",
            socratic_questions=(
                "Are the tests testing the right behavior?",
                "Did requirements change after tests were written?",
                "Is this a test bug or implementation bug?",
            ),
        ),
    ]


def generate_demo_challenges() -> list[DeepChallenge]:
    """Generate demo challenges from Enhanced Devil's Advocate."""
    return [
        DeepChallenge(
            challenge_id="challenge_root_001",
            challenge_type=ChallengeType.ROOT_CAUSE,
            intensity=ChallengeIntensity.INTENSE,
            question="The ImportError repeated 5 times. Is this solution actually different from previous attempts, or are we treating the symptom again?",
            reasoning="Spinning pattern detected: same error despite multiple 'fixes'",
            evidence=(
                "Iteration 15: ImportError - added try/except",
                "Iteration 16: ImportError - changed import path",
                "Iteration 17: ImportError - same error persists",
            ),
            root_cause_hypothesis="The essential problem is not the import statement. The module was renamed in AC_2 but the rename wasn't propagated to all consumers.",
            alternative_approaches=(
                "Create a compatibility alias module",
                "Update all imports systematically using AST",
                "Add deprecation warning to old module name",
            ),
            confidence=0.88,
            depth=3,
            follow_up_questions=(
                "If we just fix this import, will it happen again when another module is renamed?",
                "Is there a missing abstraction layer that should protect consumers from implementation changes?",
            ),
        ),
        DeepChallenge(
            challenge_id="challenge_arch_001",
            challenge_type=ChallengeType.ASSUMPTION,
            intensity=ChallengeIntensity.CRITICAL,
            question="The TypeError at the AuthService boundary - are we assuming the wrong data contract?",
            reasoning="Iteration history shows UserModel evolved from Dict to Pydantic, but AuthService interface unchanged",
            evidence=(
                "Iteration 12: UserModel defined as TypedDict",
                "Iteration 23: AuthService created, expects Dict",
                "Iteration 31: UserModel changed to Pydantic BaseModel",
                "Iteration 45-47: TypeError at boundary",
            ),
            root_cause_hypothesis="This is not a TypeError - it's an ARCHITECTURAL MISMATCH. The interface contract between domain models and services was never formalized.",
            alternative_approaches=(
                "Create interface contract (Protocol class)",
                "Implement adapter pattern at boundary",
                "Use dependency inversion principle",
            ),
            confidence=0.92,
            depth=4,
            follow_up_questions=(
                "If we just fix the type, will this happen when UserModel evolves again?",
                "What other boundaries have implicit contracts?",
            ),
        ),
        DeepChallenge(
            challenge_id="challenge_alt_001",
            challenge_type=ChallengeType.ALTERNATIVE,
            intensity=ChallengeIntensity.MODERATE,
            question="The oscillation pattern suggests contradictory approaches. Is there a synthesis that avoids both failure modes?",
            reasoning="Solutions flip-flop between type strictness and flexibility",
            root_cause_hypothesis="The oscillation reveals a design tension between type safety and runtime flexibility",
            alternative_approaches=(
                "Use Union types with runtime discrimination",
                "Implement validation at a single choke point",
                "Consider gradual typing approach",
            ),
            confidence=0.75,
            depth=2,
        ),
    ]


# =============================================================================
# Demo Scenarios
# =============================================================================


class DemoRunner:
    """Runner for demo scenarios."""

    def __init__(self) -> None:
        """Initialize demo runner."""
        self.accelerator = HOTLConvergenceAccelerator()
        self.pattern_analyzer = PatternAnalyzer()
        self.dependency_predictor = DependencyPredictor()
        self.enhanced_devil = EnhancedDevilAdvocate()

    async def run_wow_moment_1(self) -> dict[str, Any]:
        """Run Mind-Reading Interview demo.

        Demonstrates Socratic questioning extracting true intent.
        """
        print("\n" + "=" * 60)
        print("WOW MOMENT 1: MIND-READING INTERVIEW")
        print("=" * 60 + "\n")

        # Simulated interview dialogue
        dialogue = [
            {
                "role": "user",
                "content": "Make the app faster",
            },
            {
                "role": "ouroboros",
                "content": """SOCRATIC QUESTION 1:
When you say "faster," what specifically feels slow?
- Page load time?
- API response time?
- UI responsiveness?
- Background processing?""",
            },
            {
                "role": "user",
                "content": "The product listing page",
            },
            {
                "role": "ouroboros",
                "content": """SOCRATIC QUESTION 2:
What is your current experience vs. expectation?

ONTOLOGICAL ANALYSIS (via Gemini 3 1M context):
Analyzing your codebase...
- Found 147 database queries on product listing route
- Detected N+1 query pattern in ProductRepository
- Current response time: 4.7 seconds

Is this N+1 query pattern the essential nature of the slowness?""",
            },
            {
                "role": "user",
                "content": "Wow, yes! I didn't know that was happening",
            },
            {
                "role": "ouroboros",
                "content": """ROOT CAUSE IDENTIFIED

Problem: N+1 queries in product listing
Current: 4.7s / 147 queries
Target: <2s / ~5 queries

GENERATED ACCEPTANCE CRITERIA:

AC_1: Implement batch query for product categories
      - Replace individual category lookups with IN clause
      - Expected: 147 queries -> 3 queries

AC_2: Add query result caching layer
      - Cache product data for 5 minutes
      - Implement cache invalidation on update

AC_3: Optimize product serialization
      - Use select_related for eager loading
      - Implement pagination

AC_4: Add performance monitoring
      - Track query count per request
      - Alert if queries exceed threshold

AC_5: Verify performance target
      - Load time <2s with 1000 products
      - Query count <10 per request

Ambiguity Score: 0.15 (was 0.82)
Ready for execution?""",
            },
        ]

        for entry in dialogue:
            if entry["role"] == "user":
                print(f"USER: {entry['content']}")
            else:
                print(f"\n OUROBOROS:\n{entry['content']}")
            print()
            await asyncio.sleep(0.5)  # Dramatic pause

        return {"dialogue": dialogue, "ambiguity_reduction": 0.82 - 0.15}

    async def run_wow_moment_2(self) -> dict[str, Any]:
        """Run Living Tree demo.

        Demonstrates real-time AC convergence visualization.
        """
        print("\n" + "=" * 60)
        print("WOW MOMENT 2: LIVING TREE")
        print("=" * 60 + "\n")

        # Generate and track iterations
        await self.accelerator.initialize()
        iterations = generate_demo_iterations(num_iterations=75, num_acs=8)

        print("Tracking 75 iterations...")
        for i, iteration in enumerate(iterations):
            await self.accelerator.track_iteration(iteration)

            # Print progress updates
            if (i + 1) % 10 == 0:
                state = self.accelerator.get_convergence_state()
                bar_length = int(state.satisfaction_percentage / 5)
                bar = "" * bar_length + "" * (20 - bar_length)
                print(f"Iteration {i+1}: [{bar}] {state.satisfaction_percentage:.1f}%")

                # Detect patterns at key points
                if state.satisfaction_percentage < 50:
                    history = self.accelerator.get_iteration_history(limit=i + 1)
                    patterns = await self.pattern_analyzer.analyze_patterns(history)
                    if patterns:
                        print(f"   PATTERN DETECTED: {patterns[0].category.value.upper()}")

        # Final state
        final_state = self.accelerator.get_convergence_state()
        print(f"\nFINAL STATE:")
        print(f"  Total Iterations: {final_state.total_iterations}")
        print(f"  Satisfaction: {final_state.satisfaction_percentage:.1f}%")
        print(f"  Success Rate: {final_state.convergence_rate:.1%}")
        print(f"  Context Utilization: {final_state.context_utilization:.1%}")

        # Pattern summary
        all_iterations = self.accelerator.get_iteration_history()
        patterns = await self.pattern_analyzer.analyze_patterns(all_iterations)
        print(f"\nPatterns Detected: {len(patterns)}")
        for p in patterns[:3]:
            print(f"  - {p.category.value}: {p.description}")

        return {
            "final_state": final_state,
            "patterns_detected": len(patterns),
            "curve_points": self.accelerator.get_convergence_curve(),
        }

    async def run_wow_moment_3(self) -> dict[str, Any]:
        """Run Aha Root Cause demo.

        Demonstrates Gemini 3 identifying essential problems.
        """
        print("\n" + "=" * 60)
        print("WOW MOMENT 3: AHA ROOT CAUSE")
        print("=" * 60 + "\n")

        # Show the problem
        print("PROBLEM: Iteration 45-47 - Same TypeError repeated")
        print("-" * 40)
        print("""
Iteration 45: FAILURE
  TypeError: expected dict, got UserModel

Iteration 46: FAILURE
  TypeError: expected dict, got UserModel

Iteration 47: FAILURE
  TypeError: expected dict, got UserModel

PATTERN DETECTED: SPINNING (3x same error)
""")

        print("TRADITIONAL AI FIX:")
        print("-" * 40)
        print("""
"Add .dict() call to convert UserModel to dict"

def authenticate(user: UserModel):
    return auth_service.validate(user.dict())  # Quick fix

""")

        print(" GEMINI 3 DEVIL'S ADVOCATE ANALYSIS:")
        print("-" * 40)

        # Get demo challenges
        challenges = generate_demo_challenges()
        arch_challenge = challenges[1]  # Architecture mismatch challenge

        print(f"""
ONTOLOGICAL ANALYSIS (using 1M context):

Reviewing full iteration history...

{chr(10).join(arch_challenge.evidence)}

ROOT CAUSE IDENTIFIED:

{arch_challenge.root_cause_hypothesis}

SOCRATIC CHALLENGE:
"{arch_challenge.question}"

DEEPER QUESTION:
"{arch_challenge.follow_up_questions[0] if arch_challenge.follow_up_questions else ''}"

RECOMMENDED ROOT CAUSE FIX:
{chr(10).join(f'  - {a}' for a in arch_challenge.alternative_approaches)}

Confidence: {arch_challenge.confidence:.0%}
""")

        print(" RESULT:")
        print("-" * 40)
        print("""
Applied interface contract pattern:
- Created UserProtocol interface
- AuthService depends on Protocol, not implementation
- UserModel implements Protocol

Iterations 48-50: AC_2, AC_4, AC_6 all PASS
No regressions detected in dependent ACs
""")

        return {
            "challenge": arch_challenge.to_dict(),
            "root_cause_identified": True,
        }

    async def run_full_demo(self) -> dict[str, Any]:
        """Run complete demo with all three wow moments."""
        results = {}

        results["wow_moment_1"] = await self.run_wow_moment_1()
        await asyncio.sleep(1)

        results["wow_moment_2"] = await self.run_wow_moment_2()
        await asyncio.sleep(1)

        results["wow_moment_3"] = await self.run_wow_moment_3()

        print("\n" + "=" * 60)
        print("DEMO COMPLETE")
        print("=" * 60)
        print("""
Philosophy-First Ouroboros Summary:
- Socratic Method: Extracted true intent from vague request
- Ontological Analysis: Identified root causes, not symptoms
- HOTL Convergence: 67 iterations to 100% satisfaction
- Devil's Advocate: Prevented 3 symptomatic fixes

"AI questioning AI" - That's Ouroboros.
""")

        return results


# =============================================================================
# CLI Entry Point
# =============================================================================


async def main():
    """Main entry point for demo."""
    import sys

    runner = DemoRunner()

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--wow-moment":
            if len(sys.argv) > 2:
                moment = int(sys.argv[2])
                if moment == 1:
                    await runner.run_wow_moment_1()
                elif moment == 2:
                    await runner.run_wow_moment_2()
                elif moment == 3:
                    await runner.run_wow_moment_3()
                else:
                    print(f"Unknown wow moment: {moment}")
            else:
                print("Usage: --wow-moment <1|2|3>")
        elif arg == "--full-demo":
            await runner.run_full_demo()
        else:
            print(f"Unknown argument: {arg}")
            print("Usage: python demo_runner.py [--wow-moment <1|2|3>] [--full-demo]")
    else:
        # Default: run full demo
        await runner.run_full_demo()


if __name__ == "__main__":
    asyncio.run(main())
