# RFC - Delegated background-job observer

> Status: Implemented first slice
> Scope: all pollable `Start*` receipts, `ooo run`, `ooo auto`, `ooo ralph`,
> Codex/SOL host UX

## Problem

Ouroboros already executes work in background jobs and parallel runtime
sessions, but the host skill keeps the polling state machine in the main chat:

```text
start job -> wait -> update cursor -> wait -> result -> follow chained job
```

That leaves the main session blocked on repeated MCP calls, consumes its context
with status traffic, and makes a small orchestration model remember cursor,
terminal, and chained-job rules across turns. More implementation workers do
not fix this because the misplaced responsibility is observation, not execution.

## Decision

Every supported background start receipt carries `meta.job_observer`, a
structured `ouroboros.job_observer.v1` contract. A host with independent child
sessions delegates that object to exactly one read-only observer session.

```text
main session              observer session             Ouroboros
    | start job (MCP)             |                         |
    |---------------------------->| background job          |
    | receive job_observer        |                         |
    | spawn observer ------------>| job_wait (exclusive) -->|
    | remain available            | ...                     |
    |                             | job_result ------------>|
    |<----------------------------| compact terminal result |
```

The observer owns the cursor and terminal result retrieval exclusively. The
main session owns the start call, user conversation, and explicit on-demand
status checks. Hosts without child sessions use the declared main-session
fallback. OpenCode plugin mode remains unchanged because the execution itself
already belongs to a plugin child and returns no pollable job ID.

From the user's perspective the parent is event-driven: the observer translates
long-poll responses into only `phase_changed`, `progress_advanced`,
`attention_required`, and `terminal` messages. The parent does not receive or
repeat unchanged heartbeats or raw MCP output.

The initial handoff shows `dashboard_url` when the server returned one, or the
`ouroboros tui open` command otherwise. It also says explicitly that the main
conversation remains available. Safe concurrent work includes requirement
refinement, read-only inspection/review, explicit execution control, and
unrelated work in an isolated worktree. Writes to the active run workspace
require an overlap check because implementation workers may be editing it.

## SOL compatibility

The compatibility target checked for this slice is `gpt-5.6-sol`. The delegated
contract is a better fit for that model shape for four concrete reasons:

1. The main model performs one explicit handoff instead of maintaining a
   multi-turn polling loop.
2. Tool names and arguments are structured in MCP meta, so SOL does not have to
   reconstruct them from prose.
3. Exclusive ownership prevents duplicate polls when main and child sessions
   run concurrently.
4. The observer context is narrow and disposable; progress traffic does not
   displace the user's goal and decisions from the main context.

This does not assume MCP server push. The observer still long-polls, but it does
so outside the conversation that the user is actively using.

## Contract

`meta.job_observer` contains:

- `recommended_host_action="spawn_observer_session"`
- `ownership="exclusive"`
- one `wait` tool call template with a cursor and terminal wake-up
- one `result` tool call template
- `follow_result_job_keys` for evaluation/Ralph/downstream jobs
- `main_session_policy="start_and_on_demand_only"`
- event relay rules for progress, attention, and terminal notifications
- parent-session availability, live-view, and workspace-write policies
- self-contained `instructions` and `restrictions`, so the child does not need
  to reconstruct its state machine from surrounding skill prose
- a sequential fallback for hosts without child sessions

The observer is read-only. It must not edit the repository, cancel/resume the
execution, or spawn implementation workers.

## Tradeoffs

- One concurrency slot is reserved for observation while a job is active.
- Child sessions must inherit or discover the Ouroboros MCP tools.
- Live progress notices are best-effort unless the host supports child-to-parent
  messages; the dashboard remains the richer continuous observer.
- On-demand main-session status is allowed, but it must not take cursor
  ownership or start a second polling loop.

## Acceptance criteria

1. Execute, auto, evaluate, evolve, and Ralph background starts return the
   observer contract for pollable jobs.
2. Codex instructions delegate one observer and forbid duplicate main polling.
3. The initial response exposes dashboard/TUI viewing and says the main
   conversation remains available for safe concurrent work.
4. Observer progress, attention, and terminal events are relayed concisely while
   unchanged heartbeats and raw tool output are suppressed.
5. `ooo run` and `ooo auto` retain a bounded main-session fallback.
6. The observer follows chained evaluation or downstream job IDs before
   returning its final summary.
