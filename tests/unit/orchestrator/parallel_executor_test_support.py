"""Test-only seams for exercising captured parallel-executor entry roots."""

from __future__ import annotations

from typing import Any

from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


class ProcessLocalTestExecutor(ParallelACExecutor):
    """Constructor-time test double whose entry roots stay process-local.

    Production execution captures its finite internal entry roots when the
    executor is constructed. Tests that need controllable leaf behavior use
    this explicit subclass instead of mutating a production executor's roots
    after construction.
    """

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_execute_single_ac":
            object.__setattr__(self, "_test_single_ac_runner", value)
            return
        if name == "_execute_atomic_ac":
            object.__setattr__(self, "_test_atomic_ac_runner", value)
            return
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name == "_execute_single_ac":
            object.__setattr__(self, "_test_single_ac_runner", None)
            return
        if name == "_execute_atomic_ac":
            object.__setattr__(self, "_test_atomic_ac_runner", None)
            return
        object.__delattr__(self, name)

    async def _execute_single_ac(self, *args: Any, **kwargs: Any) -> Any:
        calls = getattr(self, "_test_single_ac_calls", None)
        if isinstance(calls, list):
            calls.append(dict(kwargs))
        runner = getattr(self, "_test_single_ac_runner", None)
        if runner is not None:
            return await runner(*args, **kwargs)
        return await super()._execute_single_ac(*args, **kwargs)

    async def _execute_atomic_ac(self, *args: Any, **kwargs: Any) -> Any:
        runner = getattr(self, "_test_atomic_ac_runner", None)
        if runner is not None:
            return await runner(*args, **kwargs)
        return await super()._execute_atomic_ac(*args, **kwargs)
