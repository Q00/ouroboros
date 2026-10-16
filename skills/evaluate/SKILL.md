---
name: evaluate
description: "Use when the user wants proof that finished work meets its acceptance criteria, not just that tests pass: 'verify it is actually done', 'check it against the spec', 'evaluate the run'. Runs mechanical checks, a semantic review per criterion, and multi-model consensus when uncertain. With execution.auto_evolve on (the default), a rejected result with a Seed continues into a Ralph fix loop; pass auto_evolve: false to verify only."
aliases: [eval]
---

# /ouroboros:evaluate

Evaluate an execution session with the three-stage verification pipeline.

```
/ouroboros:evaluate <session_id> [artifact]
```

## Stage authority

1. **Mechanical** (lint, build, test, static analysis, coverage; no model cost): the only stage that can grant approval. At least one configured check must run and all must pass.
2. **Semantic** (AC compliance, goal alignment, drift): advisory. Can withhold approval and give feedback; cannot grant it.
3. **Consensus** (multi-model, only on uncertainty or request): advisory. A rejection withholds approval; an approval cannot grant it or lift a Stage 2 block.

Without an executed Stage 1 check the outcome is `acceptance_state: unverified` (not approved), with the model review attached as feedback.

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros evaluate"` for `ouroboros_start_evaluate`; `"+ouroboros job"` for `ouroboros_job_wait`, `ouroboros_job_status`, and `ouroboros_job_result`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

### Steps

1. **Session**: use the given `session_id`, else the most recent execution session id in the conversation.
2. **Artifact**: a file the user names (read it with the active runtime's `inspect_code` capability), else recent execution output; ask with `ask_user` if unclear.
2.5. **Acting verification** (behavior-bearing work): Stage 1 already runs build/test, so go further when the runtime can act. Run the result and observe the real effect (command output with `run_shell`, the endpoint's response, the rendered UI) instead of reasoning over the diff, ideally in one verification child via `orchestrate_subagents`, or inline when spawning is restricted. Probe the ACs against observed behavior and the adversarial classes (`misleading_output`, `hung_command`, `stale_state`, `dirty_worktree`, ...). Put the captured evidence (commands, outputs, artifact paths) into `artifact`. If the runtime cannot act, say behavior was not observed.
3. **Start** the background evaluation so a rejected verdict can continue through the configured Ralph chain:
   ```
   Tool: ouroboros_start_evaluate
   Arguments:
     session_id: <session ID>
     artifact: <the code/output to evaluate, plus observed-behaviour evidence from 2.5>
     seed_content: <original seed YAML, if available>
     acceptance_criterion: <specific AC to check, optional>
     artifact_type: "code"  (or "docs", "config")
     working_dir: <absolute project root, recommended>
     trigger_consensus: false  (true if user requests Stage 3)
     auto_evolve: <optional override; omit to use execution.auto_evolve>
   ```
   `working_dir` sets both Stage 1 command execution and Stage 2 source visibility; pass the absolute project root whenever known.
4. **Observe the job**: the start result carries the observer contract in `response.meta.job_observer` and in a final `<!-- ouroboros-job-observer-v1 base64 ... -->` content sentinel. If the structured meta is missing, recover the contract from the sentinel and fail closed unless there is exactly one sentinel at the end of the text, its bounded payload decodes to an object whose only key is `job_observer`, that contract passes canonical v1 validation (it equals, field for field, the v1 contract the server builds from its own `job_id`, `cursor`, `session_id`, `execution_id`, and `follow_result_job_keys`), its job identity matches the visible start receipt, and it does not differ from any structured contract; never rebuild it from visible IDs. With a valid contract, hand it unchanged to exactly one read-only observer child; its instructions already follow `chained_ralph_job_id` through `follow_result_job_keys`, and the main session must not poll the same job. Without a child session, catch up with `ouroboros_job_status` / `ouroboros_job_result` on the next turn or an explicit status request, and wait live with `ouroboros_job_wait` only when the user asked to watch. Present the convergence outcome only after any chained Ralph job is terminal. `chained_ralph_skipped: seed_unavailable` means no Seed was available: keep the rejected verdict and say continuation was safely skipped. In OpenCode plugin mode with automatic evolution disabled, the plugin child is the terminal surface and `job_id` is `None`.
5. **Present** each stage's result, the final approval decision, and on rejection the reason and suggested fixes. Pick the next action from the outcome:
   - approved: accept, or `ooo evolve` to refine further
   - Stage 1 rejected with `code_changes_detected: true`: fix the build/test failures, then `ooo evaluate`, or `ooo ralph` for an automated fix loop
   - Stage 1 rejected with `code_changes_detected: false`: `ooo run` first to produce code
   - Stage 2 rejected: `ooo run` with fixes, or `ooo evolve`
   - Stage 3 rejected: `ooo interview` to re-examine requirements, or `ooo unstuck`
   - `acceptance_state: unverified`: add executable checks to `.ouroboros/mechanical.toml` (or run `ouroboros detect`), then `ooo evaluate`; the semantic review is feedback, not a verdict

### Fallback (no Ouroboros MCP server)

Adopt the `evaluator` role yourself with the prompt the MCP server uses: `$OUROBOROS_AGENTS_DIR/evaluator.md` when that override is set, else `evaluator.md` in the installed `ouroboros.agents` package, else `src/ouroboros/agents/evaluator.md` in a source checkout. Give a qualitative, advisory evaluation against the Seed (no numerical scoring and no approval authority without the Python core).

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
