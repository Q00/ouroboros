# Dual-layer Recursive Language Model MVP

This document defines the concept architecture for the `ooo rlm` MVP. The MVP
uses Ouroboros as the outer recursive scaffold and Hermes Agent as the inner
language-model worker. The command must remain isolated behind `ooo rlm`; the
existing `ooo run` and `ooo evolve` loops keep their current behavior.

## Layer Model

```text
User
  |
  v
ooo rlm
  |
  v
Ouroboros outer scaffold
  - validates ambiguity <= 0.2
  - owns ACTree recursion, max depth 5
  - owns RLM tree state, scheduling, termination, and trace persistence
  - calls Hermes through HermesCliRuntime
  |
  v
Hermes inner LM layer
  - receives one bounded recursive sub-call at a time
  - proposes decomposition, atomic execution, or synthesis output
  - returns structured evidence to Ouroboros
```

The architecture is intentionally two-layered. Ouroboros is the only recursive
controller. Hermes is a subordinate model runtime used for bounded inference
calls. Ouroboros may call Hermes many times, but Hermes must not recursively call
Ouroboros or invoke `ooo` commands as part of the RLM loop.

## Orchestration Boundaries

The RLM MVP has one orchestration owner: the `ooo rlm` Ouroboros outer
scaffold. Hermes is an inner language-model worker reached through the existing
runtime adapter. The handoff contract is deliberately narrow so recursive state,
termination, and trace replay remain controlled by Ouroboros while Hermes
performs bounded semantic work.

### Responsibility Split

| Concern | Ouroboros outer scaffold | Hermes inner LM |
| --- | --- | --- |
| Command entrypoint | Owns the isolated `ooo rlm` path and keeps `ooo run` and `ooo evolve` behavior unchanged. | Is invoked only from the RLM path; does not expose a separate RLM entrypoint. |
| Recursion | Owns RLM node scheduling, AC decomposition recursion, atomic execution recursion, retries, and stop decisions. | Proposes local decomposition, local execution, summaries, or synthesis for one bounded node. |
| Guardrails | Enforces ambiguity `<= 0.2`, AC tree max depth `5`, cancellation, retry exhaustion, and convergence-style completion. | Echoes guardrail context in responses but cannot relax, bypass, or finalize guardrails. |
| State mutation | Creates and mutates AC nodes, RLM nodes, artifacts, summaries, and EventStore trace records. | Returns structured output only; does not mutate `ACTree`, RLM state, files, or EventStore directly. |
| Context ownership | Selects source chunks, summaries, child results, ancestry, and token budgets within Hermes RPC limits (`src/ouroboros/rlm/loop.py:396-413`; `src/ouroboros/rlm/loop.py:1977-2106`; `src/ouroboros/rlm/loop.py:2109-2286`). | Uses only the supplied context and cites only supplied evidence IDs (`src/ouroboros/rlm/contracts.py:313-459`; `tests/unit/rlm/test_contracts.py:59-146`). |
| Runtime boundary | Calls `HermesCliRuntime.execute_task_to_result()` through the existing `AgentRuntime` adapter. | Runs behind the adapter's Hermes RPC/tool mechanism; no new REPL or parallel transport is introduced. |

### Handoff Contract

Each outer-to-inner handoff is a single Hermes sub-call envelope rendered as the
prompt passed to `HermesCliRuntime.execute_task_to_result()`. Ouroboros supplies
the active `rlm_node_id`, linked `ac_node_id`, mode, guardrail values, selected
context, causal parent event, and required output schema. Hermes returns one JSON
object that echoes the IDs and mode, reports a local verdict, cites supplied
evidence, and includes mode-specific artifacts.

The accepted call lifecycle is:

1. Ouroboros validates the RLM guardrails and selects exactly one sub-call mode:
   `decompose_ac`, `execute_atomic`, `summarize_chunk`, or `synthesize_parent`.
2. Ouroboros binds bounded context and writes the call-start trace metadata.
3. Ouroboros invokes Hermes through the existing `HermesCliRuntime` adapter with
   no direct recursive call to Ouroboros.
4. Ouroboros parses and validates the Hermes JSON response, including schema
   version, echoed IDs, verdict, confidence, and evidence references.
5. Ouroboros commits accepted results into the AC tree, RLM tree, artifacts, and
   EventStore trace. Invalid or unsafe responses are recorded as failures and do
   not mutate recursive state.
6. Ouroboros schedules any follow-up decomposition, atomic execution,
   summarization, synthesis, retry, or termination decision.

This means Hermes may recommend that a node is atomic, decomposed, retryable, or
ready for synthesis, but Ouroboros alone turns that recommendation into
recursive control flow.

## Recursive Execution Flow

The `ooo rlm` loop runs as an outer Ouroboros state machine that repeatedly
wraps one bounded Hermes call, validates the returned JSON, commits accepted
state, and schedules the next recursive step. A full parent-to-child-to-parent
cycle proceeds as follows:

1. **Start the isolated command:** The user invokes `ooo rlm` with a target
   prompt, file, or directory. Ouroboros creates a new RLM run ID, root RLM
   node, and root AC node without dispatching to `ooo run` or `ooo evolve`.
2. **Apply outer guardrails:** Ouroboros checks the ambiguity score is `<= 0.2`,
   confirms the AC tree depth budget is `5`, initializes retry and cancellation
   state, and records the run-start trace metadata before any Hermes call can
   affect state.
3. **Schedule the active node:** Ouroboros selects the next queued RLM node and
   linked AC node. The scheduler chooses exactly one mode for that step:
   `decompose_ac`, `execute_atomic`, `summarize_chunk`, or
   `synthesize_parent`.
4. **Bind bounded context:** Ouroboros selects only the evidence needed for the
   active node: AC text, ancestry, parent summaries, source chunks, prior child
   results, and causal event IDs. If the target is larger than one Hermes
   context window, Ouroboros splits it into chunk calls or prior summaries
   before invoking Hermes (`src/ouroboros/rlm/loop.py:1977-2106`,
   `src/ouroboros/rlm/loop.py:2109-2286`, and
   `tests/unit/rlm/test_loop.py:490-810`).
5. **Call Hermes for decomposition:** For a non-terminal AC, Ouroboros renders a
   `decompose_ac` envelope and calls Hermes through
   `HermesCliRuntime.execute_task_to_result()`. Hermes analyzes only the
   supplied context and returns either an atomic rationale or proposed child
   ACs; it does not create AC nodes itself.
6. **Commit or reject decomposition:** Ouroboros parses the Hermes response,
   validates schema version, echoed RLM/AC IDs, confidence, verdict, and
   evidence references, then records the prompt, response, and decision in the
   EventStore. If Hermes proposes child ACs and the current AC depth is below
   `5`, Ouroboros creates child AC nodes and matching child RLM nodes. If the
   response says the AC is atomic, Ouroboros marks the node ready for atomic
   execution. Invalid responses become retry or failure records and do not
   mutate the AC tree.
7. **Recurse downward:** Each accepted child RLM node returns to step 3. The
   loop repeats until every branch either becomes atomic, reaches the depth
   limit, fails under retry policy, or is cancelled. At depth `5`, Ouroboros
   cannot accept deeper child proposals; it must execute the leaf atomically,
   retry with a stricter prompt, or fail the branch under existing termination
   policy.
8. **Call Hermes for atomic execution:** For an atomic AC, Ouroboros renders an
   `execute_atomic` envelope containing the assigned chunks, expected
   deliverable shape, and citation rules. Hermes returns a local result,
   checks performed, evidence-grounded claims, residual gaps, and an advisory
   verdict.
9. **Scale atomic work with chunk recursion:** When the assigned evidence is too
   large for one Hermes call, Ouroboros creates child RLM chunk nodes, calls
   Hermes once per chunk using `execute_atomic` or `summarize_chunk`, stores
   each partial result or summary, and then schedules a parent synthesis node.
   This lets the outer scaffold process repository-scale context while Hermes
   still receives one bounded RPC envelope at a time
   (`src/ouroboros/rlm/loop.py:474-547`,
   `src/ouroboros/rlm/loop.py:1977-2106`,
   `src/ouroboros/rlm/loop.py:2474-2706`, and
   `tests/unit/rlm/test_loop.py:490-810`).
10. **Synthesize completed children:** After all child ACs or chunk nodes are
    terminal, Ouroboros calls Hermes with `synthesize_parent`. Hermes combines
    the supplied child results into a parent-level synthesis and reports
    satisfied criteria, unresolved gaps, and an advisory next decision.
11. **Commit parent results:** Ouroboros validates the synthesis, attaches the
    accepted artifact to the parent AC/RLM node, rolls evidence references up to
    the parent trace record, and marks the parent complete, retryable, failed,
    or ready for additional decomposition according to existing termination
    rules.
12. **Recurse upward and terminate:** The scheduler repeats steps 3-11 until the
    root AC is satisfied, no runnable RLM nodes remain, cancellation is
    observed, retry exhaustion occurs, or another existing Ouroboros stop
    condition fires. The final artifact is emitted only by Ouroboros.
13. **Replay from traces:** EventStore records include RLM parent-child links,
    AC parent-child links, mode, selected chunk IDs, Hermes prompt and response
    hashes, evidence references, causal parent event IDs, and the outer
    decision after each call. These records are sufficient to reconstruct both
    the RLM tree and the AC tree without asking Hermes to replay its own
    control flow.

The control path therefore alternates predictably:

```text
Ouroboros schedules node
  -> Ouroboros binds context and trace envelope
  -> Hermes performs one bounded inner-LM sub-call
  -> Ouroboros validates the response
  -> Ouroboros commits AC/RLM state and trace events
  -> Ouroboros schedules children, synthesis, retry, or termination
```

Only the outer Ouroboros scaffold owns recursion. Hermes supplies local
language-model judgments at each node, and those judgments become recursive
work only after Ouroboros validates and commits them.

### Boundary Violations

The outer scaffold must reject or quarantine any Hermes response that crosses
the orchestration boundary. Boundary violations include attempts to call
`ooo rlm`, `ooo run`, `ooo evolve`, or other Ouroboros commands; attempts to
create or edit AC/RLM/EventStore state directly; claims based on context not
selected by Ouroboros; output that omits the required echoed IDs; or any
recommendation that tries to bypass ambiguity, depth, cancellation, retry, or
termination policy.

## Outer Ouroboros Scaffold

The outer scaffold is the executable control plane behind `ooo rlm`. It owns the
recursive loop, the durable state transitions, and the safety boundaries around
Hermes sub-calls. Hermes can propose local language-model outputs, but
Ouroboros decides what becomes part of the RLM run.

### Responsibilities

Ouroboros owns these parts of the dual-layer RLM loop:

- **Command isolation:** Expose the MVP only through `ooo rlm` and keep the
  existing `ooo run` and `ooo evolve` loops unchanged.
- **Ambiguity gate:** Validate that the run ambiguity score is `<= 0.2` before
  recursive execution starts or any Hermes sub-call can mutate state.
- **AC tree control:** Create, decompose, validate, and mutate `ACTree` nodes
  while enforcing the hard AC tree maximum depth of `5`.
- **RLM tree control:** Create RLM nodes, maintain parent-child links, track
  node modes, and schedule the next recursive step for decomposition,
  summarization, atomic execution, or synthesis.
- **Context selection and chunking:** Select repository files, evidence chunks,
  prior summaries, and child results that fit within Hermes RPC token limits.
  (Implemented by `src/ouroboros/rlm/loop.py:1977-2106` and
  `src/ouroboros/rlm/loop.py:2109-2286`; verified by
  `tests/unit/rlm/test_loop.py:416-488` and `tests/unit/rlm/test_loop.py:490-810`.)
