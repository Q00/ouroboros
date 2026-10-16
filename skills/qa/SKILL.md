---
name: qa
description: "General-purpose QA verdict for any artifact type"
---

# /ouroboros:qa

Fast single-pass quality verdict with actionable suggestions for any artifact: code, documents, API responses, test output, or custom content. For formal three-stage verification of an execution, use `ooo evaluate`.

```
ooo qa [file_path | artifact_text]
ooo qa                                     # recent execution output
/ouroboros:qa [file_path | artifact_text]
```

The QA Judge scores the artifact against a quality bar on Correctness, Completeness, Quality, Intent Alignment, and Domain-Specific dimensions:

| Score | Verdict | Loop action |
|-------|---------|-------------|
| >= 0.80 | PASS | done |
| 0.40 - 0.79 | REVISE | continue |
| < 0.40 | FAIL | escalate |

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros qa"` for `ouroboros_qa`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent; this skill works without MCP setup.

### QA steps

1. **Artifact**: a file path (read it with the active runtime's `inspect_code` capability), inline text, or else the most recent execution output; ask with `ask_user` if unclear.
2. **Quality bar**: the Seed's acceptance criteria when a Seed is in context, else the user's stated bar; otherwise ask "What does 'good' mean for this artifact?"
3. **Artifact type**: `code`, `test_output`, `document`, `api_response`, `screenshot`, or `custom`.

3.5. **Acting verification fan-out** (behavior-bearing artifacts): a text judge can be fooled by a hopeful log line. Use the active runtime's `orchestrate_subagents` capability to launch, together, one probe child per acting modality the runtime exposes: a process probe (`run_shell`: run the command or app or smoke commands with bounded timeouts, capture exit codes and real output), a browser probe (HTTP services and web UIs: load them, exercise the primary flows, capture rendering and console/network errors), a computer-use probe (GUI/TUI: drive it like a user, screenshot observed states), and an artifact probe (`inspect_code`: declared files exist with real content, not placeholders). Each probe returns structured evidence only (commands, observed effects, screenshots/paths, pass/fail per behavior) and covers the applicable adversarial classes the QA tool lists. Skip a modality only when its tools are absent or it is meaningless for the artifact, and say which were skipped and why. Await all probes and pass the merged evidence as `reference`; prefer observed behavior over source text as the `artifact` when they disagree.

4. **Call the tool**:
   ```
   Tool: ouroboros_qa
   Arguments:
     artifact: <the content to evaluate>
     quality_bar: <what 'pass' means>
     artifact_type: "code"  (or other type)
     reference: <observed-behaviour evidence from step 3.5, plus any reference>
     pass_threshold: 0.80  (adjustable)
     seed_content: <seed YAML if available>
   ```

5. **Present** the score and verdict prominently, then dimension scores, specific differences, and actionable suggestions. **Empirical evidence outranks the judge**: if the judge scores PASS but a probe observed the behavior failing, present REVISE/FAIL on that evidence and say so. If nothing could act, flag that behavior was not observed. Pick the next action from the verdict:
   - **PASS (done)**: proceed; the artifact meets the quality bar
   - **REVISE (continue)**: address the suggestions, then run `ooo qa` again
   - **FAIL (escalate)**: `ooo interview` to re-examine requirements, or `ooo unstuck` to challenge assumptions

### Iterative QA

The first call returns `qa_session_id` and `iteration_entry` in meta. On later calls pass `qa_session_id` and the accumulated `iteration_history`. Continue until the verdict is `pass` or `fail`.

### Fallback (no Ouroboros MCP server)

Adopt the `qa-judge` role yourself with the same prompt the MCP tool uses: `$OUROBOROS_AGENTS_DIR/qa-judge.md` when that override is set, else `qa-judge.md` in the installed `ouroboros.agents` package, else `src/ouroboros/agents/qa-judge.md` in a source checkout. If none is reachable, judge on the dimensions and thresholds above and label the verdict as an unaligned fallback. Run the step 3.5 probes first for behavior-bearing artifacts, render the verdict in the same shape the MCP tool returns (score, verdict, threshold, dimensions, differences, suggestions, reasoning, loop action), and keep the iterative contract with a `qa-<uuid4_short>` session id and an iteration count held in the conversation.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
