# RFC: Inverted Interview Intent Preservation

## Status

Draft proposal. Tracking issue: #1504.

## Problem

Some interviews fail not because the user has not stated intent, but because the
system drifts away from intent the user already stated.

A recurring failure shape:

1. The user states a strong artifact contract, for example `CLI + web app`.
2. The interview or auto-answer path introduces a safer narrower option, for
   example `docs-only handoff`.
3. The pipeline converges on the narrower option because it is conservative and
   easy to verify.
4. The output is safe, but wrong: the final artifact class changed without an
   explicit user decision.

This is different from ordinary ambiguity. The missing behavior is not “ask more
open-ended questions.” The missing behavior is “reconstruct and preserve the
user's already-stated contract before asking the next question.”

## Proposal

Add an **inverted interview** mode or guardrail for interviews that begin with
strong user intent or prior context.

Instead of starting only with open-ended Socratic questions, the interviewer first
reconstructs the current requirement as explicit, falsifiable assumptions:

- final artifact class;
- outputs that are products versus outputs that are supporting artifacts;
- non-goals;
- locked user facts;
- unsafe interpretation changes;
- unresolved decisions that still need human authority.

Then it asks the user to correct mismatches.

Example:

```text
My current understanding:

1. Final artifact is a CLI plus web app.
2. Docs-only handoff is not the final artifact.
3. Checklist files are generated outputs of the CLI/web system, not the product.
4. Existing legacy workflow behavior must be inherited where relevant.
5. Missing source data must be represented as insufficient/unchecked, not OK.

Which line is wrong or incomplete?
```

The user can answer with a small correction instead of restating the whole task.
The interview repeats this assumption-led loop until no material mismatch
remains, then continues through the existing Seed readiness and Restate gates.

## Relationship to Socratic interview

This does not replace the existing Socratic interview. It is a front-loaded
intent-preservation pass for cases where the user has already supplied material
requirements.

Normal Socratic interview asks:

```text
What do you want to build?
```

Inverted interview asks:

```text
Here is what I think you already asked for. Which part is wrong?
```

Both are dialectic. The difference is that inverted interview treats prior user
intent as the first object of examination.

## Contract

When a user-stated artifact class is locked by goal text, preference, or a
confirmed ledger entry:

- generated answers must not replace it with a narrower artifact class;
- docs-only, review-only, handoff-only, or checklist-only options must be treated
  as scope-reduction candidates;
- scope reductions require an explicit user answer or a blocked state;
- if a human chooses the narrower artifact, record a warning and require
  confirmation before Seed generation;
- supporting artifacts may still be generated, but they must not be represented
  as the final product unless the user says so.

## Example failure this guards

A user requests a reusable local tool with both CLI and web surfaces. During
interview, a conservative default chooses a docs-only handoff package. The handoff
contains useful material, but the final artifact class changed from executable
software to static documentation.

In inverted mode, the first interview turn should lock:

```yaml
final_artifact: CLI + web app
supporting_artifacts:
  - checklist files
  - handoff documents
  - review summaries
not_final_artifact:
  - docs-only handoff
```

A later generated `docs-only handoff` answer conflicts with this contract and must
be blocked unless the user explicitly changes the contract.

## Implementation slice in this PR

This PR adds a small IntentGuard extension for narrowed-output artifact drift:

- output-contract detection recognizes CLI/web/tool/app artifacts;
- generated `docs-only` / `handoff-only` / `checkpack` / `checklist package`
  answers are treated like existing review-only reductions;
- diagnostics warn when a pending question offers a docs-only narrowing next to a
  user-locked output contract;
- tests cover CLI/web intent drifting toward docs-only handoff.

## Non-goals

- No new interview command flag yet.
- No full assumption-led UI yet.
- No replacement of the current Socratic interview.
- No Seed schema change in this slice.

## Future work

Possible follow-ups:

1. `ooo interview --mode inverted` or `ooo auto --interview-mode inverted`.
2. A first-turn assumption ledger rendered to the user for correction.
3. A Seed Draft Ledger field for locked artifact class versus supporting outputs.
4. Auto-activation when the user corrects artifact scope or when generated options
   offer docs-only/review-only alternatives to a user-locked executable artifact.