- **Hermes invocation:** Call Hermes only through the existing
  `HermesCliRuntime` adapter and provide bounded prompts for each inner
  language-model sub-call.
- **Response validation:** Parse Hermes output, verify schema and echoed IDs,
  reject unsupported verdicts, and ensure citations refer only to supplied
  context.
- **State commits:** Apply accepted outputs by adding child ACs, marking atomic
  results, storing summaries, updating RLM node status, and producing artifacts.
- **Termination decisions:** Reuse Ouroboros stop conditions where applicable,
  including max depth, failure, cancellation, retry exhaustion, and
  convergence-style completion.
- **Trace persistence:** Record EventStore events that reconstruct the RLM tree,
  AC tree, Hermes call envelopes, responses, causal links, and outer decisions.
- **Benchmark orchestration:** Run the dogfooding benchmark against the
  Ouroboros `src/` tree and require grounded claims across at least three source
  files.
  (Implemented by `src/ouroboros/rlm/benchmark.py:74-101` and
  `src/ouroboros/rlm/loop.py:1790-1928`; verified by
  `tests/unit/rlm/test_loop.py:218-284`.)

### Lifecycle States

The outer scaffold models `ooo rlm` as an event-sourced state machine. Hermes
never owns these states; it only returns a bounded response that Ouroboros
validates before any transition is committed. Each transition record should
include the `rlm_run_id`, current `rlm_node_id`, linked `ac_node_id`, mode,
previous state, next state, causal parent event ID, and the outer decision that
caused the transition.

Run-level states:

| State | Meaning |
| --- | --- |
| `initialized` | The isolated `ooo rlm` invocation has been created, but no recursive work or Hermes call has started. |
| `guarding` | Ouroboros validates the ambiguity gate, AC max depth, target context, and command isolation constraints. |
| `scheduling` | Ouroboros selects the next non-terminal RLM node from the work queue and chooses one sub-call mode. |
| `running_node` | A selected RLM node is moving through context binding, Hermes invocation, response validation, and commit. |
| `synthesizing` | Child results are complete enough for a parent-level or run-level synthesis decision. |
| `completed` | All scheduled RLM nodes and linked AC leaves are terminal and the final artifact has been emitted. |
| `failed` | A non-recoverable guardrail, adapter, validation, retry-exhaustion, or trace persistence failure stopped the run. |
| `cancelled` | External cancellation stopped scheduling before the run reached completion. |

RLM node states:

| State | Meaning |
| --- | --- |
| `queued` | The node exists in the RLM tree but has not been selected for execution. |
| `preparing` | Guardrails and the linked AC node are being checked for this recursive step. |
| `context_bound` | Ouroboros has selected bounded chunks, summaries, child results, and trace ancestry for the Hermes envelope. |
| `awaiting_hermes` | One Hermes sub-call is in flight through `HermesCliRuntime.execute_task_to_result()`. |
| `validating_response` | Ouroboros is parsing the Hermes result, checking schema, echoed IDs, evidence references, and boundary rules. |
| `committing` | The accepted result is being applied to the AC tree, RLM tree, artifacts, and EventStore trace. |
| `blocked_retry` | The node may be retried or re-prompted under existing retry and termination policy. |
| `decomposed` | The node produced accepted child AC/RLM nodes and has no direct atomic result. |
| `atomic_complete` | The node completed an accepted atomic AC execution result. |
| `summary_complete` | The node completed an accepted source chunk summary. |
| `synthesis_complete` | The node completed an accepted synthesis of child results. |
| `failed` | The node cannot continue under the outer scaffold policy. |
| `cancelled` | The node was stopped by cancellation before it reached another terminal state. |

Valid run transitions:

| From | To | Allowed when |
| --- | --- | --- |
| `initialized` | `guarding` | The `ooo rlm` command has parsed its config and target. |
| `guarding` | `scheduling` | Ambiguity is `<= 0.2`, max AC depth is `<= 5`, and the command path is isolated from `ooo run` and `ooo evolve`. |
| `guarding` | `failed` | Any required guardrail fails before a Hermes call can be made. |
| `scheduling` | `running_node` | A non-terminal queued node exists and a mode has been selected. |
| `scheduling` | `synthesizing` | No child work is runnable, but parent or run synthesis remains. |
| `scheduling` | `completed` | The work queue is empty, all AC leaves are terminal, and final synthesis is already committed. |
| `running_node` | `scheduling` | The node commit created child work, retry work, summary work, or sibling work. |
| `running_node` | `synthesizing` | The node commit completed the final child needed by its parent. |
| `running_node` | `failed` | A non-recoverable node failure, adapter failure, validation failure, or trace write failure occurs. |
| `synthesizing` | `scheduling` | Synthesis creates follow-up recursive work or a retry under existing policy. |
| `synthesizing` | `completed` | Synthesis satisfies the root AC and no runnable RLM nodes remain. |
| `synthesizing` | `failed` | Synthesis exposes an unsatisfied root criterion that cannot be retried. |
| Any non-terminal state | `cancelled` | Cancellation is observed before the next state commit. |

Valid RLM node transitions:

| From | To | Allowed when |
| --- | --- | --- |
| `queued` | `preparing` | The scheduler selects the node and its linked AC is not terminal. |
| `preparing` | `context_bound` | Node depth, AC depth, ambiguity, cancellation, and retry policy allow work to continue. |
| `preparing` | `failed` | The node violates depth, ambiguity, retry, or command-boundary policy. |
| `context_bound` | `awaiting_hermes` | The Hermes input envelope has been rendered and the call-start trace record is ready. |
| `awaiting_hermes` | `validating_response` | The Hermes adapter returns a `TaskResult`. |
| `awaiting_hermes` | `blocked_retry` | The adapter failure is recoverable under retry policy. |
| `awaiting_hermes` | `failed` | The adapter failure is non-recoverable or retries are exhausted. |
| `validating_response` | `committing` | The response schema, echoed IDs, evidence references, and boundary rules pass. |
| `validating_response` | `blocked_retry` | The response is invalid or low-confidence but recoverable under retry policy. |
| `validating_response` | `failed` | The response is unsafe, mismatched, non-JSON, or otherwise non-recoverable. |
| `blocked_retry` | `queued` | Ouroboros schedules a bounded retry of the same node. |
| `blocked_retry` | `failed` | Existing retry exhaustion or termination policy stops the node. |
| `committing` | `decomposed` | A `decompose_ac` result is accepted and child AC/RLM nodes are created while AC depth remains below `5`. |
| `committing` | `atomic_complete` | An `execute_atomic` result is accepted for the linked AC. |
| `committing` | `summary_complete` | A `summarize_chunk` result is accepted and attached to the node context. |
| `committing` | `synthesis_complete` | A `synthesize_parent` result is accepted and linked to its parent AC/RLM node. |
| `committing` | `failed` | The state mutation or trace persistence commit fails. |
| Any non-terminal state | `cancelled` | Cancellation is observed before the node reaches a terminal state. |

Recursive execution advances only through `committing -> decomposed -> queued`
child nodes, `committing -> atomic_complete -> synthesizing` parent work, or
`blocked_retry -> queued` retry work. At AC depth `5`, accepted decomposition
proposals cannot create deeper child ACs; Ouroboros must either schedule atomic
execution for that leaf, mark the node failed under existing termination policy,
or stop the run if the root criterion cannot be satisfied.

## Inner Hermes Layer

The inner layer is implemented through the existing Hermes runtime adapter in
`src/ouroboros/orchestrator/hermes_runtime.py`. The RLM loop should reuse
`HermesCliRuntime.execute_task_to_result()` or the lower-level
`execute_task()` stream as its sub-call entry point. It should not create a new
Hermes REPL, a parallel subprocess protocol, or a separate RPC stack.

### Responsibilities

Hermes is responsible for bounded language-model work inside a single
Ouroboros-owned recursive step:

- **Semantic AC decomposition:** Given one AC, local context, depth, and trace
  identifiers, propose child ACs or explain why the AC is already atomic.
- **Atomic AC execution:** Given one atomic AC and the chunked evidence assigned
  by Ouroboros, produce a concrete result with citations to the supplied
  context.
- **Chunk summarization:** Given a source chunk selected by Ouroboros, summarize
  only the facts needed by the current AC or execution prompt.
- **Local synthesis:** Given child results for one parent node, synthesize a
  parent-level result without deciding global convergence.
- **Structured response discipline:** Return machine-parseable results that
  include the requested mode, verdict, confidence, evidence references, and
  failure reason when applicable.
- **Session continuity:** Use Hermes session handles provided by
  `HermesCliRuntime` when Ouroboros chooses to resume a related sub-call.
- **Runtime-owned MCP dispatch:** Keep using the existing Hermes adapter's
  shared `ooo` skill dispatch path for runtime tool interception. The RLM loop
  calls Hermes through the adapter; it does not bypass the adapter's MCP handler
  mechanism.

### Boundaries

Hermes must not own any recursive control-plane behavior:

- It must not call `ooo rlm`, `ooo run`, `ooo evolve`, or other Ouroboros
  commands from inside an RLM sub-call.
- It must not instantiate or mutate `ACTree`; Ouroboros owns the AC tree and the
  hard maximum depth of 5.
- It must not decide that the whole RLM run is complete. It can report a local
  verdict, while Ouroboros applies termination and convergence rules.
- It must not persist trace events directly. Ouroboros records Hermes prompts,
  responses, parent-child RLM links, AC links, and causal metadata in the
  EventStore.
- It must not bypass the ambiguity gate. The outer scaffold must verify
  ambiguity is `<= 0.2` before recursive execution begins.
- It must not change behavior of existing `ooo run` or `ooo evolve` flows.
- It must not read arbitrary repository context on its own. Ouroboros selects
  chunks within Hermes RPC token limits and sends the bounded context required
  for the current call (`src/ouroboros/rlm/loop.py:1977-2106`,
  `src/ouroboros/rlm/loop.py:2109-2286`, and
  `tests/unit/rlm/test_loop.py:416-488`).

### Execution Contract With Ouroboros

The RLM MVP treats Hermes as an inner inference service reached through the
existing Ouroboros runtime adapter. The contract has three parts: invocation,
lifecycle, and failure handling.

#### Invocation

Ouroboros invokes Hermes from the `ooo rlm` path only. The implementation should
construct or receive an `AgentRuntime` backed by
`HermesCliRuntime` from `src/ouroboros/orchestrator/hermes_runtime.py`, then call
`execute_task_to_result()` for each bounded RLM sub-call:

```python
result = await hermes_runtime.execute_task_to_result(
    prompt=rendered_rlm_envelope,
    tools=[],
    system_prompt=rlm_inner_layer_system_prompt,
    resume_handle=previous_resume_handle,
)
```

The rendered prompt contains the JSON-compatible input envelope defined below.
The `system_prompt` must state that Hermes is the inner LM, must use only the
provided context, and must not invoke Ouroboros or `ooo` commands. The RLM MVP
does not create a Hermes REPL, bypass the adapter, or introduce a second RPC
transport. It reuses the adapter's existing Hermes invocation and runtime-owned
skill dispatch mechanism.

At the adapter boundary, `HermesCliRuntime` converts the call into the existing
Hermes CLI protocol, including `hermes chat -Q --source tool -q <prompt>` and
`hermes chat --resume <session_id>` when a `RuntimeHandle` is supplied. The
returned `TaskResult` is the only successful call result Ouroboros consumes:

