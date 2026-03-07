# Issue Quality Policy

This document defines the minimum quality bar for actionable GitHub issues in Ouroboros.

For this project, an issue is not just a note or a chat message. It is a structured work artifact that should be detailed enough for maintainers, contributors, and repository tooling to reason about safely.

## Why this exists

- GitHub is the source of truth for actionable work.
- Good issues reduce review churn, repeated clarification, and off-target PRs.
- Future maintainer tooling depends on issue quality before it can safely help with triage, review, or fixes.

## Core rule

Actionable issues must be written in a structured, implementation-aware format.

For feature work, the expected standard is **PRD-lite**.
For bugs, the expected standard is **fix-ready reproduction quality**.

If an issue is too vague to evaluate, scope, or verify, it is not ready for implementation.

## Feature Issue Standard: PRD-lite

Feature issues should read like a lightweight product requirements document.

### Required sections

- **Problem** — What user or maintainer problem exists?
- **Why now** — Why is this worth doing now?
- **User / Persona** — Who is affected?
- **Current behavior** — What happens today?
- **Desired behavior** — What should happen instead?
- **Constraints** — Technical, UX, cost, policy, or compatibility constraints.
- **Non-goals** — What this issue is explicitly not trying to solve.
- **Acceptance criteria** — Concrete statements that define done.
- **References** — Related issues, PRs, docs, screenshots, or examples.

### Quality bar

A feature issue is considered high quality when:

- The problem is explained more clearly than the solution.
- The desired outcome is testable.
- Scope boundaries are visible.
- A contributor could explain what success looks like without guessing.

### Not acceptable

- “We should support X” without explaining why.
- “Add feature Y” with no user problem or acceptance criteria.
- Open-ended brainstorming that belongs in GitHub Discussions or Discord.

## Bug Issue Standard: Fix-ready

Bug issues should be detailed enough that someone else can reproduce, diagnose, and verify a fix.

### Required sections

- **Summary** — What is broken?
- **Impact** — Why does this matter?
- **Environment** — OS, Python, install method, provider/model, relevant config.
- **Steps to reproduce** — A clear sequence.
- **Expected behavior** — What should happen?
- **Actual behavior** — What happened instead?
- **Logs / output** — Relevant errors or traces.
- **Minimal reproduction** — Smallest seed, command, or config if available.
- **Acceptance criteria for fix** — What will be true once fixed?

### Quality bar

A bug issue is considered high quality when:

- Another person can reproduce it from the issue body.
- The expected and actual behavior are unambiguous.
- The issue includes enough evidence to avoid guesswork.

## Routing rules

Use a GitHub issue when the outcome is actionable.

Prefer GitHub Discussions or Discord when:

- The idea is still exploratory.
- The question is mostly conversational.
- The problem is too vague to define acceptance criteria yet.

The right path is:

1. Discuss vaguely in Discord or Discussions.
2. Turn the idea into a structured issue.
3. Implement from the issue.

## Tooling implications

Repository tooling may use this policy to classify issues as:

- **Ready for discussion**
- **Ready for implementation**
- **Needs info**
- **Better suited for Discussions**

Tooling should treat missing sections as a quality signal, not as proof that the idea is bad.

## Maintainer guidance

When an issue is weak:

- Ask for the minimum missing structure.
- Redirect exploratory ideas to Discussions or Discord.
- Avoid starting implementation from ambiguous issues.

When an issue is strong:

- Apply labels.
- Link related work.
- Treat it as the canonical planning artifact for future PRs.
