"""Contract tests for the auto run-handoff idempotency invariants (#579).

Each test pins one named invariant from ``handoff_contract.py``.  If
someone re-introduces a local magic string or widens the retryable set
without updating the contract module, a test here will fail.
"""

from __future__ import annotations

import inspect


def test_retry_guidance_phrase_is_stable() -> None:
    """Resumers grep for this exact phrase; changing it is a contract break."""
    from ouroboros.auto.handoff_contract import RETRY_GUIDANCE_PHRASE

    assert RETRY_GUIDANCE_PHRASE == "retried once with idempotency key"


def test_unknown_handoff_statuses_alphabet_is_closed() -> None:
    """Per invariant #2, only these statuses authorize a retry."""
    from ouroboros.auto.handoff_contract import UNKNOWN_HANDOFF_STATUSES

    assert frozenset({"unknown_no_handle", "unknown_timeout"}) == UNKNOWN_HANDOFF_STATUSES


def test_unknown_handoff_statuses_is_frozen() -> None:
    """The retryable-status set must be immutable so runtime code cannot
    accidentally widen the alphabet at runtime."""
    from ouroboros.auto.handoff_contract import UNKNOWN_HANDOFF_STATUSES

    assert isinstance(UNKNOWN_HANDOFF_STATUSES, frozenset)


def test_max_run_handoff_retries_is_one() -> None:
    """Per invariant #2 a handoff may be retried EXACTLY ONCE."""
    from ouroboros.auto.handoff_contract import MAX_RUN_HANDOFF_RETRIES

    assert MAX_RUN_HANDOFF_RETRIES == 1


def test_idempotency_key_field_points_to_session_id() -> None:
    """Per invariant #1 the key is always sourced from auto_session_id."""
    from ouroboros.auto.handoff_contract import IDEMPOTENCY_KEY_FIELD

    assert IDEMPOTENCY_KEY_FIELD == "auto_session_id"


def test_idempotency_kwarg_name_matches_run_starter_protocol() -> None:
    """The kwarg negotiated with run_starter must match the Protocol definition."""
    from ouroboros.auto.handoff_contract import IDEMPOTENCY_KWARG_NAME
    from ouroboros.auto.pipeline import RunStarter

    assert IDEMPOTENCY_KWARG_NAME == "idempotency_key"
    # The RunStarter Protocol __call__ must accept this kwarg.
    sig = inspect.signature(RunStarter.__call__)
    assert IDEMPOTENCY_KWARG_NAME in sig.parameters


def test_pipeline_uses_contract_module(monkeypatch) -> None:  # noqa: ARG001
    """Catch silent drift: pipeline.py must not reintroduce the
    literal phrase / status set."""
    from ouroboros.auto import pipeline

    source = inspect.getsource(pipeline)
    # After stripping the symbol name itself, the raw phrase must not appear.
    assert "retried once with idempotency key" not in source.replace("RETRY_GUIDANCE_PHRASE", ""), (
        "pipeline.py must import RETRY_GUIDANCE_PHRASE, not literal it"
    )


def test_pipeline_uses_unknown_handoff_statuses_not_literal_set() -> None:
    """pipeline.py must not re-define the retryable-status set as a literal."""
    from ouroboros.auto import pipeline

    source = inspect.getsource(pipeline)
    # After stripping the symbol name, no inline set literal of both statuses
    # should remain — they must come from UNKNOWN_HANDOFF_STATUSES.
    stripped = source.replace("UNKNOWN_HANDOFF_STATUSES", "")
    assert not (
        '"unknown_no_handle"' in stripped
        and '"unknown_timeout"' in stripped
        and '{"unknown_no_handle", "unknown_timeout"}' in stripped
    ), "pipeline.py must use UNKNOWN_HANDOFF_STATUSES, not a literal set"


def test_all_contract_symbols_exported() -> None:
    """__all__ must include every public constant so wildcard imports are safe."""
    import ouroboros.auto.handoff_contract as hc

    for name in hc.__all__:
        assert hasattr(hc, name), f"{name!r} in __all__ but not defined"
    # Spot-check key symbols are present.
    for expected in (
        "RETRY_GUIDANCE_PHRASE",
        "UNKNOWN_HANDOFF_STATUSES",
        "MAX_RUN_HANDOFF_RETRIES",
        "IDEMPOTENCY_KEY_FIELD",
        "IDEMPOTENCY_KWARG_NAME",
    ):
        assert expected in hc.__all__