- `final_message` contains the Hermes JSON output candidate.
- `messages` preserves the normalized runtime transcript.
- `session_id` and `resume_handle` are recorded by Ouroboros for possible
  follow-up calls on the same RLM branch.

#### Lifecycle

Ouroboros owns the full call lifecycle around Hermes:

1. **Prepare:** Validate the `ooo rlm` guardrails before any inner call:
   ambiguity score `<= 0.2`, AC tree depth `<= 5`, and an RLM node that is not
   already terminal.
2. **Select:** Choose exactly one sub-call mode for the active RLM node:
   `decompose_ac`, `execute_atomic`, `summarize_chunk`, or
   `synthesize_parent`.
3. **Bind context:** Select source chunks, prior summaries, child results, AC
   metadata, RLM ancestry, and the causal parent event. Hermes receives only
   this bounded context.
4. **Invoke:** Persist or emit a call-start trace record, render the input
   envelope into the prompt, and call `HermesCliRuntime.execute_task_to_result()`
   through the `AgentRuntime` interface.
5. **Validate:** Parse the `TaskResult.final_message`, require the output schema
   below, verify echoed `rlm_node_id` and `ac_node_id`, and reject citations that
   do not refer to supplied chunks, summaries, or child results.
6. **Commit:** Apply accepted output in the outer scaffold only. Ouroboros, not
   Hermes, mutates `ACTree`, creates child ACs, marks atomic execution results,
   stores chunk summaries, updates RLM node state, and writes EventStore trace
   metadata.
7. **Schedule:** Reuse existing Ouroboros termination and retry signals where
   applicable, including max depth, cancellation, failure, and convergence-style
   stop conditions. If more work remains, Ouroboros schedules the next RLM node
   and may pass the prior Hermes `resume_handle` only for branch-local
   continuity.

This lifecycle keeps recursion single-owned: Hermes can recommend decomposition,
retry, or synthesis, but only Ouroboros schedules recursive work.

#### Failure Handling

Failures are normalized at the outer layer so a broken Hermes call cannot mutate
the AC tree or silently complete an RLM node:

- **Adapter or process failure:** A non-zero Hermes exit, startup or idle
  timeout, cancellation, or runtime exception is surfaced as `ProviderError`
  from `execute_task_to_result()`. Ouroboros records an RLM call failure artifact
  with the active RLM node, AC node, mode, selected chunks, and causal parent
  event.
- **Invalid output:** Empty output, non-JSON output, schema-version mismatch,
  missing required fields, ID mismatch, unsupported verdicts, or evidence
  references outside the supplied context are contract violations. Ouroboros
  quarantines the raw response in trace metadata and must not apply child ACs,
  execution results, summaries, or synthesis from that response.
- **Recoverable local gaps:** A valid response with `verdict = retryable`,
  low confidence, or non-empty `residual_gaps` is advisory. Ouroboros may retry
  the same RLM node with a stricter prompt, schedule a summarization or
  decomposition sub-call, or fail the AC according to existing termination
  policy.
- **Guardrail failure:** Ambiguity scores above `0.2`, AC depth above `5`, or
  attempts by Hermes to delegate to `ooo rlm`, `ooo run`, `ooo evolve`, or other
  Ouroboros commands are outer-layer failures. Ouroboros stops or rejects the
  affected branch without asking Hermes to self-correct recursively.
- **Skill dispatch failure:** If the Hermes adapter's runtime-owned skill
  intercept path reports a recoverable MCP dispatch error, the adapter may fall
  through to the Hermes CLI according to its existing behavior. If the final
  `TaskResult` is unsuccessful, Ouroboros handles it as an adapter failure.
- **Resume failure:** A stale or invalid `resume_handle` is not fatal to the
  whole RLM run by itself. Ouroboros may retry the call without the handle, but
  it must preserve the failed handle and retry decision in the trace.

Every failure path must leave enough EventStore metadata to reconstruct the RLM
node, linked AC node, selected context, Hermes prompt, raw response or provider
error, and the outer decision that followed.

### Inner Hermes Input Schema

Each Hermes sub-call receives one JSON-compatible envelope rendered into the
prompt passed to `HermesCliRuntime.execute_task_to_result()`. This is an
application-level input contract, not a new Hermes transport or REPL. Ouroboros
builds the envelope, passes it through the existing runtime adapter, and records
the same envelope in the trace metadata.

```json
{
  "schema_version": "rlm.hermes.input.v1",
  "mode": "decompose_ac | execute_atomic | summarize_chunk | synthesize_parent",
  "call_context": {
    "call_id": "rlm_call_0007",
    "parent_call_id": "rlm_call_0003",
    "depth": 2
  },
  "parent_execution_context": {
    "schema_version": "rlm.parent_execution_context.v1",
    "generation_id": "rlm_generation_0",
    "mode": "execute_atomic",
    "parent_node_id": "rlm_node_0001",
    "parent_ac_node_id": "ac_0001",
    "parent_call_id": "rlm_call_0003",
    "parent_trace_id": "trace_0003",
    "current_node_id": "rlm_node_0007",
    "current_ac_node_id": "ac_0004",
    "current_call_id": "rlm_call_0007",
    "current_trace_id": "trace_0007",
    "current_depth": 2,
    "child_order": 0,
    "sibling_count": 3,
    "prior_sibling_result_count": 0,
    "completed_sibling_count": 0,
    "failed_sibling_count": 0,
    "recorded_child_result_ids": [],
    "recorded_child_node_ids": [],
    "recorded_child_ac_node_ids": [],
    "recorded_child_call_ids": [],
    "recorded_child_chunk_ids": [],
    "synthesized_summary_present": false
  },
  "run": {
    "rlm_run_id": "rlm_20260428_001",
    "seed_id": "optional-seed-or-benchmark-id",
    "working_directory": "/path/selected/by/ouroboros",
    "ambiguity_score": 0.18,
    "ambiguity_threshold": 0.2
  },
  "rlm_node": {
    "id": "rlm_node_0007",
    "parent_id": "rlm_node_0003",
    "depth": 2,
    "ancestry": ["rlm_node_0001", "rlm_node_0003"]
  },
  "ac_node": {
    "id": "ac_0004",
    "parent_id": "ac_0001",
    "depth": 2,
    "max_depth": 5,
    "title": "Document Hermes input schema",
    "statement": "Concept document specifies the inner Hermes layer input schema and required context",
    "status": "pending | decomposing | atomic | executing | synthesizing"
  },
  "objective": {
    "instruction": "The bounded task Hermes must perform for this sub-call.",
    "success_criteria": ["Concrete local criteria checked by Ouroboros"],
    "non_goals": ["Do not invoke ooo commands", "Do not mutate ACTree directly"]
  },
  "constraints": {
    "max_ac_depth": 5,
    "must_not_call_ouroboros": true,
    "must_use_supplied_context_only": true,
    "token_budget": {
      "max_input_tokens": 24000,
      "max_output_tokens": 4000
    }
  },
  "context": {
    "prompt_summary": "Short outer-layer summary of why this call exists.",
    "parent_execution_context": {
      "schema_version": "rlm.parent_execution_context.v1",
      "current_call_id": "rlm_call_0007"
    },
    "parent_result": "Optional parent or prior synthesis summary.",
    "parent_node_summary": {
      "schema_version": "rlm.parent_node_summary.v1",
      "parent_node_id": "rlm_node_0001",
      "parent_ac_node_id": "ac_0001",
      "generation_id": "rlm_generation_0",
      "child_result_count": 1,
      "completed_child_count": 1,
      "failed_child_count": 0,
      "child_result_ids": ["rlm_node_0001:child_result:000"],
      "child_node_ids": ["rlm_node_0008"],
      "child_ac_node_ids": ["ac_0005"],
      "child_call_ids": ["rlm_call_atomic_chunk_001"],
      "child_chunk_ids": ["src/ouroboros/orchestrator/hermes_runtime.py:1-120"],
      "child_completion_statuses": ["completed"]
    },
    "synthesized_subcall_summary": {
      "schema_version": "rlm.synthesized_subcall_summary.v1",
      "parent_node_id": "rlm_node_0001",
      "parent_ac_node_id": "ac_0001",
      "generation_id": "rlm_generation_0",
      "summary": "1 child sub-call(s) recorded for parent synthesis: 1 completed, 0 failed.",
      "child_result_summaries": [
        {
          "child_result_id": "rlm_node_0001:child_result:000",
          "child_node_id": "rlm_node_0008",
          "child_ac_node_id": "ac_0005",
          "completion_status": "completed",
          "reported_summary": "Bounded child result."
        }
      ]
    },
    "chunks": [
      {
        "chunk_id": "src/ouroboros/orchestrator/hermes_runtime.py:1-120",
        "source_path": "src/ouroboros/orchestrator/hermes_runtime.py",
        "start_line": 1,
        "end_line": 120,
        "content": "Ouroboros-selected source excerpt or summary.",
        "token_estimate": 1800
      }
    ],
    "summaries": [
      {
        "summary_id": "summary_0002",
        "source_chunk_ids": ["src/ouroboros/orchestrator/hermes_runtime.py:1-120"],
        "content": "Previously generated bounded summary."
      }
    ],
    "child_results": [
      {
        "order": 0,
        "child_node_id": "rlm_node_0008",
        "child_ac_node_id": "ac_0005",
        "call_id": "rlm_call_atomic_chunk_001",
        "chunk_id": "src/ouroboros/orchestrator/hermes_runtime.py:1-120",
        "completion_status": "completed",
        "status_metadata": {
          "mode": "execute_atomic",
          "generation_id": "rlm_generation_0",
          "parent_call_id": "rlm_call_atomic_synthesis",
          "depth": 1,
          "exit_code": 0,
          "resume_handle_present": false
        },
        "question_payload": {
          "mode": "execute_atomic",
          "rlm_node_id": "rlm_node_0008",
          "ac_node_id": "ac_0005",
          "selected_chunk_ids": ["src/ouroboros/orchestrator/hermes_runtime.py:1-120"]
        },
        "result_payload": {
          "exit_code": 0,
          "completion": "Child-level result to synthesize.",
          "reported_result": {
            "summary": "Bounded child result."
          },
          "verdict": "passed",
          "confidence": 0.86,
          "evidence_references": [],
          "residual_gaps": []
        }
      }
    ],
    "normalized_child_ac_inputs": [
      {
        "question": {
          "mode": "execute_atomic",
          "rlm_node_id": "rlm_node_0008",
          "ac_node_id": "ac_0005",
          "title": "Execute RLM target atomically",
          "statement": "Execute one bounded atomic RLM step using only the supplied context.",
          "prompt_summary": "Chunk-level RLM atomic execution.",
          "instruction": "Produce the atomic execution result for this RLM MVP generation.",
          "success_criteria": [
            "Hermes returns a local atomic execution verdict",
            "The result references supplied evidence only"
          ],
          "selected_chunk_ids": ["src/ouroboros/orchestrator/hermes_runtime.py:1-120"]
        },
        "result": {
          "exit_code": 0,
          "completion": "Child-level result to synthesize.",
          "reported_result": {
            "summary": "Bounded child result."
          },
          "verdict": "passed",
          "confidence": 0.86,
          "evidence_references": [],
          "residual_gaps": []
        },
        "status": {
          "completion_status": "completed",
          "mode": "execute_atomic",
          "generation_id": "rlm_generation_0",
          "parent_call_id": "rlm_call_atomic_synthesis",
          "depth": 1,
          "exit_code": 0,
          "resume_handle_present": false
        },
        "ordering": {
          "order": 0,
          "sibling_index": 0,
          "child_node_id": "rlm_node_0008",
          "child_ac_node_id": "ac_0005",
          "call_id": "rlm_call_atomic_chunk_001",
          "chunk_id": "src/ouroboros/orchestrator/hermes_runtime.py:1-120",
          "generation_id": "rlm_generation_0",
          "parent_call_id": "rlm_call_atomic_synthesis",
          "depth": 1
        }
      }
    ]
  },
  "trace": {
    "event_store_session_id": "session_abc",
    "call_id": "rlm_call_0007",
    "parent_call_id": "rlm_call_0003",
    "causal_parent_event_id": "event_123",
    "depth": 2,
    "selected_chunk_ids": ["src/ouroboros/orchestrator/hermes_runtime.py:1-120"],
    "resume_handle_id": "optional-hermes-runtime-handle"
  },
  "output_contract": {
    "format": "json",
    "required_fields": [
      "mode",
      "verdict",
      "confidence",
      "result",
      "evidence_references",
      "residual_gaps"
    ]
  }
}
```

