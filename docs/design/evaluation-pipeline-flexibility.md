# Design: Evaluation Pipeline Flexibility for Non-Code Workflows

> Status: Proposal | Author: Technical Writer Agent | Date: 2026-02-07

## Problem Statement

The evaluation pipeline (Phase 4) is structurally coupled to **code artifacts**. Stage 1 runs lint/build/test/static/coverage commands, and the semantic evaluator's prompt assumes code output. This blocks Research and Analysis task types (`seed.task_type`) from receiving meaningful evaluation, even though the execution layer already supports them via `ExecutionStrategy`.

**Root cause**: The pipeline lacks a mapping from `task_type` / `artifact_type` to evaluation behavior. The `CheckType` enum and `MechanicalConfig` hard-code shell commands for Python tooling, and the semantic prompt is code-centric.

## Current Architecture

```
EvaluationContext                    PipelineConfig
  execution_id                         stage1_enabled: bool
  seed_id                              stage2_enabled: bool
  current_ac                           stage3_enabled: bool
  artifact        ────────────►        mechanical: MechanicalConfig
  artifact_type = "code"               semantic:   SemanticConfig
  goal                                 consensus:  ConsensusConfig
  constraints                          trigger:    TriggerConfig
                                            │
                       ┌────────────────────┼────────────────────┐
                       ▼                    ▼                    ▼
                  Stage 1              Stage 2              Stage 3
              MechanicalVerifier    SemanticEvaluator    ConsensusEvaluator
              (lint,build,test,     (LLM: code-centric   (multi-model vote)
               static,coverage)      prompt)
```

### Key Coupling Points

| Component | File:Line | Coupling |
|-----------|-----------|----------|
| `CheckType` enum | `evaluation/models.py:40-55` | Hard-coded to LINT, BUILD, TEST, STATIC, COVERAGE |
| `MechanicalConfig` | `evaluation/mechanical.py:28-51` | Hard-codes `ruff`, `pytest`, `mypy` commands |
| `_get_command_for_check` | `evaluation/mechanical.py:316-332` | Maps CheckType to Python-specific commands |
| `EvaluationContext.artifact_type` | `evaluation/models.py:265` | Defaults to `"code"` |
| `EVALUATION_SYSTEM_PROMPT` | `evaluation/semantic.py:47-72` | Prompt text says "software evaluation", "code artifacts" |
| `Pipeline.evaluate()` | `evaluation/pipeline.py:110-114` | Always runs all 5 CheckTypes |
| `Seed.task_type` | `core/seed.py:161-164` | Already supports "code", "research", "analysis" but evaluation ignores it |

### What Already Works

The execution side is flexible:
- `Seed.task_type` supports `"code"`, `"research"`, `"analysis"` (`core/seed.py:161`)
- `ExecutionStrategy` protocol provides per-type tools, prompts, activity maps (`orchestrator/execution_strategy.py:23-47`)
- `EvaluationContext.artifact_type` exists as a field (`models.py:265`) but is only used in prompt text, not branching logic

## Proposed Design

### Principle: Strategy-per-artifact-type, not new evaluator classes

Rather than creating entirely new evaluators for each task type, extend the existing pipeline with **configurable check profiles** and **prompt templates** selected by `artifact_type`.

### Change 1: Extend CheckType and Add Check Profiles

Add non-code check types and define named profiles that bundle appropriate checks per artifact type.

```python
# evaluation/models.py - Extend CheckType
class CheckType(StrEnum):
    # Code checks (existing)
    LINT = "lint"
    BUILD = "build"
    TEST = "test"
    STATIC = "static"
    COVERAGE = "coverage"
    # Document checks (new)
    STRUCTURE = "structure"      # Markdown structure, heading hierarchy
    REFERENCES = "references"    # Citation/link validity
    COMPLETENESS = "completeness"  # Section coverage against AC

# evaluation/check_profiles.py - NEW FILE (~30 LOC)
CHECK_PROFILES: dict[str, list[CheckType]] = {
    "code": [CheckType.LINT, CheckType.BUILD, CheckType.TEST,
             CheckType.STATIC, CheckType.COVERAGE],
    "research": [CheckType.STRUCTURE, CheckType.REFERENCES,
                 CheckType.COMPLETENESS],
    "analysis": [CheckType.STRUCTURE, CheckType.COMPLETENESS],
}

def get_check_profile(artifact_type: str) -> list[CheckType]:
    return CHECK_PROFILES.get(artifact_type, CHECK_PROFILES["code"])
```

**Impact**: ~20 LOC in `models.py`, ~30 LOC new file `check_profiles.py`

### Change 2: Make MechanicalVerifier Command-Pluggable

The verifier already dispatches via `_get_command_for_check()`. Add commands for new check types and allow `MechanicalConfig` to carry document-oriented commands.

```python
# evaluation/mechanical.py - Extend MechanicalConfig
@dataclass(frozen=True, slots=True)
class MechanicalConfig:
    # ... existing code command fields ...
    # Document check commands (new)
    structure_command: tuple[str, ...] | None = None
    references_command: tuple[str, ...] | None = None
    completeness_command: tuple[str, ...] | None = None
```

For document checks where no shell command applies, `_run_check()` already handles `None` commands by returning a "skipped" result. This means document checks can be **LLM-free structural validators** implemented as Python functions rather than shell commands.

