"""Replayed-run records for evidence verifier tests."""

from __future__ import annotations

from ouroboros.orchestrator.evidence.harness_observation import CommandObservation
from ouroboros.orchestrator.evidence.test_reexecution import safe_test_invocation


def replayed_run(command: str, **fields: object) -> CommandObservation:
    """A replayed run of ``command`` with the argv and environment replay records.

    Replay always records the argv it executed; the verifier links claims to
    that argv and never re-reads the command text.
    """
    invocation = safe_test_invocation(command)
    assert invocation is not None, command
    env_delta, argv = invocation
    return CommandObservation(
        command=command,
        argv=argv,
        env_delta=tuple(sorted(env_delta.items())),
        **fields,  # type: ignore[arg-type]
    )