The envelope fields are intentionally redundant with the trace record. The
runtime prompt needs enough local context for Hermes to answer the current
bounded question, while the EventStore needs enough metadata to replay why the
call happened and how Ouroboros interpreted the response.

### Parent Execution State

When a parent RLM node waits on child sub-calls, Ouroboros records child results
in parent-owned execution state before asking Hermes to synthesize them. The raw
record keeps the parent-owned `order`, child AC/RLM IDs, call/chunk IDs,
completion status, status metadata, extracted question payload, and raw result
payload for replay and quarantine.
Every Hermes request also carries a `parent_execution_context` object at the
top level and under `context`. It contains only parent-owned scheduling fields:
the parent RLM/AC IDs when a parent exists, current RLM/AC/call/trace IDs,
sibling order and counts, already recorded child IDs, and whether the parent
summary has been synthesized. It intentionally excludes raw child completions.
For the next child-AC input, Ouroboros also serializes each record into
`normalized_child_ac_inputs` with four stable sections: `question` contains the
child AC prompt fields, `result` contains the raw and parsed Hermes output,
`status` contains the completion state and runtime status metadata, and
`ordering` contains the parent-owned order, sibling index, child AC/RLM IDs,
call ID, and chunk ID.

Parent rollup metadata has its own schema and is not the raw child output
record. The structured `parent_node_summary` object uses schema version
`rlm.parent_node_summary.v1` and contains only parent identity, generation ID,
child counts, child result IDs, child node/AC IDs, child call/chunk IDs, and
completion statuses. It intentionally excludes `result_payload`,
`status_metadata`, raw Hermes completions, and other opaque child artifacts.
Parent synthesis prompts may include `parent_node_summary`, ordered
`child_results`, ordered `normalized_child_ac_inputs`, and
`synthesized_subcall_summary`: the summary gives replay and scheduling code a
compact, validated rollup, `child_results` remains the quarantine-friendly raw
record set, `normalized_child_ac_inputs` is the deterministic child-AC shape
Hermes synthesizes, and `synthesized_subcall_summary` is the compact child
rollup included in the parent LM input when the parent node resumes.

The executable loop captures each completed child Hermes result at the recursive
boundary immediately after the child call returns, before parent scheduling or
synthesis resumes.
The parent serializes child results sorted by `order`, so trace replay and
Hermes synthesis receive the same ordering even if child records are appended or
loaded in a different in-memory order.

### Required Context

Every mode requires these fields:

- `schema_version`, `mode`, and `objective` so Hermes can follow the exact local
  task contract.
- `run.ambiguity_score` and `run.ambiguity_threshold` so the prompt preserves
  the outer ambiguity gate that was already validated by Ouroboros.
- `parent_execution_context` so every Hermes request knows the parent-owned
  scheduling identity, current call identity, sibling order, and previously
  recorded child-result IDs without receiving raw child completions.
- `rlm_node.id`, `rlm_node.parent_id`, `rlm_node.depth`, and `rlm_node.ancestry`
  so the RLM tree can be reconstructed from traces.
- `ac_node.id`, `ac_node.parent_id`, `ac_node.depth`, `ac_node.max_depth`,
  `ac_node.statement`, and `ac_node.status` so the AC tree and RLM tree remain
  causally linked.
- `constraints.max_ac_depth = 5`, `constraints.must_not_call_ouroboros = true`,
  and `constraints.must_use_supplied_context_only = true` so the inner layer
  cannot take control of recursion or context discovery.
- `trace.event_store_session_id`, `trace.causal_parent_event_id`, and
  `trace.selected_chunk_ids` so Ouroboros can persist prompt, response, and
  chunk lineage without Hermes writing trace events directly.
- `output_contract` so Hermes returns the structured fields needed by the outer
  scaffold.

Mode-specific required context:

| Mode | Required additional input |
| --- | --- |
| `decompose_ac` | Current AC statement, parent AC summary when present, local evidence chunks or summaries, current AC depth, `max_depth = 5`, and decomposition success criteria. |
| `execute_atomic` | Atomic AC statement, assigned implementation or evidence chunks, prior decomposition rationale when available, expected deliverable shape, and citation requirements. |
| `summarize_chunk` | One source chunk, source path or logical source ID, source span, target AC ID, summary purpose, and token budget for the summary. |
| `synthesize_parent` | Parent AC statement, ordered child result summaries, child verdicts, evidence references, unresolved child gaps, and parent-level success criteria. |

Hermes may receive a `resume_handle_id` for trace readability, but the actual
resume handle is passed as the existing `resume_handle` argument on
`HermesCliRuntime.execute_task_to_result()`. The prompt schema therefore
describes the task payload; session continuation remains owned by the adapter
contract.

### Inner Hermes Output Schema

Every Hermes sub-call must return a single JSON object. The object is parsed by
Ouroboros, validated against the mode-specific contract, and then converted into
AC tree updates, RLM tree updates, and trace events. Hermes may include concise
human-readable explanation inside string fields, but it must not wrap the JSON in
Markdown fences or append extra prose outside the object.

```json
{
  "schema_version": "rlm.hermes.output.v1",
  "mode": "decompose_ac | execute_atomic | summarize_chunk | synthesize_parent",
  "rlm_node_id": "rlm_node_0007",
  "ac_node_id": "ac_0004",
  "verdict": "atomic | decomposed | passed | failed | partial | retryable",
  "confidence": 0.86,
  "result": {
    "summary": "Concise local result for the requested mode.",
    "details": "Optional bounded explanation grounded in the supplied context."
  },
  "evidence_references": [
    {
      "chunk_id": "src/ouroboros/orchestrator/hermes_runtime.py:1-120",
      "source_path": "src/ouroboros/orchestrator/hermes_runtime.py",
      "start_line": 1,
      "end_line": 120,
      "claim": "Grounded claim supported by this span."
    }
  ],
  "residual_gaps": [
    {
      "gap": "Missing or uncertain fact.",
      "impact": "Why the gap matters locally.",
      "suggested_next_step": "Bounded follow-up Ouroboros may schedule."
    }
  ],
  "artifacts": [],
  "control": {
    "requires_retry": false,
    "suggested_next_mode": "decompose_ac | execute_atomic | summarize_chunk | synthesize_parent | none",
    "must_not_recurse": false
  }
}
```

Common field rules:

- `schema_version` must be `rlm.hermes.output.v1`.
- `mode` must echo the input mode so trace replay can pair prompts and
  responses without inspecting free text.
- `rlm_node_id` and `ac_node_id` must echo the input IDs. Ouroboros rejects or
  quarantines responses whose IDs do not match the active node.
- `verdict` is local to this sub-call. Hermes can recommend `retryable` or
  `partial`, but Ouroboros decides recursion, retry, failure, and completion.
- `confidence` is a number from `0.0` to `1.0`; values below the outer policy
  threshold can trigger retry or decomposition, but do not bypass the
  `ambiguity_score <= 0.2` run gate.
- `evidence_references` may cite only supplied chunk IDs, summaries, or child
  results. Hermes must not cite files or context it discovered independently.
- `residual_gaps` must be an empty array when the verdict is `passed`,
  `decomposed`, or `atomic` with no known local blockers.
- `artifacts` contains mode-specific payloads described below.
- `control.must_not_recurse = true` is used when Hermes believes the current AC
  should be treated as atomic even if more decomposition is possible.

Mode-specific artifact schemas:

| Mode | Required `artifacts` entries |
| --- | --- |
| `decompose_ac` | One `decomposition` artifact containing either `is_atomic: true` with an `atomic_rationale`, or `is_atomic: false` with `proposed_child_acs`. Each proposed child AC must include `title`, `statement`, `success_criteria`, `rationale`, and optional `estimated_chunk_needs`. Child ACs are proposals only; Ouroboros assigns final AC IDs and enforces `max_depth = 5`. |
| `execute_atomic` | One `atomic_execution` artifact containing `deliverable`, `checks_performed`, `claims`, and `completion_notes`. Each claim must reference one or more supplied evidence IDs. File edits, commands, or benchmark findings are described as outputs; Hermes does not apply repository mutations directly inside this contract (`src/ouroboros/rlm/contracts.py:313-459`; `src/ouroboros/rlm/loop.py:1790-1928`). |
| `summarize_chunk` | One `chunk_summary` artifact containing `summary_id`, `source_chunk_ids`, `target_ac_id`, `facts`, `irrelevant_sections`, and `token_estimate_after_summary`. Facts must preserve enough line or span metadata for later synthesis. |
| `synthesize_parent` | One `parent_synthesis` artifact containing `child_result_ids`, `combined_verdict`, `satisfied_criteria`, `unsatisfied_criteria`, `evidence_rollup`, and `recommended_outer_decision`. The recommendation is advisory; Ouroboros applies termination conditions. |

The executable `decompose_ac` serialization contract is defined in
`src/ouroboros/rlm/contracts.py` by `RLMHermesACDecompositionResult` and related
value objects. `to_dict()` returns the trace-persisted JSON object,
`to_json()` emits deterministic compact JSON, and `from_json()` validates the
schema version, mode, echoed AC/RLM IDs when supplied, confidence bounds,
atomic-vs-child proposal consistency, and sibling dependency indices.

Example `decompose_ac` response:

```json
{
  "schema_version": "rlm.hermes.output.v1",
  "mode": "decompose_ac",
  "rlm_node_id": "rlm_node_0007",
  "ac_node_id": "ac_0004",
  "verdict": "decomposed",
  "confidence": 0.82,
  "result": {
    "summary": "The AC should be split into schema and artifact documentation tasks."
  },
  "evidence_references": [
    {
      "chunk_id": "docs:guides/recursive-language-model.md:1-180",
      "source_path": "docs/guides/recursive-language-model.md",
      "start_line": 1,
      "end_line": 180,
      "claim": "The guide already defines the layer model and input envelope."
    }
  ],
  "residual_gaps": [],
  "artifacts": [
    {
      "artifact_type": "decomposition",
      "is_atomic": false,
      "proposed_child_acs": [
        {
          "title": "Define Hermes output schema",
          "statement": "Document the machine-parseable response object Hermes must return.",
          "success_criteria": ["Common fields are named", "Mode-specific payloads are specified"],
          "rationale": "The outer scaffold needs a stable parser contract.",
          "estimated_chunk_needs": ["Existing RLM guide", "Hermes runtime adapter"]
        }
      ]
    }
  ],
  "control": {
    "requires_retry": false,
    "suggested_next_mode": "none",
    "must_not_recurse": false
  }
}
```

### Expected Inner-Layer Artifacts

