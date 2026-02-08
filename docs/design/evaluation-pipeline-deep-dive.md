# Evaluation Pipeline Deep Dive

## Overview

Ouroboros implements a three-stage progressive evaluation pipeline that prioritizes cost-effectiveness through intelligent staging. The pipeline executes cheap verification checks first, progressing to expensive multi-model consensus only when specific triggers fire. This design minimizes LLM API costs while maintaining rigorous quality standards.

The core principle: maximize validation quality while minimizing computational expense through strategic evaluation ordering.

## Pipeline Flow

```
EvaluationContext (execution_id, seed_id, current_ac, artifact, goal, constraints)
  → Stage 1: Mechanical Verification ($0)
    → IF FAIL → stop pipeline, return failure
  → Stage 2: Semantic Evaluation ($$)
    → IF score >= 0.8 AND no trigger → approved
    → IF score < 0.8 OR trigger fires → continue to Stage 3
  → ConsensusTrigger.evaluate() → check 6 conditions
  → Stage 3: Consensus ($$$) — only if triggered
    → Simple mode: 3-model vote, 2/3 majority
    → Deliberative mode: Advocate/Devil/Judge roles
```

The pipeline short-circuits at the earliest possible failure point, preventing unnecessary API calls for artifacts that fail basic checks.

## Stage 1: Mechanical Verification

**Implementation**: `MechanicalVerifier` in `evaluation/mechanical.py`

Stage 1 performs zero-cost local verification using standard development tools. No LLM calls are made.

### Check Types

The `CheckType` enum defines supported verification categories:

- `LINT`: Code style and quality checks
- `BUILD`: Compilation/syntax validation
- `TEST`: Automated test execution
- `STATIC`: Static type checking
- `COVERAGE`: Test coverage measurement

### Default Commands

- **Lint**: `ruff check` (Python linting)
- **Test**: `pytest` (test execution with coverage)

### Coverage Requirements

- **Threshold**: 0.7 (70% minimum coverage per NFR9)
- Coverage below threshold triggers pipeline failure

### Execution Constraints

- **Timeout**: 300 seconds per command
- Commands execute in isolated shell environments
- Failures are immediately terminal (pipeline stops)

### Cost Profile

Mechanical verification is completely free. It uses only local tooling with no external API calls.

## Stage 2: Semantic Evaluation

**Implementation**: `SemanticEvaluator` in `evaluation/semantic.py`

Stage 2 employs a single Standard-tier LLM to perform semantic analysis of the artifact against acceptance criteria and goals.

### Model Configuration

- **Default Model**: `openrouter/google/gemini-2.0-flash-001` (Standard tier)
- **Temperature**: 0.2 (reproducible, low-variance evaluation)

### Evaluation Dimensions

The `SemanticResult` dataclass captures multiple evaluation dimensions:

- `score`: Overall quality score (0.0-1.0)
- `ac_compliance`: Boolean flag for acceptance criteria satisfaction
- `goal_alignment`: Alignment with seed goal (0.0-1.0)
- `drift_score`: Deviation from seed intent (0.0-1.0)
- `uncertainty`: Evaluator confidence level (0.0-1.0)
- `reasoning`: Textual justification for scores

### Passing Thresholds

An artifact must satisfy all conditions to pass Stage 2:

| Metric | Threshold | Direction |
|--------|-----------|-----------|
| ac_compliance | true | must equal |
| score | 0.8 | >= |
| goal_alignment | 0.7 | >= |
| drift_score | 0.3 | <= |
| uncertainty | 0.3 | <= |

### Score Normalization

All numeric scores are clamped to the [0.0, 1.0] range to ensure consistent comparison.

### Stage 2 Outcomes

1. **Pass without escalation**: Score >= 0.8 AND no consensus trigger fires
2. **Escalate to Stage 3**: Score < 0.8 OR any consensus trigger condition met
3. **Immediate failure**: ac_compliance == false

## Consensus Trigger Matrix

**Implementation**: `ConsensusTrigger` in `evaluation/trigger.py`

The trigger matrix defines six conditions that mandate multi-model consensus evaluation. Triggers are evaluated in priority order with short-circuit logic (first match wins).

### Trigger Types

The `TriggerType` enum defines evaluation conditions:

| Priority | Trigger | Condition | Rationale |
|----------|---------|-----------|-----------|
| 1 | SEED_MODIFICATION | seed_modified == True | Seeds are immutable contracts; any modification requires multi-model validation |
| 2 | ONTOLOGY_EVOLUTION | ontology_changed == True | Schema changes affect all output structure and need diverse verification |
| 3 | GOAL_INTERPRETATION | goal_reinterpreted == True | Reinterpretation of goals needs multiple perspectives to validate correctness |
| 4 | SEED_DRIFT_ALERT | drift_score > 0.3 | Execution drifting from seed intent requires consensus verification |
| 5 | STAGE2_UNCERTAINTY | uncertainty > 0.3 | Single evaluator uncertainty necessitates additional validation |
| 6 | LATERAL_THINKING_ADOPTION | lateral_thinking_adopted == True | Alternative approaches need multi-model consensus before adoption |

### Trigger Evaluation

The `ConsensusTrigger.evaluate()` method:

1. Receives evaluation context and Stage 2 results
2. Checks conditions in priority order (1-6)
3. Returns first matching trigger type
4. Returns None if no triggers fire

### Default Thresholds

