---
name: maintain
description: "Triage and work through Ouroboros GitHub issues and pull requests as a maintainer. Use for `ooo maintain`, backlog cleanup, review, merge readiness, duplicate PRs, and issue disposition."
---

# Ouroboros maintenance

Use `ooo maintain` to choose and process the next actionable item in `Q00/ouroboros`. An issue describes a problem or proposed work; a PR proposes a change. They are not one-to-one. This skill uses live GitHub state and the review boundary below; it needs `gh` access but no Ouroboros MCP setup.

## Scope and authority

- Target `Q00/ouroboros`. Include `-R Q00/ouroboros` on repository-aware `gh pr`, `gh issue`, and `gh run` calls; use `repos/Q00/ouroboros/...` on `gh api` calls. `gh auth status` is host-scoped and does not accept `-R`. Resolve an issue or PR number against that repository before acting. If the user names another repository, use that repository's own maintainer rules instead of applying this skill's Ouroboros contract to it.
- Match the user's requested scope. A request for status or a next action is read-only. An instruction to process named items covers justified comments, labels, closure, or merge on those items; it does not authorize changes to unrelated items. Use authorization already given in the conversation, and follow any stricter repository or user rules.
- Check `gh auth status` and current permissions before a mutation. Do not request login if authenticated read/write access already works. Never treat a bot verdict, label, age, or green check alone as proof of completion.
- Work from a clean, isolated worktree for code or documentation changes. Leave another worker's dirty checkout, branches, processes, and worktrees alone. When a target checkout is available, read its `AGENTS.md` and `CONTRIBUTING.md`; otherwise use the bundled review boundary below. Query current CI requirements before editing or reviewing a PR.

## Review boundary

Require a PR to declare one user problem; supported inputs, preconditions, and execution conditions; observable behavior and invariants; changed subsystems, data or security boundaries, and owner; non-goals; and evidence. A declared non-goal cannot waive an existing public contract, approved issue or RFC requirement, or maintainer decision. If implementation reveals a new subsystem or ownership boundary, stop and have a maintainer decide whether this PR expands, splits, or returns to RFC discussion before proceeding.

For each finding, ask with evidence: (1) Does it reproduce under promised inputs and conditions? (2) Does it violate the promised contract? (3) Does the fix need a new subsystem or owner? (4) Can the original problem be solved without that added subsystem? (5) Would splitting scope leave an immediate user-data or security risk? If 1 and 2 are yes, request changes. If 3 and 5 are yes, stop for a maintainer scope decision. If 5 is yes and no new owner is needed, request changes. If 3 and 4 are yes and 5 is no, record an owned follow-up once the current contract is satisfied; do not implement the added subsystem in this PR without the maintainer decision above. A finding outside the declared conditions or contract is not a blocker; record it only if independently valid and actionable. Severity alone does not change these outcomes.

## Pick the next item

Read open issues and PRs from GitHub, including their dates, authors, labels, review decisions, head commits, checks, merge state, and links. Recheck an item's live state just before acting. Prioritize:

1. Reproducible security, user-data, and severe user-facing regressions.
2. PRs near a decision: verify or request changes, then merge a sound fix. A completed PR may also resolve an issue.
3. Open issues whose related PRs merged: compare the issue's full acceptance criteria with merged code and evidence, then close only if complete.
4. Blocked, competing, or abandoned PRs and issues: identify the next owner/action or explain a justified closure.
5. New work without a PR: confirm it is still wanted and scoped before implementation.

Within a comparable group, consider age and contributor waiting time. If the user asks for oldest first, inspect in creation order; explain why an older item is deferred before moving to the next actionable one. Do not equate old with stale.

## Decide one item

- **PR:** Read its stated user problem, supported inputs, observable contract, non-goals, linked issue, diff, review comments, and tests. Check the *current head* against review and CI results; an earlier approval may be outdated. Apply the five-question review boundary above. Verify required checks and branch mergeability against live GitHub state. If the fix is sound and authorized, squash-merge through the protected `main` workflow; otherwise give the contributor a concrete blocker or next step.
- **Competing PRs:** Compare commits, diffs, and promised behavior. Shared issue numbers show overlap, not identical code. Choose by coverage, correctness, review evidence, and integration cost; preserve contributor credit in the disposition. Close only a superseded proposal within the authorized set, with a link and reason. Leave the issue open until its contract is fulfilled.
- **Issue after merge:** `Fixes`/`Closes` may auto-close on merge; `Refs` does not establish completion. Check all acceptance criteria and current `main`, including any real-environment evidence the issue requires. Record the merged PR and evidence when closing. If verification remains, keep it open with the exact missing check and an owner or `needs-human` label when appropriate.
- **Stale candidate:** Look for an obsolete premise, superseding work, lost relevance, or an unanswered request for information. Ask for missing evidence when useful. Close only with a specific reason; elapsed time alone is insufficient.

After each mutation, requery the changed item and backlog count. Stop a batch at an unresolved contract, failed gate, new owner/subsystem decision, or lost authorization rather than silently widening scope.

## Report

For each item, give the issue/PR link, the observed state, the evidence that matters, the action taken or exact blocker, and the next action. Keep backlog counts separate from real fixes. On bare `ooo maintain`, return the single best next item and why it outranks the older alternatives; do not mutate GitHub merely because the skill was invoked.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