The inner Hermes layer produces artifacts for Ouroboros to consume; it does not
write directly to the EventStore, mutate the `ACTree`, or call `ooo` commands.
The executable `ooo rlm` loop should persist these artifacts as event metadata
beside the prompt envelope, raw response, parse result, and outer decision.

Expected artifacts:

- **Decomposition proposal:** Proposed child ACs, atomic rationale, local
  confidence, and evidence references. Ouroboros turns accepted proposals into
  AC nodes and links each accepted child to the RLM node that caused it.
- **Atomic execution result:** The bounded deliverable for one atomic AC,
  checks performed, supported claims, residual gaps, and pass/fail verdict.
  Ouroboros records the result on the AC node and decides whether retry or
  synthesis is needed.
- **Chunk summary:** A source-bound summary that carries `summary_id`,
  `source_chunk_ids`, facts, and token estimate. Ouroboros may reuse it in later
  prompts to handle source inputs larger than one Hermes context window
  (`src/ouroboros/rlm/loop.py:1279-1569`,
  `src/ouroboros/rlm/loop.py:1977-2106`, and
  `tests/unit/rlm/test_loop.py:490-810`).
- **Parent synthesis:** A rollup of child results, criteria coverage, evidence
  rollup, unresolved gaps, and advisory next decision. Ouroboros compares the
  synthesis with existing termination conditions before completing the parent.
- **Trace linkage record:** Echoed `rlm_node_id`, `ac_node_id`, mode, selected
  chunk IDs, evidence IDs, response hash, Hermes runtime handle when available,
  and causal parent event ID. These fields allow replay to reconstruct both the
  RLM tree and AC tree from EventStore events.
- **Benchmark evidence item:** For dogfooding runs against the Ouroboros
  `src/` tree, Ouroboros builds grounded `RLMBenchmarkSourceEvidence` records
  from selected benchmark source specs, falling back to selected target chunks
  only when fewer than three source files are cited. This artifact contains
  source-evidence claims; Hermes output is not treated as an independent
  benchmark verdict (`src/ouroboros/rlm/loop.py:77-178`,
  `src/ouroboros/rlm/loop.py:1905-2062`, and
  `tests/unit/rlm/test_loop.py:269-377`).

Invalid, non-JSON, ID-mismatched, or schema-incomplete Hermes responses become
outer-layer failure artifacts. Ouroboros may retry the same RLM node with a
stricter prompt, mark the AC as failed, or stop the run according to existing
termination policy.

## Sub-call Modes

The MVP uses explicit modes so trace replay can distinguish why Hermes was
called:

| Mode | Input Owner | Hermes Output | Ouroboros Decision |
| --- | --- | --- | --- |
| `decompose_ac` | Ouroboros selects AC and local evidence | Atomic verdict or proposed child ACs | Add child AC nodes or mark atomic |
| `execute_atomic` | Ouroboros selects atomic AC and chunks | Result, evidence references, residual gaps | Mark AC complete, failed, or retry |
| `summarize_chunk` | Ouroboros selects chunk and target AC | Bounded summary with source span | Attach summary to RLM node context |
| `synthesize_parent` | Ouroboros selects child outputs | Parent-level synthesis | Continue, retry, or complete parent |

Both recursive decomposition and atomic execution are still controlled by
Ouroboros. Hermes provides the language-model judgment for each step; Ouroboros
decides whether to recurse, retry, stop, or record failure.

## Outer Scaffold Contract

The outer Ouroboros layer is responsible for the executable RLM loop:

- Expose the MVP only through a new `ooo rlm` command.
  (Implemented by `src/ouroboros/cli/main.py:17-55` and
  `src/ouroboros/cli/commands/rlm.py:62-153`; verified by
  `tests/unit/cli/test_main.py:138-180`.)
- Reuse the existing ambiguity threshold of `<= 0.2` before execution.
  (Implemented by `src/ouroboros/rlm/loop.py:43-61` and
  `src/ouroboros/rlm/loop.py:2072-2095`.)
- Reuse `ACTree` and keep `max_depth = 5`.
  (Implemented by `src/ouroboros/core/ac_tree.py:214-289` and
  `src/ouroboros/rlm/loop.py:341-379`.)
- Represent RLM calls as a separate traceable tree whose nodes link to AC node
  IDs.
  (Implemented by `src/ouroboros/rlm/loop.py:320-379`,
  `src/ouroboros/rlm/loop.py:507-580`,
  `src/ouroboros/rlm/trace.py:124-245`, and
  `src/ouroboros/rlm/trace.py:247-323`; verified by
  `tests/unit/rlm/test_trace.py:523-612`.)
- Persist enough event data to reconstruct both the RLM tree and the AC tree.
  (Implemented by `src/ouroboros/rlm/trace.py:875-1040` and
  `src/ouroboros/persistence/event_store.py:239-372`; verified by
  `tests/unit/rlm/test_trace.py:473-612` and
  `tests/unit/rlm/test_trace.py:716-799`.)
- Reuse existing termination conditions where applicable, including depth,
  failure, cancellation, and convergence-style stop signals.
  (Implemented by `src/ouroboros/rlm/loop.py:398-505`,
  `src/ouroboros/rlm/loop.py:649-713`, and verified by
  `tests/unit/rlm/test_loop.py:2488-2624`.)
- Dogfood the benchmark against the Ouroboros `src/` tree with grounded claims
  from at least three source files.
  (Implemented by `src/ouroboros/rlm/benchmark.py:74-126`,
  `src/ouroboros/rlm/loop.py:77-178`,
  `src/ouroboros/rlm/loop.py:1905-2062`; verified by
  `tests/unit/rlm/test_loop.py:269-344`.)

### Benchmark Claim Grounding Inventory

This inventory lists only benchmark claims supported by the current
implementation, tests, or built-in benchmark fixture. Inline citations use
repository-relative `file:start-end` spans. Claims that would require a richer
evaluator, such as computed `pass`/`partial`/`fail` verdicts, full AC coverage
metrics, RLM call tree rendering, or a complete trace-replay scorecard, are
explicitly out of scope for this MVP benchmark artifact.

| Claim ID | Supported current claim | Inline citation(s) | Qualification |
| --- | --- | --- | --- |
| `RLM-BPC-001` | The `rlm` command is registered as a standalone Typer command; command execution constructs `RLMRunConfig` and dispatches to `run_rlm_loop()` or `run_rlm_benchmark()`, while `run_rlm_loop()` reports that run/evolve command paths were not invoked. | `src/ouroboros/cli/main.py:17-55`; `src/ouroboros/cli/commands/rlm.py:30-55`; `src/ouroboros/cli/commands/rlm.py:62-153`; `src/ouroboros/rlm/loop.py:2952-3016` | Code-path claim only; no runtime syscall monitor is claimed. |
| `RLM-BPC-002` | Hermes is the inner worker reached through `AgentRuntime.execute_task_to_result()` with the RLM no-recursion system prompt, and the Hermes adapter uses the existing `hermes chat -Q --source tool` path. | `src/ouroboros/rlm/loop.py:57-61`; `src/ouroboros/rlm/loop.py:2430-2500`; `src/ouroboros/orchestrator/hermes_runtime.py:394-453`; `src/ouroboros/orchestrator/hermes_runtime.py:821-881` | No separate RLM REPL is claimed. |
| `RLM-BPC-003` | RLM guardrails define AC max depth `5`, ambiguity threshold `0.2`, stable root RLM/AC IDs, and config validation before execution. | `src/ouroboros/rlm/loop.py:43-61`; `src/ouroboros/rlm/loop.py:341-379`; `src/ouroboros/rlm/loop.py:2072-2095`; `src/ouroboros/core/ac_tree.py:214-289` | The benchmark observes configured bounds, not a proof of every future recursion path. |
| `RLM-BPC-004` | Source targets are chunked into bounded line spans with stable chunk IDs, token estimates, and a configured maximum chunk count. | `src/ouroboros/rlm/loop.py:2111-2241`; `tests/unit/rlm/test_loop.py:348-377`; `tests/unit/rlm/test_loop.py:585-647` | Chunking rules are implementation-defined within the RLM MVP. |
| `RLM-BPC-005` | Multi-chunk atomic execution creates chunk-level RLM/AC child nodes, records child results, and rolls those child results into a parent synthesis call. | `src/ouroboros/rlm/loop.py:507-580`; `src/ouroboros/rlm/loop.py:2755-2876`; `src/ouroboros/rlm/loop.py:2878-2949`; `tests/unit/rlm/test_loop.py:585-647`; `tests/unit/rlm/test_loop.py:713-872`; `tests/unit/rlm/test_loop.py:2541-2624` | This supports the current chunk-recursive path; it is not a general context-window optimizer claim. |
| `RLM-BPC-006` | EventStore-backed RLM traces persist Hermes call lifecycle records with RLM node IDs, AC node IDs, selected chunk IDs, generated child AC IDs, and causal links. | `src/ouroboros/rlm/trace.py:124-245`; `src/ouroboros/rlm/trace.py:247-323`; `src/ouroboros/rlm/trace.py:875-1040`; `src/ouroboros/rlm/loop.py:2430-2605`; `src/ouroboros/persistence/event_store.py:239-372`; `tests/unit/rlm/test_trace.py:473-612`; `tests/unit/rlm/test_trace.py:716-799` | Supports replay of persisted links; no full trace scorecard is claimed. |
| `RLM-BPC-007` | The dogfood benchmark is named `rlm-mvp-src-dogfood-v1`, targets the repository `src` tree, and asks for evidence from at least three source files. | `src/ouroboros/rlm/benchmark.py:8-126`; `src/ouroboros/rlm/benchmark.py:146-215`; `tests/unit/rlm/test_benchmark.py:24-46`; `tests/unit/rlm/test_benchmark.py:71-84` | Fixture-definition claim. |
| `RLM-BPC-008` | Benchmark source-evidence records are generated from explicit source specs, serialized as `source_evidence`, and rendered in a compact Markdown `Source Evidence` table. | `src/ouroboros/rlm/loop.py:77-178`; `src/ouroboros/rlm/loop.py:1787-1834`; `src/ouroboros/rlm/loop.py:1905-2062`; `tests/unit/rlm/test_loop.py:269-344` | These are repository source claims, not Hermes-computed verdicts. |
| `RLM-BPC-009` | Benchmark citations are limited to source paths supplied as selected target chunks; fallback citations use selected chunks until at least three distinct source files are cited. | `src/ouroboros/rlm/loop.py:1905-1975`; `src/ouroboros/rlm/loop.py:2029-2062`; `tests/unit/rlm/test_loop.py:348-377` | Applies to the current built-in benchmark output path. |
| `RLM-BPC-010` | The Wonder/Reflect ontology migration item is a benchmark question and prompt-context requirement; the current RLM benchmark does not claim separate Wonder/Reflect trace verification. | `src/ouroboros/rlm/benchmark.py:128-144`; `tests/unit/rlm/test_benchmark.py:49-68`; `src/ouroboros/evolution/wonder.py:141-236`; `src/ouroboros/evolution/reflect.py:49-80`; `src/ouroboros/evolution/loop.py:1032-1116`; `src/ouroboros/evolution/loop.py:1118-1243` | Question coverage only, not ontology-change trace validation. |
| `RLM-BPC-011` | The current benchmark output shape is compact: benchmark ID, schema version, source evidence, cited source-file count, generated RLM tree depth, and Markdown report. | `src/ouroboros/rlm/loop.py:1787-1834`; `src/ouroboros/rlm/loop.py:1978-2062`; `tests/unit/rlm/test_loop.py:269-325` | It omits computed verdicts, AC coverage, full RLM/AC tree rendering, and residual-gap scorecards. |

