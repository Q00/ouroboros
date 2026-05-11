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

# Phrase appended to ``state.run_handoff_guidance`` after a retry.
# Resumers MUST treat the presence of this phrase as "this attempt
# was already retried once" — see invariant #2.
RETRY_GUIDANCE_PHRASE: Final[str] = "retried once with idempotency key"

# Handoff statuses for which the runtime cannot definitively say
# whether the underlying run started. Per invariant #2 these are the
# only statuses that authorize a second handoff attempt under the
# same idempotency key.
UNKNOWN_HANDOFF_STATUSES: Final[frozenset[str]] = frozenset(
    {"unknown_no_handle", "unknown_timeout"}
)

# Per invariant #2: a handoff may be retried EXACTLY ONCE.
MAX_RUN_HANDOFF_RETRIES: Final[int] = 1

# Per invariant #1: the idempotency key for run handoff is the
# auto-session id. Documented as a constant so a future refactor
# that wants to switch keys (e.g., a per-seed UUID) has a single
# touchpoint.
IDEMPOTENCY_KEY_FIELD: Final[str] = "auto_session_id"

# Kwarg name negotiated with the run starter via _accepts_keyword.
IDEMPOTENCY_KWARG_NAME: Final[str] = "idempotency_key"

__all__ = [
    "IDEMPOTENCY_KEY_FIELD",
    "IDEMPOTENCY_KWARG_NAME",
    "MAX_RUN_HANDOFF_RETRIES",
    "RETRY_GUIDANCE_PHRASE",
    "UNKNOWN_HANDOFF_STATUSES",
]
