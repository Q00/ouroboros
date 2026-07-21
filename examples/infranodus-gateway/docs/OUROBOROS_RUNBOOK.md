# Ouroboros manual graph gates

This runbook keeps Ouroboros authoritative. InfraNodus advice is advisory and never advances, rewrites, or terminates a run.

## Gate 1: seed review

1. Run the normal interview and seed flow: `ooo interview start`, complete the session, then `ooo seed SESSION_ID`.
2. Pass sanitized requirements prose as `objective` and a sanitized seed summary as `candidate` to `graph_review_seed`.
3. Review the bounded observations. Update the seed through the normal Ouroboros workflow if a material gap is real.
4. Start `ooo run` only after the human accepts or explicitly dismisses the advisory result.

Do not send source code, URLs, credentials, personal data, or raw interview transcripts.

## Gate 2: stagnation diagnosis

1. Use the normal Ouroboros trace/harness surfaces to determine that the run is repeating an assumption: `ooo harness list`, `ooo harness show RUN_ID`, or `ooo harness frontier --metric METRIC`.
2. Summarize the intended outcome as `objective` and the current failed approach as `candidate`.
3. Call `graph_diagnose_stagnation` manually.
4. Treat one observation as a hypothesis to test through Ouroboros. Do not treat it as an automatic lateral event.

If the result is `DEGRADED_NO_GRAPH`, continue locally and record that graph advice was unavailable.

## Gate 3: delivery comparison

1. Summarize acceptance criteria as `objective` and verified delivery evidence as `candidate`.
2. Call `graph_compare_delivery`.
3. Resolve or explicitly dismiss material observations.
4. Run the authoritative local check, `ooo qa ARTIFACT`. Its exit code remains the delivery decision.

## Failure and rollback

- InfraNodus timeout or error: the adapter returns `DEGRADED_NO_GRAPH`; Ouroboros continues without graph advice.
- Policy rejection: remove sensitive/raw content and replace it with safe prose. Never weaken the policy for one call.
- Suspected persistence: stop using the adapter, run the live inventory verifier, and compare its count and digest evidence.
- Rollback: remove the MCP host entry and stop the stdio process. No Ouroboros or InfraNodus repository/database migration is required.

## Promotion gates beyond v1

Saved-graph reads, GraphRAG, automatic lifecycle hooks, and any write capability are separate security phases. Each requires a new threat model, explicit user approval, a narrower tool contract, immutable before/after inventory evidence, and its own rollback proof.