The benchmark sections below are restatements or operator-facing groupings of
the inventory above. No additional benchmark claim should be introduced there
without adding a new `RLM-BPC-*` row and inline code citation here.

| Later section | Benchmark claim coverage |
| --- | --- |
| `MVP Benchmark Scenario` / `Invocation` | `RLM-BPC-001`, `RLM-BPC-003`, `RLM-BPC-007` |
| `Benchmark Prompt` | `RLM-BPC-001`, `RLM-BPC-002`, `RLM-BPC-003`, `RLM-BPC-006`, `RLM-BPC-007`, `RLM-BPC-010` |
| `Required Source Evidence` | `RLM-BPC-001`, `RLM-BPC-002`, `RLM-BPC-003`, `RLM-BPC-004`, `RLM-BPC-005`, `RLM-BPC-006`, `RLM-BPC-010` |
| `Current Observable Checks` / `Current Verification Criteria` | `RLM-BPC-001`, `RLM-BPC-002`, `RLM-BPC-003`, `RLM-BPC-005`, `RLM-BPC-008`, `RLM-BPC-009` |
| `Current Output Artifact` / `Current Evaluation Output` | `RLM-BPC-008`, `RLM-BPC-009`, `RLM-BPC-011` |

## MVP Benchmark Scenario

The MVP benchmark is a dogfood run named `rlm-mvp-src-dogfood-v1` (defined in
`src/ouroboros/rlm/benchmark.py:8-126`). Its current purpose is to exercise the
isolated `ooo rlm` path against the Ouroboros source tree, embed benchmark
questions in Hermes prompt context, and emit grounded source-evidence claims in
the compact benchmark artifact (`src/ouroboros/rlm/benchmark.py:146-215`;
`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1905-1975`;
`src/ouroboros/rlm/loop.py:1978-2062`; verified by
`tests/unit/rlm/test_loop.py:269-344` and
`tests/unit/rlm/test_benchmark.py:87-152`).

### Invocation

The benchmark uses the repository source tree as the default target (defined in
`src/ouroboros/rlm/benchmark.py:99-126` and wired through
`src/ouroboros/cli/commands/rlm.py:53-60`):

```bash
ooo rlm src
```

The benchmark target corpus is `ouroboros-src`: root `src`, package include
pattern `src/ouroboros/**/*.py`, and the required evidence files listed below
(defined in `src/ouroboros/rlm/benchmark.py:99-126` and verified by
`tests/unit/rlm/test_benchmark.py:24-38`).

The terminal equivalent is:

```bash
uv run ouroboros rlm src --debug
```

The command must enter only the `ooo rlm` path. It must not delegate to
`ooo run`, `ooo evolve`, `ouroboros run`, or `ouroboros evolve` (implemented by
`src/ouroboros/cli/main.py:17-55`, `src/ouroboros/cli/commands/rlm.py:27-168`,
and `src/ouroboros/rlm/loop.py:2952-3016`). The benchmark starts with
`ambiguity_score <= 0.2`, `ambiguity_threshold = 0.2`, and `max_ac_depth = 5`
(implemented by `src/ouroboros/rlm/loop.py:43-61`,
`src/ouroboros/rlm/loop.py:2072-2095`, and
`src/ouroboros/rlm/loop.py:2329-2382`).

### Benchmark Prompt

The root benchmark AC is:

> Analyze the Ouroboros `src/` tree and report whether the current RLM MVP
> satisfies the dual-layer recursive language model constraints, using only
> supplied source chunks and citing evidence from at least three source files.

This root benchmark AC is defined in `src/ouroboros/rlm/benchmark.py:93-97`.
The benchmark fixture supplies question prompts to the Hermes context. In the
current MVP they are prompt requirements, not separately materialized AC tree
nodes (`src/ouroboros/rlm/benchmark.py:128-215`; verified by
`tests/unit/rlm/test_benchmark.py:71-84` and
`tests/unit/rlm/test_benchmark.py:87-152`):

| Sub-AC | Required evidence | Supporting code citation |
| --- | --- | --- |
| Command isolation | Ask Hermes to check that `rlm` is registered as its own command and constructs `RLMRunConfig` instead of invoking run/evolve code. | `src/ouroboros/rlm/benchmark.py:153-164`; `src/ouroboros/cli/main.py:17-55`; `src/ouroboros/cli/commands/rlm.py:27-168` |
| Hermes inner-LM boundary | Ask Hermes to check that RLM uses `HermesCliRuntime` through `AgentRuntime.execute_task_to_result()` and passes a no-`ooo` system prompt. | `src/ouroboros/rlm/benchmark.py:165-177`; `src/ouroboros/rlm/loop.py:57-61`; `src/ouroboros/rlm/loop.py:2430-2500`; `src/ouroboros/orchestrator/hermes_runtime.py:394-453` |
| AC and RLM recursion guardrails | Ask Hermes to check max depth, ambiguity threshold, RLM node IDs, AC node IDs, and chunk child calls. | `src/ouroboros/rlm/benchmark.py:178-189`; `src/ouroboros/rlm/loop.py:43-61`; `src/ouroboros/rlm/loop.py:507-580`; `src/ouroboros/core/ac_tree.py:214-289` |
| Trace and replay readiness | Ask Hermes to check selected chunk IDs, call IDs, parent call IDs, child results, and AC/RLM linkage. | `src/ouroboros/rlm/benchmark.py:190-203`; `src/ouroboros/rlm/trace.py:124-245`; `src/ouroboros/rlm/trace.py:247-323`; `src/ouroboros/rlm/trace.py:681-761` |
| Context scaling | Ask Hermes to check that larger targets are split into bounded chunks and synthesized by a parent RLM node. | `src/ouroboros/rlm/benchmark.py:204-212`; `src/ouroboros/rlm/loop.py:2111-2241`; `src/ouroboros/rlm/loop.py:2755-2876`; `src/ouroboros/rlm/loop.py:2878-2949` |
| Wonder/Reflect generation-level ontology migration | Ask whether Wonder/Reflect preserve generation-level ontology migration. This is a benchmark question, not a claim that RLM itself traces Wonder/Reflect ontology changes. | `src/ouroboros/rlm/benchmark.py:128-144`; `tests/unit/rlm/test_benchmark.py:49-68`; `src/ouroboros/evolution/wonder.py:141-236`; `src/ouroboros/evolution/reflect.py:49-80`; `src/ouroboros/evolution/loop.py:1032-1116`; `src/ouroboros/evolution/loop.py:1118-1243` |

### Required Source Evidence

For the built-in `src` dogfood target, the benchmark report emits evidence for
at least three selected source files when the repository source specs are
available. These files are the current grounded evidence set for
`rlm-mvp-src-dogfood-v1`
(`src/ouroboros/rlm/loop.py:77-178`, `src/ouroboros/rlm/loop.py:1905-1975`,
`src/ouroboros/rlm/loop.py:1978-2062`, and
`tests/unit/rlm/test_loop.py:269-344`):

| Source file | Grounded claim emitted by benchmark evidence |
| --- | --- |
| `src/ouroboros/cli/commands/rlm.py:27-168` | The `rlm` command defaults to target `src`, accepts RLM guardrail options, builds `RLMRunConfig`, and dispatches only to the isolated RLM loop or benchmark helper. |
| `src/ouroboros/cli/main.py:17-55` | The top-level CLI imports `rlm` and registers `app.command(name="rlm")(rlm.command)` separately from existing `run` command groups. |
| `src/ouroboros/rlm/loop.py:43-61`; `src/ouroboros/rlm/loop.py:2329-2382` | The RLM loop defines the AC depth cap `5`, ambiguity threshold `0.2`, root RLM/AC IDs, and a Hermes system prompt that forbids Ouroboros or `ooo` recursion. |
| `src/ouroboros/rlm/loop.py:2111-2241` | Source targets are read into bounded line chunks with stable chunk IDs, spans, token estimates, and a maximum chunk count. |
| `src/ouroboros/rlm/loop.py:2243-2420`; `src/ouroboros/rlm/loop.py:2430-2500` | The prompt envelope carries run, RLM node, AC node, constraints, selected chunks, trace IDs, and the Hermes sub-call through `execute_task_to_result()`. |
| `src/ouroboros/rlm/loop.py:507-580`; `src/ouroboros/rlm/loop.py:2755-2876`; `src/ouroboros/rlm/loop.py:2878-2949` | Multi-chunk atomic execution creates chunk-level RLM/AC child IDs, records child results, and schedules parent synthesis. |
| `src/ouroboros/orchestrator/hermes_runtime.py:394-453` | Hermes execution uses the existing Hermes CLI/RPC adapter path, including skill dispatch interception and `hermes chat -Q --source tool`. |
| `src/ouroboros/orchestrator/hermes_runtime.py:821-881` | `execute_task_to_result()` collects Hermes messages into the standard `TaskResult` consumed by the RLM loop. |
| `src/ouroboros/core/ac_tree.py:214-289` | `ACTree` stores a max depth of `5` and rejects nodes whose depth exceeds the configured limit. |
| `src/ouroboros/persistence/event_store.py:239-372` | EventStore appends events transactionally and replays aggregate events in deterministic order, which is the persistence basis for RLM trace replay. |
| `src/ouroboros/evolution/wonder.py:141-236` | Wonder prompt construction supplies seed scope, current ontology, evaluation summary, execution output, and recent lineage. |
| `src/ouroboros/evolution/reflect.py:49-80` | Reflect output carries next-generation acceptance criteria, ontology mutations, and reasoning for benchmark inspection. |
| `src/ouroboros/evolution/loop.py:1032-1116`; `src/ouroboros/evolution/loop.py:1118-1243` | The evolutionary loop runs Wonder then Reflect for generation 2+, emits phase changes, generates a next seed, and computes ontology deltas across generations. |

Additional files may be cited only when they are selected target chunks.
Unsupported benchmark claims should be removed or reworded before release
(`src/ouroboros/rlm/loop.py:1905-1975`; verified by
`tests/unit/rlm/test_loop.py:348-377`).

### Current Observable Checks

The current MVP benchmark artifact supports these artifact-level checks without
claiming a full evaluator scorecard or independent Hermes-authored verdict
(`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1978-2062`; verified by
`tests/unit/rlm/test_loop.py:269-344`):

| Check | Current output or trace source | Supporting code citation |
| --- | --- | --- |
| Command isolation | `ooo rlm` constructs `RLMRunConfig` and calls only the isolated RLM loop or benchmark helper. | `src/ouroboros/cli/commands/rlm.py:27-168`; `src/ouroboros/rlm/loop.py:2952-3040` |
| Guardrails | `RLMRunResult` and the Markdown report include configured AC max depth and ambiguity threshold. | `src/ouroboros/rlm/loop.py:43-61`; `src/ouroboros/rlm/loop.py:1978-2062` |
| Hermes boundary | Non-dry runs call `execute_task_to_result()` with no tools and the RLM system prompt. | `src/ouroboros/rlm/loop.py:2430-2500`; `src/ouroboros/orchestrator/hermes_runtime.py:394-453` |
| Evidence grounding | `RLMBenchmarkOutput` includes `source_evidence`, `cited_source_file_count`, and a Markdown source-evidence table. | `src/ouroboros/rlm/loop.py:1787-1834`; `src/ouroboros/rlm/loop.py:1905-1975`; `src/ouroboros/rlm/loop.py:1978-2062`; `tests/unit/rlm/test_loop.py:269-344` |
| Context scaling | When selected context spans multiple chunks, child RLM/AC nodes are scheduled and summarized by a parent synthesis call. | `src/ouroboros/rlm/loop.py:507-580`; `src/ouroboros/rlm/loop.py:2755-2876`; `src/ouroboros/rlm/loop.py:2878-2949`; `tests/unit/rlm/test_loop.py:585-647`; `tests/unit/rlm/test_loop.py:713-872` |

