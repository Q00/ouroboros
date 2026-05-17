"""Auto execution handoff idempotency contract (#579).

This module names the invariants the ``ooo auto`` run-handoff path
relies on so future refactors do not silently drift. It is an
observational layer over behavior that already exists in
``src/ouroboros/auto/pipeline.py``; importing from this module —
rather than re-stating the same magic strings — gives the contract
a single source of truth.

Three invariants:

1. **Replay-safety**: the same logical handoff attempt is keyed by
   ``state.auto_session_id`` so re-entering RUN through
   ``_handoff_to_ralph`` after a crash deduplicates against the
   prior attempt at the run-starter boundary. The key is stable
   across resume / re-pickup, never re-generated mid-session.

2. **Retry boundary**: a handoff that surfaces as ``unknown_no_handle``
   or ``unknown_timeout`` may be retried EXACTLY ONCE. The retry
   reuses the same idempotency key (invariant #1) and the
   ``run_handoff_guidance`` carries
   ``RETRY_GUIDANCE_PHRASE`` so a resumer can detect a second
   re-entry as already-retried and block instead of duplicating.

3. **Deduplication**: the run starter accepts an ``idempotency_key``
   kwarg via ``_accepts_keyword(self.run_starter, \"idempotency_key\")``;
   when present the runtime forwards the key. Run starters that
   honour the key MUST collapse two invocations with the same key
   onto a single underlying handoff (the runtime relies on this for
   crash-safety between ``state.run_handoff_status = \"started\"``
   and the actual run dispatch).
"""

from __future__ import annotations

from typing import Final

# Known run-handoff lifecycle status values. Kept named so presentation
# surfaces key off the pipeline state machine rather than any non-empty
# run_handoff_status.
RUN_HANDOFF_STARTED_STATUS: Final[str] = "started"

# Unknown handoff status values. Kept named so pipeline code does not need to
# restate the retryable status alphabet outside this contract module.
UNKNOWN_NO_HANDLE_STATUS: Final[str] = "unknown_no_handle"
UNKNOWN_TIMEOUT_STATUS: Final[str] = "unknown_timeout"

# Phrase appended to ``state.run_handoff_guidance`` after a retry.
# Resumers MUST treat the presence of this phrase as "this attempt
# was already retried once" — see invariant #2.
RETRY_GUIDANCE_PHRASE: Final[str] = "retried once with idempotency key"

# Per invariant #1: the idempotency key for run handoff is the
# auto-session id. Documented as a constant so a future refactor
# that wants to switch keys (e.g., a per-seed UUID) has a single
# touchpoint.
IDEMPOTENCY_KEY_FIELD: Final[str] = "auto_session_id"

# Kwarg name negotiated with the run starter via _accepts_keyword.
IDEMPOTENCY_KWARG_NAME: Final[str] = "idempotency_key"

# Handoff statuses for which the runtime cannot definitively say
# whether the underlying run started. Per invariant #2 these are the
# only statuses that authorize a second handoff attempt under the
# same idempotency key.
UNKNOWN_HANDOFF_STATUSES: Final[frozenset[str]] = frozenset(
    {UNKNOWN_NO_HANDLE_STATUS, UNKNOWN_TIMEOUT_STATUS}
)

UNKNOWN_TIMEOUT_GUIDANCE: Final[str] = (
    "Run starter timed out before a durable tracking handle was captured. "
    "The runtime may still have created an execution. Resume will attempt "
    "exactly one automatic retry reusing the same idempotency key "
    f"(state.{IDEMPOTENCY_KEY_FIELD}) so the server-side handler can "
    "short-circuit a duplicate enqueue. After that retry budget is exhausted "
    "the pipeline blocks and any further resume requires manual inspection."
)

UNKNOWN_NO_HANDLE_GUIDANCE: Final[str] = (
    "Run starter was attempted, but no durable tracking handle was captured. "
    "Resume will attempt exactly one automatic retry reusing the same "
    f"idempotency key (state.{IDEMPOTENCY_KEY_FIELD}) so the server-side handler "
    "can short-circuit a duplicate enqueue. After that retry budget is exhausted "
    "the pipeline blocks and any further resume requires manual inspection."
)

# Per invariant #2: a handoff may be retried EXACTLY ONCE.
MAX_RUN_HANDOFF_RETRIES: Final[int] = 1


def unknown_handoff_guidance(status: str) -> str:
    """Return documented retry guidance for an unknown run-handoff status."""
    if status == UNKNOWN_TIMEOUT_STATUS:
        return UNKNOWN_TIMEOUT_GUIDANCE
    return UNKNOWN_NO_HANDLE_GUIDANCE


__all__ = [
    "IDEMPOTENCY_KEY_FIELD",
    "IDEMPOTENCY_KWARG_NAME",
    "RUN_HANDOFF_STARTED_STATUS",
    "MAX_RUN_HANDOFF_RETRIES",
    "UNKNOWN_NO_HANDLE_GUIDANCE",
    "UNKNOWN_NO_HANDLE_STATUS",
    "RETRY_GUIDANCE_PHRASE",
    "UNKNOWN_HANDOFF_STATUSES",
    "UNKNOWN_TIMEOUT_GUIDANCE",
    "UNKNOWN_TIMEOUT_STATUS",
    "unknown_handoff_guidance",
]