- **Drift threshold**: 0.3 (30% maximum drift)
- **Uncertainty threshold**: 0.3 (30% maximum uncertainty)

These thresholds are tuned to balance false positives (unnecessary consensus) against false negatives (missed validation needs).

## Stage 3a: Simple Consensus

**Implementation**: `ConsensusEvaluator` in `evaluation/consensus.py`

Simple consensus mode implements straightforward majority voting across multiple frontier-tier models.

### Model Selection

- **Default Models**: `openrouter/openai/gpt-4o`, `openrouter/anthropic/claude-sonnet-4-20250514`, `openrouter/google/gemini-2.5-pro`
- **Tier**: Frontier (highest capability)
- **Count**: 3 models minimum

### Voting Mechanism

1. All models evaluate artifact in parallel via `asyncio.gather()`
2. Each model produces a vote with:
   - `model`: Model identifier
   - `approved`: Boolean approval decision
   - `confidence`: Confidence score (0.0-1.0)
   - `reasoning`: Justification text
3. Votes are aggregated and counted

### Consensus Rules

- **Majority threshold**: 0.66 (2/3 majority required)
- **Minimum votes**: 2 (quorum requirement)
- **Decision**: approved_votes / total_votes >= majority_threshold

### Parallel Execution

All model calls execute concurrently to minimize latency. The pipeline waits for all votes before aggregating results.

## Stage 3b: Deliberative Consensus

**Implementation**: `DeliberativeConsensus` in `evaluation/consensus.py`

Deliberative mode implements a structured two-round debate process with specialized roles.

### Voter Roles

The `VoterRole` enum defines three distinct perspectives:

- `ADVOCATE`: Identifies strengths and validations
- `DEVIL`: Critiques via ontological questioning
- `JUDGE`: Reviews both positions and renders verdict

### Two-Round Process

#### Round 1 (Parallel Execution)

- **Advocate**: Builds case for approval by finding strengths
- **Devil's Advocate**: Builds case for rejection via Aspect-Oriented Programming (AOP) questions

Both roles execute simultaneously using frontier-tier models.

#### Round 2 (Sequential)

- **Judge**: Reviews Advocate and Devil positions
- Renders final verdict with confidence and reasoning
- May impose conditions for conditional approval

### Ontological Questioning

The Devil's Advocate employs the `DevilAdvocateStrategy` (imported from `strategies/devil_advocate.py`) which integrates with the `OntologicalAspect` AOP framework to probe fundamental assumptions:

- "Is this solving the root cause or treating symptoms?"
- "What assumptions are implicit in this solution?"

The strategy converts the `EvaluationContext` into a `ConsensusContext` for ontological analysis, then maps the `AnalysisResult` back to a `Vote` format.

### Verdict Structure

The `FinalVerdict` enum defines possible outcomes:

- `APPROVED`: Unconditional approval
- `REJECTED`: Rejection with reasoning
- `CONDITIONAL`: Approval contingent on specified conditions

### Judgment Result

The `JudgmentResult` dataclass captures:

- `verdict`: Final decision (FinalVerdict enum)
- `confidence`: Judge's confidence level (0.0-1.0)
- `reasoning`: Detailed justification
- `conditions`: List of conditions (if verdict == CONDITIONAL)

The `DeliberationResult` wraps the full deliberation output:

- `final_verdict`: The Judge's verdict
- `advocate_position`: Advocate's Vote
- `devil_position`: Devil's Advocate's Vote
- `judgment`: The JudgmentResult
- `is_root_solution`: True when Devil's Advocate also approves

### Root Solution Detection

When `DeliberationResult.is_root_solution == True`, the artifact has passed both advocacy and adversarial scrutiny, indicating a fundamental solution rather than a superficial fix.

## Configuration Defaults

### Stage 1 Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| coverage_threshold | 0.7 | Minimum test coverage (70%) |
| command_timeout | 300s | Maximum execution time per check |

### Stage 2 Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| semantic_temperature | 0.2 | Low-variance reproducible evaluation |
| satisfaction_threshold | 0.8 | Minimum score to skip Stage 3 |

### Stage 3 Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| majority_threshold | 0.66 | 2/3 consensus requirement |

### Trigger Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| drift_threshold | 0.3 | Maximum acceptable drift (30%) |
| uncertainty_threshold | 0.3 | Maximum acceptable uncertainty (30%) |

## Cost-Effectiveness Analysis

The staged pipeline minimizes costs through strategic ordering:

1. **Stage 1**: $0 cost eliminates trivially broken artifacts
2. **Stage 2**: Single Standard-tier call (~ $0.01) catches most issues
3. **Stage 3**: Three Frontier-tier calls (~ $0.30) only when necessary

Expected cost per evaluation:

- **90% of artifacts**: $0.01 (Stage 1 + Stage 2 only)
- **10% of artifacts**: $0.31 (full pipeline)
- **Average cost**: ~$0.04 per artifact

This represents a 90% cost reduction compared to naive "always use frontier models" approaches.

## Pipeline Guarantees

The evaluation pipeline provides several architectural guarantees:

1. **Monotonic strictness**: Each stage is strictly more rigorous than the previous
2. **Early termination**: Failures stop the pipeline immediately
3. **Deterministic triggering**: Consensus triggers are deterministic and auditable
4. **Cost predictability**: Maximum cost per evaluation is bounded
5. **Quality floor**: All approved artifacts pass mechanical, semantic, and (when triggered) consensus validation

These guarantees enable confident autonomous agent operation while maintaining cost discipline.