### Expected Recursive Shape

For the current executable MVP, a normal `src/` dogfood run embeds benchmark
questions in the prompt context and executes the selected source chunks through
atomic chunk recursion. It does not currently materialize each benchmark question
as a separate AC child node.

```text
rlm_node_root / rlm_ac_root
  execute_atomic -> one Hermes call when selected context fits one bounded chunk
  execute_atomic children -> one Hermes call per bounded chunk when context is larger
  synthesize_parent -> one root synthesis that consumes ordered child results
```

The chunk-recursive path creates child RLM/AC IDs, sends each child exactly one
bounded chunk, records each child result, and rolls the ordered results into
parent synthesis (`src/ouroboros/rlm/loop.py:507-580`;
`src/ouroboros/rlm/loop.py:2755-2876`;
`src/ouroboros/rlm/loop.py:2878-2949`; verified by
`tests/unit/rlm/test_loop.py:585-647` and
`tests/unit/rlm/test_loop.py:713-872`). Hermes still cannot create AC nodes, call
an `ooo` command, or decide global completion by itself
(`src/ouroboros/rlm/loop.py:57-61`; `src/ouroboros/rlm/loop.py:321-379`;
`src/ouroboros/rlm/loop.py:666-713`).

### Current Verification Criteria

The current benchmark artifact is grounded when these implementation-backed
conditions hold:

- The run starts through `ooo rlm` or `uv run ouroboros rlm src`; the command
  constructs `RLMRunConfig` and dispatches only to `run_rlm_loop()` or
  `run_rlm_benchmark()` (`src/ouroboros/cli/commands/rlm.py:27-168`;
  `src/ouroboros/rlm/loop.py:2952-3040`).
- Guardrail values are bounded by `MAX_RLM_AC_TREE_DEPTH = 5` and
  `MAX_RLM_AMBIGUITY_THRESHOLD = 0.2`
  (`src/ouroboros/rlm/loop.py:43-61`; `src/ouroboros/rlm/loop.py:2072-2095`).
- Hermes calls use `execute_task_to_result()` with the RLM no-recursion system
  prompt and the existing Hermes chat tool path
  (`src/ouroboros/rlm/loop.py:2430-2500`;
  `src/ouroboros/orchestrator/hermes_runtime.py:394-453`).
- The benchmark output cites at least three supplied source files when benchmark
  spec files are available, and every built-in evidence spec resolves to a
  concrete repository span (`src/ouroboros/rlm/loop.py:77-178`;
  `src/ouroboros/rlm/loop.py:1905-1975`;
  `src/ouroboros/rlm/loop.py:1978-2062`; verified by
  `tests/unit/rlm/test_loop.py:269-344`).
- Chunk recursion and parent synthesis are observable for multi-chunk targets
  (`src/ouroboros/rlm/loop.py:507-580`; `src/ouroboros/rlm/loop.py:2755-2876`;
  `src/ouroboros/rlm/loop.py:2878-2949`;
  verified by `tests/unit/rlm/test_loop.py:585-647` and
  `tests/unit/rlm/test_loop.py:713-872`).

### Current Output Artifact

The current benchmark artifact is `RLMBenchmarkOutput`. It serializes the schema
version, benchmark ID, source-evidence records, cited source-file count, and a
compact Markdown report with `Benchmark`, `Guardrails`, and `Source Evidence`
sections (`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1978-2062`; verified by
`tests/unit/rlm/test_loop.py:269-325`). It does not currently claim a computed
`pass`/`partial`/`fail` verdict, AC coverage table, full RLM call tree, full AC
tree, replay scorecard, residual gap table, or follow-up work list.

## Current Evaluation Output

The current `ooo rlm` benchmark output has one operator-readable Markdown report
and one machine-readable `RLMBenchmarkOutput` dictionary. Both describe the same
grounded source-evidence artifact (`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1978-2062`; verified by
`tests/unit/rlm/test_loop.py:269-325`).

### Human-Readable Report Format

The report is Markdown with these current sections in order:

1. `Benchmark`: benchmark ID, invocation, working directory, and target.
2. `Guardrails`: ambiguity threshold, configured AC max depth, and observed
   Hermes sub-call count.
3. `Source Evidence`: cited source-file count and a table of evidence ID,
   source file, claim categories, and grounded claim.

The report avoids unsupported narrative claims by rendering only
`RLMBenchmarkSourceEvidence` records generated from benchmark specs or selected
fallback chunks (`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1905-1975`;
`src/ouroboros/rlm/loop.py:1978-2062`; verified by
`tests/unit/rlm/test_loop.py:269-344`).

### Machine-Readable Summary Format

The summary is the `RLMBenchmarkOutput.to_dict()` shape. It is intentionally
compact and evidence-focused (`src/ouroboros/rlm/loop.py:1787-1834`).

```json
{
  "schema_version": "rlm.evaluation.output.v1",
  "benchmark_id": "rlm-mvp-src-dogfood-v1",
  "source_evidence": [
    {
      "evidence_id": "src/ouroboros/rlm/loop.py:43-61",
      "source_path": "src/ouroboros/rlm/loop.py",
      "start_line": 43,
      "end_line": 61,
      "claim_categories": ["AC/RLM traceability", "Guardrails"],
      "claim": "The RLM loop defines the depth cap, ambiguity threshold, root RLM/AC IDs, and a Hermes boundary prompt that forbids recursive Ouroboros calls."
    }
  ],
  "cited_source_file_count": 3,
  "report_markdown": "# RLM MVP Benchmark\n..."
}
```

### Evaluation Verdict Rules

The current MVP does not compute a benchmark-level `pass`, `partial`, or `fail`
verdict. A future evaluator may derive those statuses from measured command
isolation, guardrails, trace replay, context scaling, and source-evidence
coverage, but that richer verdict logic is not part of the present
`RLMBenchmarkOutput` contract (`src/ouroboros/rlm/loop.py:1787-1834`;
`src/ouroboros/rlm/loop.py:1978-2062`).

## Trace Requirements

The recursive execution trace is an EventStore event stream using
`rlm.trace.v1` payloads. Each persisted record is a normal Ouroboros
`BaseEvent`: `type` names the transition, `aggregate_type` is `rlm_run`,
`aggregate_id` is the `rlm_run_id`, `timestamp` is supplied by the EventStore,
and `data` contains the RLM-specific replay payload. Hermes never writes these
events directly; Ouroboros writes them before and after each inner sub-call
(`src/ouroboros/rlm/trace.py:124-245`, `src/ouroboros/rlm/trace.py:247-323`,
`src/ouroboros/rlm/trace.py:681-761`,
and `src/ouroboros/rlm/loop.py:2296-2471`; verified by
`tests/unit/rlm/test_trace.py:473-612` and `tests/unit/rlm/test_loop.py:888-1120`).

### Recursive Trace Schema

Every `rlm.trace.v1` event payload has this common shape:

```json
{
  "schema_version": "rlm.trace.v1",
  "trace_id": "trace_0012",
  "rlm_run_id": "rlm_generation_0",
  "generation_id": "rlm_generation_0",
  "sequence": 12,
  "phase": "outer | inner",
  "step": "node_scheduled",
  "mode": "decompose_ac | execute_atomic | summarize_chunk | synthesize_parent | none",
  "subcall_id": "rlm_subcall_0007",
  "parent_trace_id": "trace_0011",
  "causal_parent_event_id": "event_0011",
  "rlm_node": {
    "id": "rlm_node_0007",
    "parent_id": "rlm_node_0003",
    "depth": 2,
    "status_before": "queued",
    "status_after": "context_bound",
    "child_ids": ["rlm_node_0008"]
  },
  "ac_node": {
    "id": "ac_0004",
    "parent_id": "ac_0002",
    "depth": 2,
    "max_depth": 5,
    "status_before": "pending",
    "status_after": "decomposed",
    "child_ids": ["ac_0005"]
  },
  "context": {
    "selected_chunk_ids": ["src/ouroboros/rlm/loop.py:1-80"],
    "summary_ids": ["summary_0002"],
    "child_result_ids": ["result_0006"],
    "token_estimate": 4200,
    "truncated": false
  },
  "hermes": {
    "call_id": "rlm_call_0007",
    "subcall_id": "rlm_subcall_0007",
    "parent_call_id": "rlm_call_0003",
    "runtime": "hermes",
    "resume_handle_id": "optional-hermes-runtime-handle",
    "prompt": "Rendered Hermes input envelope.",
    "completion": "Raw Hermes final message.",
    "prompt_hash": "sha256:...",
    "response_hash": "sha256:...",
    "depth": 2,
    "exit_code": 0
  },
  "decision": {
    "local_verdict": "decomposed",
    "outer_decision": "accepted_children",
    "termination_reason": null,
    "retry_count": 0,
    "failure_class": null
  },
  "artifacts": {
    "artifact_ids": ["artifact_0009"],
    "evidence_references": [
      {
        "chunk_id": "src/ouroboros/rlm/loop.py:1-80",
        "source_path": "src/ouroboros/rlm/loop.py",
        "start_line": 1,
        "end_line": 80,
        "claim": "Grounded claim recorded from Hermes output."
      }
    ],
    "residual_gap_ids": []
  },
  "replay": {
    "creates_rlm_node_ids": ["rlm_node_0008"],
    "creates_ac_node_ids": ["ac_0005"],
    "generated_child_ac_node_ids": ["ac_0005"],
    "updates_rlm_node_ids": ["rlm_node_0007"],
    "updates_ac_node_ids": ["ac_0004"],
    "links": [
      {
        "from_type": "rlm_node",
        "from_id": "rlm_node_0007",
        "to_type": "ac_node",
        "to_id": "ac_0004",
        "relationship": "executes"
      }
    ]
  }
}
```

Common field rules:

- `sequence` is monotonic within the RLM run, so replay can order events even
  when timestamps are close.
- `trace_id`, `subcall_id`, `parent_trace_id`, and
  `causal_parent_event_id` are stable causal links. They identify the current
  trace record, the inner sub-call, the parent trace record, and the EventStore
  event that led to the current transition.
- `phase` distinguishes Ouroboros outer control transitions from Hermes inner
  sub-call transitions.
- `step` is the replay discriminator. It must be specific enough to rebuild
  scheduling, context binding, Hermes invocation, validation, commit, retry, and
  termination decisions without reading free-form text.
- `rlm_node` and `ac_node` always carry IDs, parent IDs, depth, and before/after
  status when the event touches a node. These fields reconstruct both trees.
  Persisted child AC node records also carry `originating_subcall_trace_id`
  when an RLM/Hermes sub-call produced the child, so replay can join the AC node
  back to the exact trace record that created it.
- `context` records exactly what Ouroboros supplied to Hermes: chunk IDs,
  summaries, child results, token estimate, and truncation state.
- `hermes` is present for inner-call events and contains runtime handle data,
  call ancestry, the raw sub-call `prompt`, raw `completion`, recursion `depth`,
  prompt/response hashes, and exit status. The MVP stores prompt and completion
  on the replayable trace record; later storage layers may additionally move
  large raw artifacts behind IDs while keeping these fields available.
- `decision` records the local Hermes verdict and the authoritative Ouroboros
  decision derived from it.
