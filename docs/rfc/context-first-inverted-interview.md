# RFC: Context-First Inverted Interview

## Status

Draft proposal.

## Problem

Some Seed interviews are hard for the wrong reason.

The user is not starting from a blank slate. They may have already supplied:

- a long prior discussion;
- raw files;
- recordings or transcripts;
- handoff notes;
- failed previous Seeds;
- generated artifacts that are useful as leads but not trustworthy;
- domain constraints that have already been corrected multiple times.

In those cases, a normal open-ended interview can create three failures:

1. **Context loss** — the system asks for requirements that are already present
   in the pre-work.
2. **Fast shallow convergence** — the interview reaches `seed_ready` with a safe
   but under-scoped interpretation.
3. **User fatigue** — the user has to restate the same intent instead of only
   correcting the system's current understanding.

This is different from an underspecified new idea. The missing behavior is not
"ask more questions." The missing behavior is "synthesize the existing context
first, then ask the user to correct the synthesis."

## Proposal

Add a **context-first inverted interview** mode before Seed generation.

Instead of beginning with broad questions, the interviewer first performs a
pre-work synthesis pass:

1. inspect supplied context and raw artifacts;
2. reconstruct the current requirement model;
3. separate confirmed facts, inferred assumptions, unresolved ambiguities, and
   human-only decisions;
4. present that model back as short falsifiable statements;
5. ask the user to correct only the wrong or incomplete lines;
6. repeat until material ambiguity is gone;
7. then proceed to normal Seed generation and review.

## Example interaction

```text
Interviewer:

Based on the prior context and files, my current understanding is:

1. The final product is a reusable CLI plus a local web surface.
2. Static handoff docs are supporting outputs, not the product.
3. The legacy workflow should be treated as a reference source, not discarded.
4. New reviewer checks should be added only as candidate alerts, not final
   judgments.
5. Missing required input data must become `insufficient_data` / `unchecked`, not
   `OK`.

Which line is wrong or incomplete?
```

The user can answer:

```text
Line 4 is right, but add that reviewer checks must include the items from the
recording. Line 1 must say CLI is the batch/reproducibility surface and web is the
human review surface.
```

The next turn updates the requirement model instead of starting over.

## Output model

The pre-work synthesis should produce an auditable ledger before the Seed:

```yaml
confirmed_facts:
  - value: "Final product includes CLI and web surface"
    source: user_prior_context

inferred_assumptions:
  - value: "Static handoff documents are generated outputs"
    source: prework_synthesis
    confidence: 0.8

unresolved_ambiguities:
  - question: "Which reviewer checks are mandatory for MVP?"
    reason: "Named in prior discussion but not mapped to acceptance criteria"

human_only_decisions:
  - question: "May the system change the final artifact class?"
    reason: "Changes product scope and must not be defaulted"
```

## Relationship to existing Socratic interview

This does not replace the Socratic interview.

Normal Socratic interview is best when the user has a vague goal and needs the
system to expose hidden assumptions.

Context-first inverted interview is best when there is already substantial
pre-work and the risk is losing it.

The two modes can compose:

```text
prework synthesis -> inverted correction loop -> Socratic follow-up for only the
remaining ambiguities -> Seed
```

## Activation candidates

The mode may be explicit:

```bash
ooo interview --mode inverted
ooo auto --interview-mode inverted
```

It may also be suggested when signals appear:

- the prompt references prior discussion, recordings, raw files, or pre-work;
- the user says the interview is too hard or keeps losing context;
- previous auto/Seed attempts produced a safe but wrong artifact class;
- the user corrects the same scope assumption more than once;
- the driver detects many raw artifacts but few confirmed ledger entries.

## Contract

When inverted mode is active:

- the system should not ask broad blank-slate questions until it has presented a
  pre-work synthesis;
- each synthesized statement must be source-tagged as confirmed, inferred,
  ambiguous, or human-only;
- inferred assumptions must be falsifiable and easy for the user to correct;
- user corrections outrank generated synthesis;
- Seed generation must not proceed while a human-only decision is unresolved;
- generated safe defaults may fill local reversible gaps, but must not replace
  a user-stated artifact class or domain constraint.

## Non-goals

- This RFC does not require a new Seed schema immediately.
- This RFC does not remove current interview behavior.
- This RFC does not require full raw-file analysis in the first implementation
  slice.
- This RFC does not make generated synthesis authoritative without user review.

## Initial implementation slices

Small follow-up PRs could land independently:

1. Add a pre-work synthesis ledger type.
2. Add an `--interview-mode inverted` flag that prints the synthesized ledger
   before the first question.
3. Add a diagnostic that detects high-context prompts with no synthesis pass.
4. Add regression tests for safe-but-wrong Seed outputs after rich pre-work.
5. Add examples showing context-first inverted interview transcripts.

## Acceptance criteria

- A high-context prompt can produce an assumption-led first interview turn.
- Confirmed user facts, inferred assumptions, ambiguities, and human-only
  decisions are distinguishable in output.
- The user can correct a numbered line without restating the whole requirement.
- Seed generation waits for unresolved human-only decisions.
- Tests cover a case where pre-work contains the intended final artifact, but a
  blank-slate interview would otherwise converge to a narrower safe output.