```python
# evaluation/mechanical.py - Add to _get_command_for_check
def _get_command_for_check(self, check_type: CheckType) -> tuple[str, ...] | None:
    commands = {
        CheckType.LINT: self.config.lint_command,
        # ... existing ...
        CheckType.STRUCTURE: self.config.structure_command,
        CheckType.REFERENCES: self.config.references_command,
        CheckType.COMPLETENESS: self.config.completeness_command,
    }
    return commands.get(check_type)
```

**Alternative (recommended)**: For document checks, bypass shell commands entirely and implement Python-native validators:

```python
async def _run_check(self, check_type: CheckType, artifact: str = "") -> CheckResult:
    # New: Python-native checks for document types
    if check_type == CheckType.STRUCTURE:
        return self._check_document_structure(artifact)
    if check_type == CheckType.REFERENCES:
        return self._check_references(artifact)
    if check_type == CheckType.COMPLETENESS:
        return self._check_completeness(artifact)
    # Existing: shell command checks for code types
    command = self._get_command_for_check(check_type)
    # ... rest unchanged ...
```

**Impact**: ~40 LOC added to `mechanical.py`, ~30 LOC for native validators

### Change 3: Parameterize Semantic Evaluation Prompts

The semantic evaluator prompt is hard-coded for code. Add prompt templates selected by `artifact_type`.

```python
# evaluation/semantic.py - Add prompt map
EVALUATION_PROMPTS: dict[str, str] = {
    "code": EVALUATION_SYSTEM_PROMPT,  # existing prompt
    "research": """You are a rigorous research evaluation assistant. Your task is to evaluate
research documents against acceptance criteria, source quality, and analytical depth.

You must respond ONLY with a valid JSON object in the following exact format:
{
    "score": <float between 0.0 and 1.0>,
    "ac_compliance": <boolean>,
    "goal_alignment": <float between 0.0 and 1.0>,
    "drift_score": <float between 0.0 and 1.0>,
    "uncertainty": <float between 0.0 and 1.0>,
    "reasoning": "<string explaining your evaluation>"
}

Evaluation criteria:
- score: Overall quality (depth, accuracy, source diversity)
- ac_compliance: true if the research addresses the acceptance criterion
- goal_alignment: How well findings align with the research goal
- drift_score: Deviation from original research intent (0.0 = on target)
- uncertainty: Your confidence in this evaluation
- reasoning: Brief explanation""",

    "analysis": """You are a rigorous analytical evaluation assistant. Your task is to evaluate
analytical documents against acceptance criteria, reasoning quality, and conclusion soundness.
... (similar structure, emphasizing logical rigor, evidence quality, balanced perspectives)""",
}

def get_evaluation_prompt(artifact_type: str) -> str:
    return EVALUATION_PROMPTS.get(artifact_type, EVALUATION_PROMPTS["code"])
```

Then in `SemanticEvaluator.evaluate()`:

```python
messages = [
    Message(role=MessageRole.SYSTEM, content=get_evaluation_prompt(context.artifact_type)),
    Message(role=MessageRole.USER, content=build_evaluation_prompt(context)),
]
```

**Impact**: ~40 LOC for prompt templates, ~2 LOC change in `evaluate()`

### Change 4: Wire artifact_type Through the Pipeline

The pipeline currently ignores `artifact_type`. Pass it through to select check profiles.

```python
# evaluation/pipeline.py - In EvaluationPipeline.evaluate()
if self._config.stage1_enabled:
    # Use artifact_type to select appropriate checks
    from ouroboros.evaluation.check_profiles import get_check_profile
    checks = get_check_profile(context.artifact_type)
    result = await self._mechanical.verify(
        context.execution_id,
        checks=checks,
    )
```

**Impact**: ~5 LOC change in `pipeline.py`

## Summary of Changes

| File | Change Type | Est. LOC |
|------|-------------|----------|
| `evaluation/models.py` | Extend `CheckType` enum | +10 |
| `evaluation/check_profiles.py` | **New**: Check profile registry | +30 |
| `evaluation/mechanical.py` | Add document check commands + native validators | +70 |
| `evaluation/semantic.py` | Add prompt templates + selection | +45 |
| `evaluation/pipeline.py` | Wire `artifact_type` to check profiles | +5 |
| `evaluation/__init__.py` | Export new symbols | +5 |
| **Total** | | **~165 LOC** |

No changes needed to:
- `evaluation/consensus.py` (already artifact-agnostic via prompts)
- `evaluation/trigger.py` (works on scores, not artifact content)
- `core/seed.py` (already has `task_type`)
- `orchestrator/execution_strategy.py` (already provides per-type behavior)

## Migration Path

1. **Phase 1** (this design): Add check profiles, prompt templates, native validators. All existing code/tests continue to work because `"code"` profile matches current behavior exactly.

2. **Phase 2** (future): Add richer document validators (markdown AST parsing, reference link checking). These are pure Python additions to `_check_document_structure()` etc.

3. **Phase 3** (future): Allow seeds to define custom `evaluation_config` overriding default check profiles and prompt templates for domain-specific workflows.

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| New CheckTypes break existing tests | Default profile for `"code"` is identical to current behavior |
| Prompt quality for research/analysis | Start with minimal prompts, iterate based on real usage |
| Native validators too simplistic | They're zero-cost sanity checks; semantic evaluator does the real work |

## Open Questions

1. Should `MechanicalConfig` be split into `CodeMechanicalConfig` / `DocumentMechanicalConfig`, or stay unified with optional fields?
   - **Recommendation**: Stay unified. Optional fields are simpler, and the config is already frozen/immutable.

2. Should document native validators receive the full `EvaluationContext` or just the artifact string?
   - **Recommendation**: Full context, so completeness checks can compare artifact against AC text.
