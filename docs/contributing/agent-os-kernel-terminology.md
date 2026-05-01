# Agent OS Kernel Terminology

This document locks the kernel-level vocabulary for the Agent OS workstream.
It exists so review comments and stacked PRs use the same words for runtime
context, control decisions, transport, and journaled observability.

## Locked Terms

| Term | Layer | Definition | Naming Rule |
|------|-------|------------|-------------|
| `AgentRuntimeContext` | Runtime context | The per-agent execution envelope. It carries stable runtime dependencies and capabilities such as event storage, runtime backend identity, LLM backend identity, MCP bridge access, and control surfaces. | Use when describing what an agent process is allowed to see or use during a run. |
| `ControlPlane` | Kernel control layer | The top-level conceptual layer for workflow control. It owns the vocabulary, contracts, routing rules, and transport surfaces that tell a runtime what should happen next. | Use for the architecture layer. Do not use `ControlBus` as the top-level concept. |
| `ControlContract` | Control invariant layer | The stable schema and behavioral contract for control messages: allowed directives, terminal semantics, retry budgets, timeout behavior, resume invariants, and required journaling. | Use when a PR defines or changes an invariant that consumers must obey. |
| `Directive` | Control vocabulary | The small, stable action alphabet emitted by decision sites, for example `CONTINUE`, `RETRY`, `WAIT`, `CANCEL`, and `CONVERGE`. | Use for the actual command value. Do not introduce local synonyms when an existing directive carries the meaning. |
| `ControlBus` | Control transport | The in-process publish/subscribe implementation that delivers control events or directives to subscribers. It is a transport inside the `ControlPlane`, not the plane itself. | Use only for the implementation that dispatches messages. |
| `IOJournal` | Observability journal | The durable record of external I/O, including paired LLM and tool call events. It explains what the runtime asked, what came back, and whether a call failed. | Use for the event-backed I/O log. Avoid treating ad hoc logs or stdout as the journal. |

## Layering

The intended relationship is:

```text
AgentRuntimeContext
  -> exposes ControlPlane and runtime capabilities to a running agent

ControlPlane
  -> governed by ControlContract
  -> emits Directive values
  -> may use ControlBus as an in-process transport
  -> persists durable decisions through control.* events

IOJournal
  -> records external LLM/tool I/O alongside control decisions
```

In OS terms, `AgentRuntimeContext` is the process execution envelope,
`ControlPlane` is the kernel control layer, `ControlContract` is the syscall
contract, `Directive` is the command vocabulary, `ControlBus` is one local
delivery mechanism, and `IOJournal` is the replayable black box.

## Control Contract Invariants

The following invariants should guide Agent OS PR review:

- Terminal directives are terminal. After `CANCEL` or `CONVERGE`, a runtime
  must not start new work for the same execution unless a new contract ID or
  explicit rerun is allocated.
- Retry behavior belongs to the control contract. `RETRY` must respect the
  owning retry budget and must be journaled with enough context to explain why
  the retry happened.
- `WAIT` means no forward progress until the awaited input, timeout, or queued
  event arrives.
- Resume must preserve the original execution envelope: runtime backend, LLM
  backend, working directory, MCP bridge capability, and user-selected safety
  options must not silently change.
- Every durable control decision must be reconstructable from the event store.
  Reactive delivery through `ControlBus` is not a substitute for persistence.
- External I/O that influences a decision must be reconstructable from the
  `IOJournal`, using paired request/return events when possible.

## Review Guidance

When reviewing Agent OS PRs:

- Ask whether the change belongs to context, contract, vocabulary, transport,
  or journal.
- Prefer `ControlPlane` for architecture discussions and `ControlBus` only for
  the in-process pub/sub implementation.
- Treat `ControlContract` changes as compatibility-sensitive, even when the
  code diff is small.
- Treat `IOJournal` gaps as observability bugs when they prevent replaying why
  a control decision happened.
- Treat resume drift as a control-contract bug, not a CLI polish issue.
