---
name: interview
description: "Understand what the user wants and get explicit approval before any Seed is generated"
aliases: [socratic]
---

# /ouroboros:interview (experimental: proposal-first)

Purpose: reach an approved statement of what to build.

The conversation itself is free. No mandated tone, question format, option count,
or phrasing — talk the way you naturally would with this user, in their language.
The contract below fixes behaviors and failure conditions only, never wording.

## On entry — read, then propose

1. Read everything the user handed over, end to end: the prompt, files, repo
   signals, prior discussion about this task. Do not propose before reading — a
   proposal without reading is a guess. Handed over means given for this
   interview; unrelated session memory or past work is not material, and intent
   must never be manufactured from it.
2. Open with a proposal, not a question. State in one line, grounded in what you
   read (point at it), what you take the user to be trying to do — and briefly
   what you would build for that.
3. The user's job is to confirm, correct, or adjust. If the user has to compose
   their requirements from scratch to answer you, the opening failed — redo it.
4. If two intents are plausible, propose the more likely and note the other.
   Never present a guess as the only reading.
5. If nothing (or too little) was handed over, do not guess and do not explain
   what signals are missing — just ask, in a line or two, what they want to
   build or can share. Then wait. The proposal comes after there is something
   to read.

## During — track intent, dig for what was not said

6. Absorb every correction and scope change; keep your working statement of the
   intent current. Never silently rewrite the user's goal.
7. Keep stated and inferred separate at all times. Anything the user did not say
   is an inference; before it can bind the Seed, it needs their confirmation.
8. Hunt for what the user did not say: constraints, target environments,
   downstream steps, habits implied by their material. Confirm the risky ones.
   How and when you ask — batched, one by one, choices, free-form — is your
   judgment, sized to the user in front of you.

## On close — restate and get approval

9. Before any Seed is generated, restate the intent compactly: purpose / what
   will be built / what counts as done / constraints. Mark which parts are
   inferred rather than stated, and give the user the chance to correct them.
10. Seed generation requires the user's explicit approval of that restatement.
    No approval, no Seed.
11. If the user corrects the restatement, absorb the correction and restate —
    approval must cover the final version.

## Failure conditions — any one means the interview failed

- Proposed without reading.
- The user had to compose their requirements from scratch.
- An inference bound the Seed without confirmation.
- A Seed was generated without explicit approval of the final restatement.

## After approval

Run seed generation (`ooo seed`) with the approved restatement, the confirmed
facts, and the remaining marked assumptions as the interview result.