- `artifacts` records accepted output IDs, evidence references, and residual
  gaps. Evidence references must point only to supplied context.
- `replay` records created nodes, generated child AC node IDs, updated nodes,
  and causal links so a replay consumer can reconstruct the RLM tree, AC tree,
  and cross-tree relationships.

### Trace Event Types

The MVP uses dot-notation event types under the RLM aggregate:

| Event type | Phase | Purpose |
| --- | --- | --- |
| `rlm.run.started` | Outer | Record command arguments, ambiguity gate result, root RLM node, root AC node, depth limit `5`, and initial scheduler state. |
| `rlm.node.scheduled` | Outer | Record the selected RLM node, linked AC node, chosen mode, retry count, and causal parent event. |
| `rlm.context.bound` | Outer | Record selected chunks, summaries, child results, token estimates, truncation, and output contract before Hermes sees the prompt. |
| `rlm.hermes.call.started` | Inner | Record Hermes call ID, parent call ID, runtime name, resume handle when available, prompt, prompt hash, selected chunk IDs, depth, and mode. |
| `rlm.hermes.call.completed` | Inner | Record completion, exit code, success flag, response hash, runtime handle, elapsed time when available, and adapter error details if present. |
| `rlm.hermes.response.validated` | Outer | Record schema version, echoed IDs, confidence, local verdict, evidence reference validation, residual gaps, and validation errors. |
| `rlm.state.committed` | Outer | Record accepted AC/RLM state changes, created child IDs, artifact IDs, evidence rollups, and the outer decision. |
| `rlm.node.terminal` | Outer | Record final node status, completion/failure reason, retry exhaustion state, and linked artifact or gap IDs. |
| `rlm.run.finished` | Outer | Record root verdict, termination reason, final artifact IDs, total Hermes calls, total AC/RLM nodes, and unresolved gaps. |

### Fields Captured At Each Inner And Outer Step

| Step | Phase | Fields captured |
| --- | --- | --- |
| Run start | Outer | `rlm_run_id`, command target, target kind, `cwd`, ambiguity score, ambiguity threshold `0.2`, `max_ac_depth = 5`, root `rlm_node.id`, root `ac_node.id`, initial run status, and scheduler policy. |
| Node scheduling | Outer | Active `rlm_node.id`, `rlm_node.parent_id`, `rlm_node.depth`, linked `ac_node.id`, `ac_node.parent_id`, `ac_node.depth`, selected mode, retry count, previous event ID, and current termination guardrail snapshot. |
| Context binding | Outer | Selected chunk IDs, source paths, line spans, summaries, child result IDs, evidence budget, token estimate, truncation flag, output contract version, prompt hash preview input, and causal parent event ID. |
| Hermes call start | Inner | Hermes `call_id`, `parent_call_id`, runtime adapter name, mode, echoed RLM/AC IDs, rendered prompt, prompt hash, selected chunk IDs, depth, resume handle ID when available, and system-prompt policy hash when available. |
| Hermes call completion | Inner | `call_id`, completion, exit code, success flag, response hash, final message hash or artifact ID, runtime handle ID, elapsed time when available, and adapter/provider error class when the call fails. |
| Response validation | Outer | Parsed schema version, echoed mode, echoed `rlm_node_id`, echoed `ac_node_id`, local verdict, confidence, artifact type, evidence reference validation result, residual gaps, boundary-violation flag, and validation error messages. |
| Decomposition commit | Outer | Accepted or rejected child AC proposals, created `ac_node.child_ids`, created `rlm_node.child_ids`, sibling dependency links, updated parent AC status, updated parent RLM status, depth-limit decision, artifact IDs, and evidence rollup. |
| Atomic execution commit | Outer | Atomic result artifact ID, checks performed, claims, evidence references, pass/fail/partial decision, residual gaps, updated AC status, updated RLM status, retry or synthesis scheduling decision, and causal Hermes `call_id`. |
| Chunk recursion commit | Outer | Chunk RLM node IDs, chunk AC node IDs when used, chunk source spans, chunk result IDs, parent synthesis call ID, chunk truncation state, and child-to-parent RLM links. |
| Parent synthesis commit | Outer | Child result IDs, combined verdict, satisfied and unsatisfied criteria, evidence rollup, parent artifact ID, updated parent AC/RLM statuses, and next outer decision. |
| Retry or failure | Outer | Failed step, failure class, retry count, retry limit, quarantined artifact ID or raw-response hash, selected recovery mode, branch status, and causal parent event. |
| Node terminal | Outer | Final node status, terminal verdict, termination reason, artifact IDs, residual gap IDs, child IDs, and cross-link from RLM node to AC node. |
| Run finish | Outer | Root AC status, root RLM status, termination reason, total nodes, total Hermes calls, final artifact IDs, benchmark evidence file count when applicable, and unresolved gap summary. |

This keeps Hermes as an inner inference worker while preserving replayability in
the Ouroboros event-sourced scaffold. A trace replay can rebuild the RLM tree
from `rlm_node.parent_id` and `replay.links`, rebuild the AC tree from
`ac_node.parent_id` and `replay.creates_ac_node_ids`, and recover the causal path
from each AC change back to the Hermes call and outer decision that produced it
(`src/ouroboros/rlm/trace.py:246-323`, `src/ouroboros/rlm/trace.py:326-527`,
and `tests/unit/rlm/test_trace.py:523-612`).

### Interpreting Captured Traces

Trace replay is not only an audit log. The MVP treats replay as the primary way
to assess whether recursion is making useful progress, converging, failing
cleanly, or exposing a debuggable defect. A trace interpreter first sorts events
by `sequence`, applies each `replay` mutation to an in-memory RLM tree and AC
tree, then derives run-level signals from node statuses, causal links, artifacts,
residual gaps, and Hermes call outcomes (`src/ouroboros/rlm/trace.py:326-527`,
`src/ouroboros/rlm/trace.py:681-761`, and `tests/unit/rlm/test_trace.py:387-612`).

**Recursion progress** is measured from structural movement and accepted work:

- The active frontier is the set of RLM or AC nodes with queued, scheduled,
  retryable, or context-bound statuses. A shrinking frontier with growing
  terminal nodes indicates progress.
  (State support: `src/ouroboros/rlm/loop.py:166-213` and
  `src/ouroboros/rlm/loop.py:365-473`.)
- Downward recursion is visible when `rlm.state.committed` creates child RLM
  nodes and child AC nodes after `decompose_ac`; upward recursion is visible when
  `synthesize_parent` consumes `child_result_ids` and commits a parent artifact.
  (Decomposition contract support: `src/ouroboros/rlm/contracts.py:313-459`;
  chunk-child and synthesis support: `src/ouroboros/rlm/loop.py:474-547` and
  `src/ouroboros/rlm/loop.py:2638-2706`.)
- Atomic execution progress is visible when `execute_atomic` or
  `summarize_chunk` events cover new `selected_chunk_ids`, create result or
  summary artifacts, and roll those artifacts into a parent synthesis.
  (Trace fields: `src/ouroboros/rlm/trace.py:246-323`; chunk execution:
  `src/ouroboros/rlm/loop.py:2474-2706`; tests:
  `tests/unit/rlm/test_loop.py:490-810`.)
- Depth budget consumption is derived from `ac_node.depth` and `max_depth = 5`.
  A healthy run either resolves nodes before depth `5` or records why a depth-5
  leaf was executed atomically, retried, or failed.
  (Depth support: `src/ouroboros/core/ac_tree.py:214-289` and
  `src/ouroboros/rlm/loop.py:474-547`.)
- Context scaling is visible when large targets produce multiple chunk child
  nodes, each with bounded token estimates and source spans, followed by a
  parent synthesis that references all expected chunk result IDs.
  (Chunking and synthesis support: `src/ouroboros/rlm/loop.py:1977-2106`,
  `src/ouroboros/rlm/loop.py:2474-2706`, and
  `tests/unit/rlm/test_loop.py:490-810`.)

**Convergence** is assessed by comparing the replayed trees with existing
Ouroboros termination conditions. A run is converged only when the root AC and
root RLM node are terminal, no queued or retryable descendants remain, required
child results have been synthesized into their parents, and `rlm.run.finished`
records a completion-style `termination_reason`. Residual gaps do not
automatically prevent convergence, but they must be explicitly classified in the
final artifact: accepted known limitation, unresolved failure, or follow-up work.
Repeated events with the same active node, same prompt hash, same selected
chunks, and no new artifacts or status change indicate non-convergence even if
Hermes continues returning successful responses (`src/ouroboros/rlm/loop.py:566-612`,
`src/ouroboros/rlm/loop.py:2687-2768`, `src/ouroboros/rlm/trace.py:124-245`,
`src/ouroboros/rlm/trace.py:247-323`,
and `tests/unit/rlm/test_loop.py:2417-2554`).

**Failure modes** are interpreted from the first authoritative outer decision
that blocks state mutation or terminates a branch:

- Guardrail failures: ambiguity above `0.2`, attempted AC depth beyond `5`,
  cancellation, or retry exhaustion.
  (Guardrails: `src/ouroboros/rlm/loop.py:43-61`,
  `src/ouroboros/rlm/loop.py:1938-1960`, and
  `src/ouroboros/core/ac_tree.py:214-289`.)
- Contract failures: invalid JSON, schema-version mismatch, echoed RLM/AC ID
  mismatch, unsupported mode, low confidence when confidence is required, or
  missing mode-specific artifact fields.
  (Contracts: `src/ouroboros/rlm/contracts.py:313-459` and
  `tests/unit/rlm/test_contracts.py:59-146`.)
- Evidence failures: citations outside supplied chunks, missing source spans,
  unsupported benchmark claims, or child synthesis that omits required child
  result IDs.
  (Evidence and synthesis: `src/ouroboros/rlm/loop.py:1790-1928`,
  `src/ouroboros/rlm/loop.py:2474-2706`, and
  `tests/unit/rlm/test_loop.py:218-284`.)
- Runtime failures: Hermes adapter error, non-zero exit code, missing response
  hash, timeout, or unavailable resume handle when the runtime promised one.
  (Runtime failure trace support: `src/ouroboros/rlm/loop.py:2369-2440` and
  `tests/unit/rlm/test_loop.py:2241-2340`.)
- Recursion failures: branch explosion without synthesis, repeated
  decomposition of an already atomic node, child creation at depth `5`, or a
  parent marked complete while descendants remain non-terminal.
  (Recursion limits and parent completion: `src/ouroboros/rlm/loop.py:474-547`,
  `src/ouroboros/rlm/loop.py:2638-2706`, and
  `tests/unit/rlm/test_loop.py:2457-2534`.)

**Debugging signals** should point from symptoms back to the smallest causal
event chain. The interpreter follows `causal_parent_event_id`, Hermes
`parent_call_id`, `rlm_node.parent_id`, `ac_node.parent_id`, and `replay.links`
to explain why a node reached its final state. Useful high-signal checks include
sequence gaps, duplicate node IDs, missing cross-links between an RLM node and
its AC node, prompt hash reuse with incompatible context, response hash reuse
with different parsed artifacts, rising retry counts, persistent residual gaps,
unexpected truncation, and any outer `decision.outer_decision` that disagrees
with the Hermes `decision.local_verdict`. These signals make the trace actionable:
the operator can identify the exact node, AC, chunk set, Hermes call, validated
response, and outer commit or rejection that caused the run to advance or stop
(`src/ouroboros/rlm/trace.py:124-245`, `src/ouroboros/rlm/trace.py:247-323`,
`src/ouroboros/rlm/trace.py:326-527`,
and `tests/unit/rlm/test_trace.py:387-612`).
