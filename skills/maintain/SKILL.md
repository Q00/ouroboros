---
name: maintain
description: "Triage and work through Ouroboros GitHub issues and pull requests as a maintainer. Use for `ooo maintain`, backlog cleanup, review, merge readiness, duplicate PRs, and issue disposition."
---

# Ouroboros maintenance

Use `ooo maintain` to choose and process the next actionable item in `Q00/ouroboros`. An issue describes a problem or proposed work; a PR proposes a change. They are not one-to-one. This skill uses the live GitHub state and the repository's [review boundary contract](../../CONTRIBUTING.md#review-boundary-contract); it needs `gh` access but no Ouroboros MCP setup.

## Scope and authority

- Target `Q00/ouroboros` and include `-R Q00/ouroboros` in every `gh` call. Resolve an issue or PR number against that repository before acting. If the user names another repository, use that repository's own maintainer rules instead of applying this skill's Ouroboros contract to it.
- Match the user's requested scope. A request for status or a next action is read-only. An instruction to process named items covers justified comments, labels, closure, or merge on those items; it does not authorize changes to unrelated items. Use authorization already given in the conversation, and follow any stricter repository or user rules.
- Check `gh auth status` and current permissions before a mutation. Do not request login if authenticated read/write access already works. Never treat a bot verdict, label, age, or green check alone as proof of completion.
- Work from a clean, isolated worktree for code or documentation changes. Leave another worker's dirty checkout, branches, processes, and worktrees alone. Read repository-specific `AGENTS.md`, `CONTRIBUTING.md`, and current CI requirements before editing or reviewing a PR.

## Pick the next item

Read open issues and PRs from GitHub, including their dates, authors, labels, review decisions, head commits, checks, merge state, and links. Recheck an item's live state just before acting. Prioritize:

1. Reproducible security, user-data, and severe user-facing regressions.
2. PRs near a decision: verify or request changes, then merge a sound fix. A completed PR may also resolve an issue.
3. Open issues whose related PRs merged: compare the issue's full acceptance criteria with merged code and evidence, then close only if complete.
4. Blocked, competing, or abandoned PRs and issues: identify the next owner/action or explain a justified closure.
5. New work without a PR: confirm it is still wanted and scoped before implementation.

Within a comparable group, consider age and contributor waiting time. If the user asks for oldest first, inspect in creation order; explain why an older item is deferred before moving to the next actionable one. Do not equate old with stale.

## Decide one item

- **PR:** Read its stated user problem, supported inputs, observable contract, non-goals, linked issue, diff, review comments, and tests. Check the *current head* against review and CI results; an earlier approval may be outdated. Apply the [five-question review rubric](../../CONTRIBUTING.md#five-question-review-rubric). Verify required checks and branch mergeability against live GitHub state. If the fix is sound and authorized, squash-merge through the protected `main` workflow; otherwise give the contributor a concrete blocker or next step.
- **Competing PRs:** Compare commits, diffs, and promised behavior. Shared issue numbers show overlap, not identical code. Choose by coverage, correctness, review evidence, and integration cost; preserve contributor credit in the disposition. Close only a superseded proposal within the authorized set, with a link and reason. Leave the issue open until its contract is fulfilled.
- **Issue after merge:** `Fixes`/`Closes` may auto-close on merge; `Refs` does not establish completion. Check all acceptance criteria and current `main`, including any real-environment evidence the issue requires. Record the merged PR and evidence when closing. If verification remains, keep it open with the exact missing check and an owner or `needs-human` label when appropriate.
- **Stale candidate:** Look for an obsolete premise, superseding work, lost relevance, or an unanswered request for information. Ask for missing evidence when useful. Close only with a specific reason; elapsed time alone is insufficient.

After each mutation, requery the changed item and backlog count. Stop a batch at an unresolved contract, failed gate, new owner/subsystem decision, or lost authorization rather than silently widening scope.

## Report

For each item, give the issue/PR link, the observed state, the evidence that matters, the action taken or exact blocker, and the next action. Keep backlog counts separate from real fixes. On bare `ooo maintain`, return the single best next item and why it outranks the older alternatives; do not mutate GitHub merely because the skill was invoked.
