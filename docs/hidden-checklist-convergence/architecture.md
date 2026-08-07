# Hidden-Checklist Convergence Architecture

> Generated: 2026-08-07
> Approach: Pragmatic reuse of existing evaluation and Ralph owners

## Data Flow

```text
start_execute_seed
  -> execute result (success or evaluable failure)
  -> start_evaluate (30 minute bound)
  -> approved: terminal
  -> rejected: checklist -> EvaluationSummary -> Gen1 lineage events
  -> start_ralph(lineage continuation, max_generations=3 by default)
```

## Components

| Component | Responsibility | Location |
|---|---|---|
| Assertion-safe contract prompt | Show artifact obligations while hiding grader inputs | `src/ouroboros/orchestrator/atomic_prompt_builder.py` |
| Retry hint builder | Sanitize harness output and summarize worker trace facts | `src/ouroboros/orchestrator/retry_hints.py` |
| Evaluation/Ralph bridge | Convert checklist verdicts and seed Gen1 idempotently | `src/ouroboros/mcp/tools/evaluate_ralph_chain.py` |
| Evaluation terminal hook | Enqueue or reconnect Ralph after explicit rejection | `src/ouroboros/mcp/tools/evaluation_handlers.py` |
| Execution chain | Evaluate completed success/failure runs | `src/ouroboros/mcp/tools/execution_handlers.py` |
| Plugin Seed vault | Keep the raw Seed parent-owned and give workers only an opaque, session-bound handle | `src/ouroboros/mcp/tools/seed_handoff.py` |
| Runtime composition adapter | Reuse the production handler graph for builtin runtime interception | `src/ouroboros/mcp/tools/runtime_tool_composition.py` |

## Key Decisions

| Decision | Rationale |
|---|---|
| No answer-key configuration knob | Hidden grading is a correctness boundary, not a tuning option. |
| Manifest is read-only coaching input | Retry quality improves without allowing evidence to affect the deliver verdict. |
| Ralph owns convergence | Avoids duplicating loop termination, focus, and budget logic. |
| Deterministic lineage ID | A 120-bit digest of the complete Seed/run tuple fits the event store's 36-character aggregate-ID contract. |
| Durable lineage claims | Cross-process retries elect one Gen1 writer and one Ralph successor owner. |
| Atomic Gen1 publication | The creation and generation-completed events commit together; legacy creation-only state is repaired under the same claim. |
| Terminal successor recovery | Retries reconnect to both active and terminal Ralph jobs, closing the enqueue/result crash gap. |
| Single-AC checklist absence yields no fabricated AC result | Existing full-graph focus fallback remains honest and safe. |
| Raw Seed handoff is process-local | Persisting hidden verifier material in worker-queryable events would defeat the boundary. A server restart invalidates the opaque handle and evaluation fails closed instead of exposing the Seed. |
