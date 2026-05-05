# AgentProcess Lifecycle Contract

Issue #518 tracks the move from ad-hoc agent/session orchestration toward a
small `AgentProcess` lifecycle boundary. This document defines the contract the
remaining implementation slices should preserve before durable pause, resume,
cancel, and replay behavior is widened beyond the current lightweight process
primitive.

## Goal

`AgentProcess` should be the owner of one agent-like unit of work. Stage routing
(`interview`, `execute`, `evaluate`, `reflect`) decides which runtime family is
used, but process-local concerns stay inside the process boundary:

- spawn the runtime session or child task;
- observe heartbeats, progress, and terminal status;
- accept cooperative pause/resume/cancel directives;
- persist replayable lifecycle decisions;
- expose enough state for status, TUI, and MCP job tools without depending on a
  specific runtime backend.

## Current Boundary

The repository already has several lifecycle pieces, but they are not yet one
fully durable abstraction:

- Runtime adapters expose backend-neutral session handles.
- EventStore replay reconstructs sessions and lineage state.
- TUI and CLI commands understand paused, resumed, and cancelled execution
  states.
- Evolution and Ralph paths still own substantial loop/session behavior outside
  a reusable process boundary.

The next implementation slices should move behavior into `AgentProcess` without
changing the public runtime profile stage vocabulary.

## Non-goals

The lifecycle boundary should not become a new scheduler, stage router, or
policy table. In particular:

- Do not add per-handler stage keys such as `qa_judge`, `unstuck`, or `ralph` to
  `runtime_profile.stages`.
- Do not hide runtime-specific resume handles in opaque strings when structured
  handles are available.
- Do not make pause/resume best-effort only in memory; state transitions must be
  replayable once a process advertises durable lifecycle support.

## Required Events

Each durable process transition should have one persisted decision event before
side effects are considered complete:

| Transition | Required event purpose |
| --- | --- |
| Spawn | Record process id, parent execution/lineage, runtime backend, and resume handle when available. |
| Pause requested | Record the operator/client request and target process. |
| Paused | Record the point at which the runtime acknowledged a safe pause boundary. |
| Resume requested | Record the requested resume target and source pause event. |
| Resumed | Record the restored runtime handle/session id and resumed phase. |
| Cancel requested | Record reason, requester, and target process. |
| Cancelled / failed / completed | Record terminal status and final diagnostic metadata. |

Replay should be able to answer: what process existed, what it was doing, which
runtime handle can resume it, and why it stopped.

## Slice Plan

1. **State model slice** — define durable process states and projection rules
   from lifecycle events. Acceptance: replaying events reconstructs the same
   process state after spawn, pause, resume, cancel, and terminal transitions.
2. **Runtime adapter slice** — route pause/resume/cancel through the process
   boundary for one execution runtime. Acceptance: no caller mutates runtime
   handles directly when controlling that process.
3. **Evolution/Ralph slice** — let long-running generation loops own child work
   through `AgentProcess` instead of ad-hoc loop variables. Acceptance: a Ralph
   or evolve job can report process state and stop from a persisted directive.
4. **CLI/TUI/MCP slice** — expose process lifecycle status consistently through
   status, cancel, resume, and job tools. Acceptance: the same process id is
   visible across all three surfaces.
5. **Replay/resume slice** — restart after interruption from persisted state.
   Acceptance: tests prove resume does not re-run completed phases and does not
   lose final evaluation/reflect artifacts.

## Risk Controls

- Keep each slice backward compatible with existing EventStore records.
- Prefer additive events before removing current session events.
- Preserve runtime-specific resume handles as data, not as control flow.
- Add projection tests before changing live runtime behavior.
- Keep `AgentProcess` below stage routing so runtime profile semantics stay
  stable.

## Acceptance Checklist

A future #518 implementation PR should state which checklist rows it satisfies:

- [ ] Process state can be reconstructed only from events.
- [ ] Pause/resume/cancel transitions are persisted before or at the same time as
      external side effects.
- [ ] Runtime handles survive process replay.
- [ ] Existing `run workflow --resume` behavior remains compatible.
- [ ] TUI, CLI, and MCP job status agree on terminal state.
- [ ] Ralph/evolution loops do not need client-side lifecycle bookkeeping once
      migrated.
