"""Shared runtime rate-limit coordination for orchestrator workers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import dis
import hashlib
import inspect
import json
import marshal
import math
import time
from typing import Any, Literal
import uuid

from ouroboros.core.security import is_sensitive_field, is_sensitive_value

RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_HEARTBEAT_SECONDS = 30.0
RATE_LIMIT_MAX_WAIT_SECONDS = 120.0
RATE_LIMIT_TOKEN_ESTIMATION_VERSION = 1
RATE_LIMIT_GATE_ALGORITHM_VERSION = 1
DEFAULT_ANTHROPIC_RPM_CEILING = 40
DEFAULT_ANTHROPIC_TPM_CEILING = 32_000
_TOKEN_ESTIMATE_DIVISOR = 4
_TOKEN_COMPLETION_CUSHION = 1024
_GATE_FACTORY_MAX_DEPTH = 8
_GATE_FACTORY_MAX_ITEMS = 256
_DECLARED_RATE_GATE_FACTORY: object | None = None
_DECLARED_RATE_GATE_FACTORY_IMPLEMENTATION: _DeclaredFunctionImplementation | None = None
_DECLARED_RATE_TOKEN_ESTIMATOR: object | None = None
_DECLARED_RATE_TOKEN_ESTIMATOR_IMPLEMENTATION: _DeclaredFunctionImplementation | None = None


@dataclass(frozen=True, slots=True)
class _DeclaredFunctionImplementation:
    """Import-time implementation state for a portable Python function."""

    code: object
    defaults: object
    default_values: tuple[object, ...]
    kwdefaults: object
    kwdefault_items: tuple[tuple[str, object], ...]
    closure: object
    closure_values: tuple[object, ...]
    resolved_globals: dict[str, object]
    resolved_builtins: dict[str, object]
    module_member_values: dict[tuple[str, str], object]
    global_function_bodies: dict[str, _DeclaredFunctionBody]
    module_function_bodies: dict[tuple[str, str], _DeclaredFunctionBody]


@dataclass(frozen=True, slots=True)
class _DeclaredFunctionBody:
    """The mutable implementation fields of a directly resolved function."""

    code: object
    defaults: object
    default_values: tuple[object, ...]
    kwdefaults: object
    kwdefault_items: tuple[tuple[str, object], ...]
    closure: object
    closure_values: tuple[object, ...]


def _function_closure_values(closure: object) -> tuple[object, ...]:
    """Read closure cells without accepting an opaque replacement container."""
    if closure is None:
        return ()
    if not isinstance(closure, tuple):
        raise ValueError("rate gate factory closure is not a tuple")
    return tuple(cell.cell_contents for cell in closure)


def _declared_function_body(function: object) -> _DeclaredFunctionBody:
    """Capture code/default/closure state without recursively traversing globals."""
    if not inspect.isfunction(function):
        raise RuntimeError("declared rate-gate dependency is not a Python function")
    kwdefaults = function.__kwdefaults__
    if kwdefaults is not None and not isinstance(kwdefaults, dict):
        raise RuntimeError("declared rate-gate keyword defaults are invalid")
    return _DeclaredFunctionBody(
        code=function.__code__,
        defaults=function.__defaults__,
        default_values=tuple(function.__defaults__ or ()),
        kwdefaults=kwdefaults,
        kwdefault_items=tuple(sorted((kwdefaults or {}).items())),
        closure=function.__closure__,
        closure_values=_function_closure_values(function.__closure__),
    )


def _declared_function_body_is_intact(
    function: object,
    declared: _DeclaredFunctionBody,
) -> bool:
    """Check code/default/closure state against an import-time snapshot."""
    if not inspect.isfunction(function):
        return False
    try:
        current_kwdefaults = function.__kwdefaults__
        current_kwdefault_items = tuple(sorted((current_kwdefaults or {}).items()))
        current_closure_values = _function_closure_values(function.__closure__)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        function.__code__ is declared.code
        and function.__defaults__ is declared.defaults
        and len(function.__defaults__ or ()) == len(declared.default_values)
        and all(
            current is expected
            for current, expected in zip(
                function.__defaults__ or (),
                declared.default_values,
                strict=True,
            )
        )
        and current_kwdefaults is declared.kwdefaults
        and len(current_kwdefault_items) == len(declared.kwdefault_items)
        and all(
            current_name == expected_name and current_value is expected_value
            for (current_name, current_value), (expected_name, expected_value) in zip(
                current_kwdefault_items,
                declared.kwdefault_items,
                strict=True,
            )
        )
        and function.__closure__ is declared.closure
        and len(current_closure_values) == len(declared.closure_values)
        and all(
            current is expected
            for current, expected in zip(
                current_closure_values,
                declared.closure_values,
                strict=True,
            )
        )
    )


def _declared_function_direct_module_attributes(
    function: object,
    global_name: str,
) -> list[str]:
    """Find direct ``module.attribute`` reads made by a Python function."""
    if not inspect.isfunction(function):
        raise RuntimeError("declared rate-gate dependency is not a Python function")
    instructions = list(dis.get_instructions(function))
    attributes: set[str] = set()
    for index, instruction in enumerate(instructions):
        if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"} or instruction.argval != global_name:
            continue
        for following in instructions[index + 1 : index + 4]:
            if following.opname in {"CACHE", "EXTENDED_ARG", "PUSH_NULL"}:
                continue
            if following.opname in {"LOAD_ATTR", "LOAD_METHOD"} and isinstance(
                following.argval, str
            ):
                attributes.add(following.argval)
            break
    return sorted(attributes)


def _declared_function_implementation(
    function: object,
) -> _DeclaredFunctionImplementation:
    """Capture the implementation state that must remain import-time exact."""
    if not inspect.isfunction(function):
        raise RuntimeError("declared rate-gate member is not a Python function")
    body = _declared_function_body(function)
    closure_vars = inspect.getclosurevars(function)
    module_member_values: dict[tuple[str, str], object] = {}
    module_function_bodies: dict[tuple[str, str], _DeclaredFunctionBody] = {}
    for global_name, dependency in closure_vars.globals.items():
        if not inspect.ismodule(dependency):
            continue
        for attribute in _declared_function_direct_module_attributes(function, global_name):
            try:
                member = getattr(dependency, attribute)
            except Exception:
                raise RuntimeError("declared rate-gate module member is unobservable") from None
            key = (global_name, attribute)
            module_member_values[key] = member
            if inspect.isfunction(member):
                module_function_bodies[key] = _declared_function_body(member)
    return _DeclaredFunctionImplementation(
        code=body.code,
        defaults=body.defaults,
        default_values=body.default_values,
        kwdefaults=body.kwdefaults,
        kwdefault_items=body.kwdefault_items,
        closure=body.closure,
        closure_values=body.closure_values,
        resolved_globals=dict(closure_vars.globals),
        resolved_builtins=dict(closure_vars.builtins),
        module_member_values=module_member_values,
        global_function_bodies={
            name: _declared_function_body(dependency)
            for name, dependency in closure_vars.globals.items()
            if inspect.isfunction(dependency)
        },
        module_function_bodies=module_function_bodies,
    )


def _declared_function_implementation_is_intact(
    function: object,
    declared: _DeclaredFunctionImplementation,
) -> bool:
    """Return whether a function retains its exact import-time behavior state."""
    if not inspect.isfunction(function):
        return False
    try:
        closure_vars = inspect.getclosurevars(function)
    except (AttributeError, TypeError, ValueError):
        return False
    body_is_intact = _declared_function_body_is_intact(
        function,
        _DeclaredFunctionBody(
            code=declared.code,
            defaults=declared.defaults,
            default_values=declared.default_values,
            kwdefaults=declared.kwdefaults,
            kwdefault_items=declared.kwdefault_items,
            closure=declared.closure,
            closure_values=declared.closure_values,
        ),
    )
    if (
        not body_is_intact
        or set(closure_vars.globals) != set(declared.resolved_globals)
        or any(
            closure_vars.globals[name] is not dependency
            for name, dependency in declared.resolved_globals.items()
        )
        or set(closure_vars.builtins) != set(declared.resolved_builtins)
        or any(
            closure_vars.builtins[name] is not dependency
            for name, dependency in declared.resolved_builtins.items()
        )
    ):
        return False
    for (global_name, attribute), expected_member in declared.module_member_values.items():
        dependency = closure_vars.globals.get(global_name)
        if not inspect.ismodule(dependency):
            return False
        try:
            current_member = getattr(dependency, attribute)
        except Exception:
            return False
        if current_member is not expected_member:
            return False
    for name, declared_body in declared.global_function_bodies.items():
        if not _declared_function_body_is_intact(closure_vars.globals[name], declared_body):
            return False
    for (global_name, attribute), declared_body in declared.module_function_bodies.items():
        dependency = closure_vars.globals.get(global_name)
        if not inspect.ismodule(dependency):
            return False
        try:
            current_member = getattr(dependency, attribute)
        except Exception:
            return False
        if not _declared_function_body_is_intact(current_member, declared_body):
            return False
    return True


def _declared_member_function_targets(raw_member: object) -> tuple[object, ...]:
    """Return Python-function targets exposed by a raw class member."""
    if isinstance(raw_member, (classmethod, staticmethod)):
        return (raw_member.__func__,)
    if isinstance(raw_member, property):
        return tuple(
            target
            for target in (raw_member.fget, raw_member.fset, raw_member.fdel)
            if target is not None
        )
    if inspect.isfunction(raw_member):
        return (raw_member,)
    return ()


@dataclass(frozen=True, slots=True)
class _DeclaredClassImplementation:
    """Import-time raw-member and executable state for a class dependency."""

    runtime_class: type[object]
    class_chain: tuple[type[object], ...]
    raw_items: dict[type[object], dict[str, object]]
    member_implementations: dict[
        tuple[type[object], str, int], _DeclaredFunctionImplementation
    ]


def _declared_class_implementation(
    runtime_class: type[object],
) -> _DeclaredClassImplementation:
    """Capture executable state without recursively following class globals."""
    class_chain = tuple(
        current for current in runtime_class.__mro__ if current is not object
    )
    raw_items: dict[type[object], dict[str, object]] = {}
    member_implementations: dict[
        tuple[type[object], str, int], _DeclaredFunctionImplementation
    ] = {}
    for current_class in class_chain:
        current_raw_items = dict(vars(current_class))
        raw_items[current_class] = current_raw_items
        for member_name, raw_member in current_raw_items.items():
            for index, target in enumerate(_declared_member_function_targets(raw_member)):
                member_implementations[(current_class, member_name, index)] = (
                    _declared_function_implementation(target)
                )
    return _DeclaredClassImplementation(
        runtime_class=runtime_class,
        class_chain=class_chain,
        raw_items=raw_items,
        member_implementations=member_implementations,
    )


def _declared_class_implementation_is_intact(
    value: object,
    declared: _DeclaredClassImplementation,
) -> bool:
    """Return whether a class dependency retains its imported implementation."""
    if not isinstance(value, type) or value is not declared.runtime_class:
        return False
    current_mro = tuple(current for current in value.__mro__ if current is not object)
    if current_mro != declared.class_chain:
        return False
    for current_class in declared.class_chain:
        expected_raw_items = declared.raw_items[current_class]
        current_raw_items = dict(vars(current_class))
        if (
            set(current_raw_items) != set(expected_raw_items)
            or any(
                expected_raw_items[name] is not current
                for name, current in current_raw_items.items()
            )
        ):
            return False
        for member_name, raw_member in current_raw_items.items():
            for index, target in enumerate(_declared_member_function_targets(raw_member)):
                declared_member = declared.member_implementations.get(
                    (current_class, member_name, index)
                )
                if (
                    declared_member is None
                    or not _declared_function_implementation_is_intact(target, declared_member)
                ):
                    return False
    return True


def _declared_module_member_implementation(
    value: object,
) -> _DeclaredFunctionImplementation | _DeclaredClassImplementation | None:
    """Capture mutable executable state of one direct module member."""
    if inspect.isfunction(value):
        return _declared_function_implementation(value)
    if isinstance(value, type):
        return _declared_class_implementation(value)
    return None


def _declared_module_member_implementation_is_intact(
    value: object,
    declared: _DeclaredFunctionImplementation | _DeclaredClassImplementation | None,
) -> bool:
    """Check a direct module dependency against its import-time declaration."""
    if declared is None:
        return True
    if isinstance(declared, _DeclaredFunctionImplementation):
        return _declared_function_implementation_is_intact(value, declared)
    return _declared_class_implementation_is_intact(value, declared)


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """Current shared-budget usage for one runtime backend."""

    runtime_backend: str
    requests_in_window: int
    request_limit: int | None
    tokens_in_window: int
    token_limit: int | None


class SharedRateLimitBucket:
    """Sliding-window request/token budget shared by concurrent runtime workers."""

    def __init__(
        self,
        *,
        runtime_backend: str,
        request_limit: int | None,
        token_limit: int | None,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self._runtime_backend = runtime_backend
        self._request_limit = request_limit if request_limit and request_limit > 0 else None
        self._token_limit = token_limit if token_limit and token_limit > 0 else None
        self._window_seconds = window_seconds
        self._time = time_provider or time.monotonic
        self._lock = asyncio.Lock()
        self._reservations: deque[tuple[float, int]] = deque()

    @property
    def enabled(self) -> bool:
        """Return True when either request or token budgets are active."""
        return self._request_limit is not None or self._token_limit is not None

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._reservations and self._reservations[0][0] <= cutoff:
            self._reservations.popleft()

    def _tokens_in_window(self) -> int:
        return sum(tokens for _, tokens in self._reservations)

    def _snapshot(self) -> RateLimitSnapshot:
        return RateLimitSnapshot(
            runtime_backend=self._runtime_backend,
            requests_in_window=len(self._reservations),
            request_limit=self._request_limit,
            tokens_in_window=self._tokens_in_window(),
            token_limit=self._token_limit,
        )

    def _request_wait_seconds(self, now: float) -> float:
        if self._request_limit is None or len(self._reservations) < self._request_limit:
            return 0.0
        oldest_timestamp, _ = self._reservations[0]
        return max(0.0, oldest_timestamp + self._window_seconds - now)

    def _token_wait_seconds(self, now: float, estimated_tokens: int) -> float:
        if self._token_limit is None:
            return 0.0

        current_tokens = self._tokens_in_window()
        if current_tokens + estimated_tokens <= self._token_limit:
            return 0.0

        remaining_tokens = current_tokens
        wait_seconds = 0.0
        for timestamp, reserved_tokens in self._reservations:
            remaining_tokens -= reserved_tokens
            wait_seconds = max(0.0, timestamp + self._window_seconds - now)
            if remaining_tokens + estimated_tokens <= self._token_limit:
                return wait_seconds

        if not self._reservations:
            return 0.0
        newest_timestamp, _ = self._reservations[-1]
        return max(0.0, newest_timestamp + self._window_seconds - now)

    async def acquire(self, estimated_tokens: int) -> tuple[float, RateLimitSnapshot]:
        """Reserve capacity immediately or return the wait time before retry."""
        normalized_tokens = max(1, estimated_tokens)
        async with self._lock:
            now = self._time()
            self._prune(now)
            wait_seconds = max(
                self._request_wait_seconds(now),
                self._token_wait_seconds(now, normalized_tokens),
            )
            if wait_seconds <= 0:
                self._reservations.append((now, normalized_tokens))
                return 0.0, self._snapshot()
            return wait_seconds, self._snapshot()

    async def force_reserve(self, estimated_tokens: int) -> RateLimitSnapshot:
        """Reserve capacity unconditionally (for timeout escape hatch).

        Used when the wait loop has exhausted its maximum wait budget and
        must proceed regardless. This preserves the budget accounting
        invariant — without this, N workers timing out simultaneously
        would each bypass the bucket, causing N× the intended RPM to
        hit the upstream API in lockstep.
        """
        normalized_tokens = max(1, estimated_tokens)
        async with self._lock:
            now = self._time()
            self._prune(now)
            self._reservations.append((now, normalized_tokens))
            return self._snapshot()


@dataclass(frozen=True, slots=True)
class RateLimitBackoff:
    """Observability record for one gate backoff or forced-reserve event."""

    wait_seconds: float
    total_waited: float
    max_wait_seconds: float
    snapshot: RateLimitSnapshot
    forced: bool


class RateLimitGate:
    """Backend-agnostic dispatch gate around a :class:`SharedRateLimitBucket`.

    Wraps the acquire/heartbeat/force-reserve wait loop so any caller — not just
    the native Claude adapter — can pace dispatch within a shared RPM/TPM budget.
    When the underlying bucket carries no limits the gate is *dormant*:
    :meth:`acquire` returns immediately, so wiring it onto a path that has no
    configured limits is a no-op.

    Observability is delivered through an optional ``on_backoff`` callback rather
    than by yielding messages, keeping the gate independent of any UI/event type.
    """

    def __init__(
        self,
        bucket: SharedRateLimitBucket,
        *,
        max_wait_seconds: float = RATE_LIMIT_MAX_WAIT_SECONDS,
        heartbeat_seconds: float = RATE_LIMIT_HEARTBEAT_SECONDS,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._bucket = bucket
        self._max_wait_seconds = max_wait_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._sleep = sleep or asyncio.sleep

    @property
    def enabled(self) -> bool:
        """Return True when the underlying budget is active."""
        return self._bucket.enabled

    async def acquire(
        self,
        estimated_tokens: int,
        *,
        on_backoff: Callable[[RateLimitBackoff], None] | None = None,
    ) -> None:
        """Block until shared budget headroom is available (or forced).

        Returns immediately when the gate is dormant. Otherwise waits in
        heartbeat-sized sleeps until capacity is reserved, force-reserving once
        the cumulative wait exceeds ``max_wait_seconds`` so concurrent
        timeout-fallbacks cannot bypass the bucket in lockstep (an N× burst).
        """
        if not self._bucket.enabled:
            return

        total_waited = 0.0
        while True:
            wait_seconds, snapshot = await self._bucket.acquire(estimated_tokens)
            if wait_seconds <= 0:
                return

            if total_waited >= self._max_wait_seconds:
                snapshot = await self._bucket.force_reserve(estimated_tokens)
                if on_backoff is not None:
                    on_backoff(
                        RateLimitBackoff(
                            wait_seconds=0.0,
                            total_waited=total_waited,
                            max_wait_seconds=self._max_wait_seconds,
                            snapshot=snapshot,
                            forced=True,
                        )
                    )
                return

            sleep_seconds = min(wait_seconds, self._heartbeat_seconds)
            if on_backoff is not None:
                on_backoff(
                    RateLimitBackoff(
                        wait_seconds=sleep_seconds,
                        total_waited=total_waited,
                        max_wait_seconds=self._max_wait_seconds,
                        snapshot=snapshot,
                        forced=False,
                    )
                )
            await self._sleep(sleep_seconds)
            total_waited += sleep_seconds


@dataclass(frozen=True, slots=True)
class AuthorityBoundRateLimitGate:
    """One installed gate plus the private state that its authority covers.

    The public algorithm contract is intentionally digest-only. Dispatch also
    needs a live in-process guard: an instance-field replacement can alter the
    active bucket without changing that static digest. This binding retains only
    references already owned by the executor so it can reject such drift before
    the captured ``acquire`` callable is invoked.
    """

    gate: RateLimitGate
    algorithm: dict[str, object]
    factory: object
    acquire: Callable[..., Any]
    bucket: SharedRateLimitBucket
    runtime_backend: str
    request_limit: int | None
    token_limit: int | None
    window_seconds: float
    max_wait_seconds: float
    heartbeat_seconds: float
    sleep: object
    clock: object
    lock: object
    reservations: object


def _declared_gate_class_members() -> dict[tuple[type[object], str, int], object]:
    """Capture the original direct gate-class members at module load time."""
    members: dict[tuple[type[object], str, int], object] = {}
    for runtime_class in (SharedRateLimitBucket, RateLimitGate):
        for member_name, raw_member in vars(runtime_class).items():
            targets: tuple[object, ...]
            if isinstance(raw_member, (classmethod, staticmethod)):
                targets = (raw_member.__func__,)
            elif isinstance(raw_member, property):
                targets = tuple(
                    target
                    for target in (raw_member.fget, raw_member.fset, raw_member.fdel)
                    if target is not None
                )
            elif inspect.isfunction(raw_member):
                targets = (raw_member,)
            else:
                continue
            for index, target in enumerate(targets):
                members[(runtime_class, member_name, index)] = target
    return members


# This is intentionally an object-identity manifest, not module/qualname text:
# ``functools.wraps`` can forge those display fields while leaving a live,
# stateful replacement installed.  A replacement is valid for a process, but
# its semantics are not portable authority data.
_DECLARED_GATE_CLASS_MEMBERS = _declared_gate_class_members()
_DECLARED_GATE_CLASS_RAW_MEMBERS: dict[tuple[type[object], str], object] = {
    (runtime_class, member_name): vars(runtime_class)[member_name]
    for runtime_class, member_name, _index in _DECLARED_GATE_CLASS_MEMBERS
}
_DECLARED_GATE_CLASS_RAW_ITEMS: dict[type[object], dict[str, object]] = {
    runtime_class: dict(vars(runtime_class))
    for runtime_class in (SharedRateLimitBucket, RateLimitGate)
}
_DECLARED_GATE_MEMBER_IMPLEMENTATIONS = {
    key: _declared_function_implementation(target)
    for key, target in _DECLARED_GATE_CLASS_MEMBERS.items()
}


def _declared_gate_member_dependencies(
    attribute: str,
) -> dict[tuple[type[object], str, int], dict[str, object]]:
    """Capture the direct globals or builtins resolved by original members."""
    dependencies: dict[tuple[type[object], str, int], dict[str, object]] = {}
    for key, target in _DECLARED_GATE_CLASS_MEMBERS.items():
        function = target.__func__ if inspect.ismethod(target) else target
        if not inspect.isfunction(function):
            raise RuntimeError("declared rate-gate member is not a Python function")
        closure_vars = inspect.getclosurevars(function)
        resolved = getattr(closure_vars, attribute)
        dependencies[key] = dict(resolved)
    return dependencies


_DECLARED_GATE_MEMBER_GLOBALS = _declared_gate_member_dependencies("globals")
_DECLARED_GATE_MEMBER_BUILTINS = _declared_gate_member_dependencies("builtins")
_DECLARED_GATE_LEAF_CLASS_IMPLEMENTATIONS = {
    dependency: _declared_class_implementation(dependency)
    for dependencies in _DECLARED_GATE_MEMBER_GLOBALS.values()
    for dependency in dependencies.values()
    if isinstance(dependency, type)
    and dependency not in (SharedRateLimitBucket, RateLimitGate)
}

# Direct standard-module dependencies reached by the original gate classes.
# A new member added to those classes without updating this manifest fails
# closed, as does an in-memory replacement of any listed member.
_DECLARED_GATE_MODULE_MEMBERS: dict[tuple[str, str], object] = {
    ("asyncio", "Lock"): asyncio.Lock,
    ("asyncio", "sleep"): asyncio.sleep,
    ("time", "monotonic"): time.monotonic,
}
_DECLARED_GATE_MODULE_MEMBER_IMPLEMENTATIONS = {
    key: _declared_module_member_implementation(member)
    for key, member in _DECLARED_GATE_MODULE_MEMBERS.items()
}


@dataclass(frozen=True, slots=True)
class ResolvedDispatchRatePolicy:
    """Immutable settings used by both dispatch behavior and authority identity."""

    backend: str
    owner: Literal["ouroboros", "runtime"]
    observed: bool
    self_governs_rate_limit: bool
    requests_per_minute: int | None
    tokens_per_minute: int | None
    window_seconds: float = RATE_LIMIT_WINDOW_SECONDS
    heartbeat_seconds: float = RATE_LIMIT_HEARTBEAT_SECONDS
    max_wait_seconds: float = RATE_LIMIT_MAX_WAIT_SECONDS
    token_estimation_version: int = RATE_LIMIT_TOKEN_ESTIMATION_VERSION
    gate_algorithm_version: int = RATE_LIMIT_GATE_ALGORITHM_VERSION

    @classmethod
    def resolve(
        cls,
        *,
        backend: str,
        self_governs_rate_limit: bool,
        requests_per_minute: int | None,
        tokens_per_minute: int | None,
    ) -> ResolvedDispatchRatePolicy:
        normalized_backend = backend.strip() or "unknown"
        if self_governs_rate_limit:
            # Ouroboros deliberately installs a dormant gate, but the runtime's
            # internal limiter is not yet exposed as a durable identity contract.
            return cls(
                backend=normalized_backend,
                owner="runtime",
                observed=False,
                self_governs_rate_limit=True,
                requests_per_minute=None,
                tokens_per_minute=None,
            )
        return cls(
            backend=normalized_backend,
            owner="ouroboros",
            observed=True,
            self_governs_rate_limit=False,
            requests_per_minute=(
                requests_per_minute
                if requests_per_minute is not None and requests_per_minute > 0
                else None
            ),
            tokens_per_minute=(
                tokens_per_minute
                if tokens_per_minute is not None and tokens_per_minute > 0
                else None
            ),
        )

    @property
    def gate_enabled(self) -> bool:
        return self.owner == "ouroboros" and (
            self.requests_per_minute is not None or self.tokens_per_minute is not None
        )

    def build_gate(self) -> RateLimitGate:
        return build_rate_limit_gate(
            self.backend,
            request_limit=self.requests_per_minute if self.owner == "ouroboros" else None,
            token_limit=self.tokens_per_minute if self.owner == "ouroboros" else None,
            window_seconds=self.window_seconds,
            max_wait_seconds=self.max_wait_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
        )

    def to_contract_data(
        self,
        *,
        gate_algorithm: Mapping[str, object] | None = None,
        token_estimator: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Serialize one immutable policy with its captured rate helpers.

        Executor construction supplies the algorithm returned by
        :func:`build_authority_bound_rate_limit_gate` and the estimator returned
        by :func:`capture_authority_bound_rate_limit_token_estimator`.  The
        serialized identity therefore describes the exact callables installed
        into that executor rather than a later mutable module global.
        """
        return {
            "version": 1,
            "backend": self.backend,
            "owner": self.owner,
            "observed": self.observed,
            "self_governs_rate_limit": self.self_governs_rate_limit,
            "requests_per_minute": self.requests_per_minute,
            "tokens_per_minute": self.tokens_per_minute,
            "gate_enabled": self.gate_enabled,
            "window_seconds": self.window_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "max_wait_seconds": self.max_wait_seconds,
            "token_estimation_version": self.token_estimation_version,
            "gate_algorithm_version": self.gate_algorithm_version,
            "gate_algorithm": dict(gate_algorithm)
            if gate_algorithm is not None
            else rate_limit_gate_algorithm_contract(),
            "token_estimator": dict(token_estimator)
            if token_estimator is not None
            else rate_limit_token_estimator_contract(),
        }


def _process_local_gate_algorithm_contract() -> dict[str, object]:
    """Return a deliberately non-portable identity for an opaque factory."""
    return {
        "version": 1,
        "observed": False,
        "instance_nonce": uuid.uuid4().hex,
    }


def _canonical_gate_factory_data(value: object, *, depth: int = 0) -> object:
    """Normalize safe data before it is digested into a factory identity.

    Values are never retained in the authority payload; this intermediate form
    exists only long enough to calculate a digest.  Unknown objects and
    credential-shaped values fail closed rather than being stringified.
    """
    if depth > _GATE_FACTORY_MAX_DEPTH:
        raise ValueError("rate gate factory data exceeds dependency depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("rate gate factory data is not finite")
        return value
    if isinstance(value, str):
        if is_sensitive_value(value):
            raise ValueError("rate gate factory data is sensitive")
        return value
    if isinstance(value, (tuple, list)):
        if len(value) > _GATE_FACTORY_MAX_ITEMS:
            raise ValueError("rate gate factory data is oversized")
        return [
            _canonical_gate_factory_data(item, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        if len(value) > _GATE_FACTORY_MAX_ITEMS:
            raise ValueError("rate gate factory data is oversized")
        items = [
            _canonical_gate_factory_data(item, depth=depth + 1)
            for item in value
        ]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
        )
    if isinstance(value, dict):
        if len(value) > _GATE_FACTORY_MAX_ITEMS:
            raise ValueError("rate gate factory data is oversized")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or is_sensitive_field(key):
                raise ValueError("rate gate factory data has an unsafe key")
            normalized[key] = _canonical_gate_factory_data(item, depth=depth + 1)
        return normalized
    raise ValueError("rate gate factory data has an unsupported value")


def _gate_factory_data_digest(value: object) -> str:
    normalized = _canonical_gate_factory_data(value)
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gate_factory_direct_module_attributes(
    function: object,
    global_name: str,
) -> list[str]:
    """Find direct ``module.attribute`` reads made by one Python function."""
    if not inspect.isfunction(function):
        raise ValueError("rate gate factory module owner is not inspectable")
    instructions = list(dis.get_instructions(function))
    attributes: set[str] = set()
    for index, instruction in enumerate(instructions):
        if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"} or instruction.argval != global_name:
            continue
        for following in instructions[index + 1 : index + 4]:
            if following.opname in {"CACHE", "EXTENDED_ARG", "PUSH_NULL"}:
                continue
            if following.opname in {"LOAD_ATTR", "LOAD_METHOD"} and isinstance(
                following.argval, str
            ):
                attributes.add(following.argval)
            break
    return sorted(attributes)


def _gate_factory_has_unbound_global(function: object, names: frozenset[str]) -> bool:
    """Distinguish a real unbound global from ordinary instance attributes."""
    if not inspect.isfunction(function):
        raise ValueError("rate gate factory owner is not inspectable")
    loaded_globals = {
        instruction.argval
        for instruction in dis.get_instructions(function)
        if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}
        and isinstance(instruction.argval, str)
    }
    return bool(loaded_globals.intersection(names))


def _gate_factory_module_member_identity(value: object) -> dict[str, object]:
    """Identify one direct module member without traversing its object graph."""
    if inspect.ismethod(value) or inspect.isfunction(value):
        function = value.__func__ if inspect.ismethod(value) else value
        assert inspect.isfunction(function)
        return {
            "kind": "function",
            "module": function.__module__,
            "qualname": function.__qualname__,
            "code_digest": "sha256:" + hashlib.sha256(
                marshal.dumps(function.__code__)
            ).hexdigest(),
            "defaults_digest": _gate_factory_data_digest(
                {
                    "positional": tuple(function.__defaults__ or ()),
                    "named": dict(function.__kwdefaults__ or {}),
                }
            ),
        }
    if inspect.isbuiltin(value):
        module = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", None)
        if not isinstance(module, str) or not module or not isinstance(qualname, str) or not qualname:
            raise ValueError("rate gate factory module builtin is not identifiable")
        return {"kind": "builtin", "module": module, "qualname": qualname}
    if isinstance(value, type):
        try:
            source = inspect.getsource(value)
        except (OSError, TypeError):
            source = None
        return {
            "kind": "class",
            "module": value.__module__,
            "qualname": value.__qualname__,
            "source_digest": (
                "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
                if source is not None
                else None
            ),
        }
    return {"kind": "data", "digest": _gate_factory_data_digest(value)}


def _gate_factory_module_identity(
    value: object,
    *,
    function: object,
    global_name: str,
    require_declared_members: bool = False,
) -> dict[str, object]:
    """Bind the precise module members read by a factory or gate-class method."""
    if not inspect.ismodule(value):
        raise ValueError("rate gate factory module dependency is invalid")
    module_name = getattr(value, "__name__", None)
    if not isinstance(module_name, str) or not module_name:
        raise ValueError("rate gate factory module has no name")
    attributes = _gate_factory_direct_module_attributes(function, global_name)
    if not attributes:
        raise ValueError("rate gate factory module is used without a bound member")
    members: dict[str, object] = {}
    for attribute in attributes:
        try:
            member = getattr(value, attribute)
            if require_declared_members:
                expected_member = _DECLARED_GATE_MODULE_MEMBERS.get((module_name, attribute))
                if expected_member is not member:
                    raise ValueError("rate gate factory module member is not declaration-bound")
                declared_implementation = _DECLARED_GATE_MODULE_MEMBER_IMPLEMENTATIONS.get(
                    (module_name, attribute)
                )
                if not _declared_module_member_implementation_is_intact(
                    member,
                    declared_implementation,
                ):
                    raise ValueError("rate gate factory module implementation drifted")
            members[attribute] = _gate_factory_module_member_identity(member)
        except Exception:
            raise ValueError("rate gate factory module member is not observable") from None
    return {"kind": "module", "name": module_name, "members": members}


def _gate_factory_function_global_identity(
    function: object,
    name: str,
    value: object,
    *,
    active: set[int],
    depth: int,
) -> dict[str, object]:
    """Bind a function global, including an accessed module's live member."""
    if inspect.ismodule(value):
        return _gate_factory_module_identity(value, function=function, global_name=name)
    return _gate_factory_dependency_identity(value, active=active, depth=depth)


def _gate_factory_class_member_global_identity(
    function: object,
    name: str,
    value: object,
    *,
    active: set[int],
    depth: int,
) -> dict[str, object]:
    """Bind a member global without recursively expanding incidental classes.

    The factory's direct classes receive a full current-member graph.  Classes
    referenced *inside* those member methods (for example immutable result
    dataclasses) are leaf dependencies: their implementation/source identity
    still detects replacement without traversing generated dataclass helpers.
    """
    if inspect.ismodule(value):
        return _gate_factory_module_identity(
            value,
            function=function,
            global_name=name,
            require_declared_members=True,
        )
    if isinstance(value, type):
        declared_implementation = _DECLARED_GATE_LEAF_CLASS_IMPLEMENTATIONS.get(value)
        if (
            declared_implementation is not None
            and not _declared_class_implementation_is_intact(value, declared_implementation)
        ):
            raise ValueError("rate gate factory result type implementation drifted")
        return _gate_factory_module_member_identity(value)
    return _gate_factory_dependency_identity(value, active=active, depth=depth)


def _gate_factory_class_member_identity(
    value: object,
    *,
    active: set[int],
    depth: int,
    expected_globals: dict[str, object],
    expected_builtins: dict[str, object],
) -> dict[str, object]:
    """Bind a class member's currently installed executable state.

    ``inspect.getsource(class)`` continues to describe the original declaration
    after a test or plugin replaces ``Class.__init__`` in memory.  Record each
    current function's bytecode and its safe bound state as well, so direct
    runtime member replacement cannot retain the original factory identity.
    """
    function = value.__func__ if inspect.ismethod(value) else value
    if not inspect.isfunction(function):
        raise ValueError("rate gate factory class member is not inspectable")
    if depth > _GATE_FACTORY_MAX_DEPTH:
        raise ValueError("rate gate factory class member exceeds dependency depth")
    closure_vars = inspect.getclosurevars(function)
    if _gate_factory_has_unbound_global(function, closure_vars.unbound):
        raise ValueError("rate gate factory class member has unbound dependencies")
    if (
        set(closure_vars.globals) != set(expected_globals)
        or any(expected_globals[name] is not dependency for name, dependency in closure_vars.globals.items())
        or set(closure_vars.builtins) != set(expected_builtins)
        or any(expected_builtins[name] is not dependency for name, dependency in closure_vars.builtins.items())
    ):
        raise ValueError("rate gate factory class member dependencies drifted")
    return {
        "module": function.__module__,
        "qualname": function.__qualname__,
        "code_digest": "sha256:" + hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
        "defaults_digest": _gate_factory_data_digest(
            {
                "positional": tuple(function.__defaults__ or ()),
                "named": dict(function.__kwdefaults__ or {}),
            }
        ),
        "closures": {
            name: _gate_factory_dependency_identity(
                dependency,
                active=active,
                depth=depth + 1,
            )
            for name, dependency in sorted(closure_vars.nonlocals.items())
        },
        "globals": {
            name: _gate_factory_class_member_global_identity(
                function,
                name,
                dependency,
                active=active,
                depth=depth + 1,
            )
            for name, dependency in sorted(closure_vars.globals.items())
        },
        "builtins": {
            name: _gate_factory_dependency_identity(
                dependency,
                active=active,
                depth=depth + 1,
            )
            for name, dependency in sorted(closure_vars.builtins.items())
        },
    }


def _gate_factory_class_identity(value: type[object]) -> dict[str, object]:
    """Bind a direct class dependency and its current executable members."""
    try:
        source = inspect.getsource(value)
    except (OSError, TypeError):
        raise ValueError("rate gate factory class is not inspectable") from None
    members: dict[str, object] = {}
    for runtime_class in value.__mro__:
        if runtime_class is object:
            continue
        expected_member_keys = {
            key
            for key in _DECLARED_GATE_CLASS_MEMBERS
            if key[0] is runtime_class
        }
        if not expected_member_keys:
            raise ValueError("rate gate factory class is not declaration-bound")
        expected_raw_items = _DECLARED_GATE_CLASS_RAW_ITEMS[runtime_class]
        current_raw_items = dict(vars(runtime_class))
        if (
            set(current_raw_items) != set(expected_raw_items)
            or any(
                expected_raw_items[name] is not current
                for name, current in current_raw_items.items()
            )
        ):
            # Special descriptors such as ``__getattribute__`` can alter a
            # gate's visible methods while never appearing as a conventional
            # function/classmethod/property member. Any class-dict drift is
            # live process behavior, not portable factory identity.
            raise ValueError("rate gate factory class dictionary drifted")
        observed_member_keys: set[tuple[type[object], str, int]] = set()
        qualified_name = f"{runtime_class.__module__}.{runtime_class.__qualname__}"
        for member_name, raw_member in vars(runtime_class).items():
            expected_raw_member = _DECLARED_GATE_CLASS_RAW_MEMBERS.get(
                (runtime_class, member_name)
            )
            if expected_raw_member is not None and expected_raw_member is not raw_member:
                # A descriptor can expose the original ``__func__`` while
                # changing binding behavior in ``__get__``. The descriptor
                # object itself is therefore part of static gate identity.
                raise ValueError("rate gate factory class descriptor drifted")
            targets: tuple[object, ...]
            if isinstance(raw_member, (classmethod, staticmethod)):
                targets = (raw_member.__func__,)
            elif isinstance(raw_member, property):
                targets = tuple(
                    target
                    for target in (raw_member.fget, raw_member.fset, raw_member.fdel)
                    if target is not None
                )
            elif inspect.isfunction(raw_member):
                targets = (raw_member,)
            else:
                continue
            for index, target in enumerate(targets):
                member_key = (runtime_class, member_name, index)
                observed_member_keys.add(member_key)
                expected_target = _DECLARED_GATE_CLASS_MEMBERS.get(member_key)
                if expected_target is not target:
                    # A dynamically injected member can close over or read
                    # arbitrary process state. Display metadata is insufficient
                    # because ``functools.wraps`` can copy the original module
                    # and qualname; only the import-time declaration is portable.
                    raise ValueError("rate gate factory class member is not declaration-bound")
                declared_implementation = _DECLARED_GATE_MEMBER_IMPLEMENTATIONS.get(member_key)
                if (
                    declared_implementation is None
                    or not _declared_function_implementation_is_intact(
                        target,
                        declared_implementation,
                    )
                ):
                    # A function object can retain object identity after its
                    # ``__code__`` or defaults have been changed in place.
                    # That is live process behavior, not the imported gate
                    # declaration Foundation A is allowed to make portable.
                    raise ValueError("rate gate factory class implementation drifted")
                members[f"{qualified_name}.{member_name}:{index}"] = (
                    _gate_factory_class_member_identity(
                        target,
                        active=set(),
                        depth=0,
                        expected_globals=_DECLARED_GATE_MEMBER_GLOBALS[member_key],
                        expected_builtins=_DECLARED_GATE_MEMBER_BUILTINS[member_key],
                    )
                )
        if observed_member_keys != expected_member_keys:
            raise ValueError("rate gate factory class members are incomplete")
    return {
        "kind": "class",
        "module": value.__module__,
        "qualname": value.__qualname__,
        "source_digest": "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "members": members,
    }


def _gate_factory_function_identity(
    value: object,
    *,
    active: set[int],
    depth: int,
) -> dict[str, object]:
    """Bind code plus every directly-resolved Python dependency.

    ``__code__`` alone is not behavior: two closures can have identical bytecode
    yet install gates with different limits.  Defaults, closures, globals, and
    builtins are therefore represented by safe nested identities.  Anything we
    cannot inspect becomes process-local through the public caller.
    """
    function = value.__func__ if inspect.ismethod(value) else value
    if not inspect.isfunction(function):
        raise ValueError("rate gate factory function is not inspectable")
    if depth > _GATE_FACTORY_MAX_DEPTH:
        raise ValueError("rate gate factory exceeds dependency depth")
    code = function.__code__
    code_digest = "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest()
    function_id = id(function)
    if function_id in active:
        return {
            "kind": "recursive_function",
            "module": function.__module__,
            "qualname": function.__qualname__,
            "code_digest": code_digest,
        }

    active.add(function_id)
    try:
        closure_vars = inspect.getclosurevars(function)
        if _gate_factory_has_unbound_global(function, closure_vars.unbound):
            raise ValueError("rate gate factory has unbound dependencies")
        return {
            "kind": "function",
            "module": function.__module__,
            "qualname": function.__qualname__,
            "code_digest": code_digest,
            "defaults": _gate_factory_dependency_identity(
                tuple(function.__defaults__ or ()),
                active=active,
                depth=depth + 1,
            ),
            "kwdefaults": _gate_factory_dependency_identity(
                dict(function.__kwdefaults__ or {}),
                active=active,
                depth=depth + 1,
            ),
            "closures": {
                name: _gate_factory_dependency_identity(
                    dependency,
                    active=active,
                    depth=depth + 1,
                )
                for name, dependency in sorted(closure_vars.nonlocals.items())
            },
            "globals": {
                name: _gate_factory_function_global_identity(
                    function,
                    name,
                    dependency,
                    active=active,
                    depth=depth + 1,
                )
                for name, dependency in sorted(closure_vars.globals.items())
            },
            "builtins": {
                name: _gate_factory_dependency_identity(
                    dependency,
                    active=active,
                    depth=depth + 1,
                )
                for name, dependency in sorted(closure_vars.builtins.items())
            },
        }
    finally:
        active.remove(function_id)


def _gate_factory_dependency_identity(
    value: object,
    *,
    active: set[int],
    depth: int,
) -> dict[str, object]:
    """Return a safe, digest-only identity for one factory dependency."""
    if inspect.ismethod(value) or inspect.isfunction(value):
        return _gate_factory_function_identity(value, active=active, depth=depth)
    if inspect.isbuiltin(value):
        module = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", None)
        if not isinstance(module, str) or not module or not isinstance(qualname, str) or not qualname:
            raise ValueError("rate gate factory builtin is not identifiable")
        return {"kind": "builtin", "module": module, "qualname": qualname}
    if isinstance(value, type):
        return _gate_factory_class_identity(value)
    return {"kind": "data", "digest": _gate_factory_data_digest(value)}


def rate_limit_gate_algorithm_contract(
    factory: object | None = None,
) -> dict[str, object]:
    """Fingerprint one captured gate factory and its bound behavior.

    ``factory`` deliberately defaults to the module symbol for standalone
    inspection, but executor construction passes the callable captured *before*
    invoking it.  A factory can otherwise restore that module symbol while
    returning a behaviorally different gate, leaving a time-of-check/time-of-use
    hole between construction and authority serialization.
    """
    target = build_rate_limit_gate if factory is None else factory
    declared_implementation = _DECLARED_RATE_GATE_FACTORY_IMPLEMENTATION
    if (
        target is not _DECLARED_RATE_GATE_FACTORY
        or declared_implementation is None
        or not _declared_function_implementation_is_intact(target, declared_implementation)
    ):
        # A test/plugin may deliberately replace the factory.  It can still
        # execute, but arbitrary callable graphs can hide mutable module,
        # descriptor, or foreign-runtime state that Foundation A must not
        # promote into a portable identity.
        return _process_local_gate_algorithm_contract()
    try:
        identity = _gate_factory_function_identity(target, active=set(), depth=0)
        module = identity.get("module")
        qualname = identity.get("qualname")
        if not isinstance(module, str) or not module or not isinstance(qualname, str) or not qualname:
            raise ValueError("rate gate factory lacks a qualified identity")
        payload = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except Exception:
        return _process_local_gate_algorithm_contract()
    return {
        "version": 1,
        "observed": True,
        # Do not expose callable display metadata in authority JSON.  Dynamic
        # plugin labels are provider-controlled input and can contain a token.
        "identity_digest": "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"module": module, "qualname": qualname},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest(),
        "digest": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def build_rate_limit_gate(
    runtime_backend: str,
    *,
    request_limit: int | None,
    token_limit: int | None,
    window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    max_wait_seconds: float = RATE_LIMIT_MAX_WAIT_SECONDS,
    heartbeat_seconds: float = RATE_LIMIT_HEARTBEAT_SECONDS,
    sleep: Callable[[float], Any] | None = None,
) -> RateLimitGate:
    """Build a :class:`RateLimitGate` over a fresh shared bucket.

    With ``request_limit`` and ``token_limit`` both ``None`` the resulting gate
    is dormant — the intended default for backends with no configured limits.
    """
    bucket = SharedRateLimitBucket(
        runtime_backend=runtime_backend,
        request_limit=request_limit,
        token_limit=token_limit,
        window_seconds=window_seconds,
    )
    return RateLimitGate(
        bucket,
        max_wait_seconds=max_wait_seconds,
        heartbeat_seconds=heartbeat_seconds,
        sleep=sleep,
    )


# Capture this only after the function declaration exists.  The comparison in
# ``rate_limit_gate_algorithm_contract`` is object identity on purpose: copied
# code, ``functools.wraps`` metadata, or an equivalent closure is still a live
# replacement rather than the declared portable implementation.
_DECLARED_RATE_GATE_FACTORY = build_rate_limit_gate
_DECLARED_RATE_GATE_FACTORY_IMPLEMENTATION = _declared_function_implementation(
    build_rate_limit_gate
)
_DECLARED_RATE_LIMIT_GATE_TYPE = RateLimitGate
_DECLARED_SHARED_RATE_LIMIT_BUCKET_TYPE = SharedRateLimitBucket


def estimate_runtime_request_tokens(
    prompt: str,
    *,
    system_prompt: str | None = None,
) -> int:
    """Estimate the cost of starting one runtime request."""
    prompt_chars = len(prompt)
    system_chars = len(system_prompt or "")
    prompt_tokens = (prompt_chars + system_chars) // _TOKEN_ESTIMATE_DIVISOR
    return max(1, prompt_tokens + _TOKEN_COMPLETION_CUSHION)


_DECLARED_RATE_TOKEN_ESTIMATOR = estimate_runtime_request_tokens
_DECLARED_RATE_TOKEN_ESTIMATOR_IMPLEMENTATION = _declared_function_implementation(
    estimate_runtime_request_tokens
)


def _rate_limit_gate_matches_policy(
    gate: object,
    *,
    runtime_backend: str,
    request_limit: int | None,
    token_limit: int | None,
    window_seconds: float,
    max_wait_seconds: float,
    heartbeat_seconds: float,
) -> bool:
    """Check that an installed gate is exactly the policy that was authorized.

    The factory is intentionally replaceable for in-process experimentation, but
    Foundation A must not call a replacement and then serialize the original
    policy as though it still governed dispatch.  Direct attribute reads avoid
    an overridden descriptor manufacturing a friendly-looking view of a gate.
    """
    if type(gate) is not _DECLARED_RATE_LIMIT_GATE_TYPE:
        return False
    try:
        bucket = object.__getattribute__(gate, "_bucket")
        if type(bucket) is not _DECLARED_SHARED_RATE_LIMIT_BUCKET_TYPE:
            return False
        return (
            object.__getattribute__(bucket, "_runtime_backend") == runtime_backend
            and object.__getattribute__(bucket, "_request_limit") == request_limit
            and object.__getattribute__(bucket, "_token_limit") == token_limit
            and object.__getattribute__(bucket, "_window_seconds") == window_seconds
            and object.__getattribute__(gate, "_max_wait_seconds") == max_wait_seconds
            and object.__getattribute__(gate, "_heartbeat_seconds") == heartbeat_seconds
        )
    except Exception:
        return False


def build_authority_bound_rate_limit_gate(
    runtime_backend: str,
    *,
    request_limit: int | None,
    token_limit: int | None,
    window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    max_wait_seconds: float = RATE_LIMIT_MAX_WAIT_SECONDS,
    heartbeat_seconds: float = RATE_LIMIT_HEARTBEAT_SECONDS,
) -> AuthorityBoundRateLimitGate:
    """Build a gate and capture the exact live state that dispatch will use.

    The callable is captured before it is invoked and the same capture is used
    for the algorithm contract afterwards.  This closes the mutable-global
    alias/TOCTOU path where a factory can swap ``build_rate_limit_gate`` back to
    the declared implementation before authority serialization.
    """
    factory = build_rate_limit_gate
    if not callable(factory):
        raise ValueError("dispatch rate gate factory is not callable")
    gate = factory(
        runtime_backend,
        request_limit=request_limit,
        token_limit=token_limit,
        window_seconds=window_seconds,
        max_wait_seconds=max_wait_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    if type(gate) is not _DECLARED_RATE_LIMIT_GATE_TYPE:
        raise ValueError("dispatch rate gate factory returned an unsupported gate type")
    algorithm = rate_limit_gate_algorithm_contract(factory)
    if not _rate_limit_gate_matches_policy(
        gate,
        runtime_backend=runtime_backend,
        request_limit=request_limit,
        token_limit=token_limit,
        window_seconds=window_seconds,
        max_wait_seconds=max_wait_seconds,
        heartbeat_seconds=heartbeat_seconds,
    ):
        # The gate remains usable for this live process, but it cannot be
        # promoted to portable authority because policy and behavior disagree.
        algorithm = _process_local_gate_algorithm_contract()
    try:
        bucket = object.__getattribute__(gate, "_bucket")
        acquire = object.__getattribute__(gate, "acquire")
        sleep = object.__getattribute__(gate, "_sleep")
        clock = object.__getattribute__(bucket, "_time")
        lock = object.__getattribute__(bucket, "_lock")
        reservations = object.__getattribute__(bucket, "_reservations")
    except Exception:
        raise ValueError("dispatch rate gate state is not observable") from None
    if not callable(acquire):
        raise ValueError("dispatch rate gate acquire is not callable")
    return AuthorityBoundRateLimitGate(
        gate=gate,
        algorithm=algorithm,
        factory=factory,
        acquire=acquire,
        bucket=bucket,
        runtime_backend=runtime_backend,
        request_limit=request_limit,
        token_limit=token_limit,
        window_seconds=window_seconds,
        max_wait_seconds=max_wait_seconds,
        heartbeat_seconds=heartbeat_seconds,
        sleep=sleep,
        clock=clock,
        lock=lock,
        reservations=reservations,
    )


def _same_bound_callable(current: object, captured: object) -> bool:
    """Compare a regenerated bound method without trusting display metadata."""
    current_self = getattr(current, "__self__", None)
    captured_self = getattr(captured, "__self__", None)
    current_function = getattr(current, "__func__", None)
    captured_function = getattr(captured, "__func__", None)
    if current_self is not None or captured_self is not None:
        return current_self is captured_self and current_function is captured_function
    return current is captured


def authority_bound_rate_limit_gate_is_intact(binding: AuthorityBoundRateLimitGate) -> bool:
    """Reject post-construction gate, class, or instance-state drift.

    A portable algorithm must still match the import-time implementation just
    before dispatch. Independently, every installed gate verifies its captured
    policy fields and live collaborators. This catches a later replacement of
    ``RateLimitGate.acquire``/``enabled`` as well as an in-place bucket-limit,
    clock, sleeper, lock, or queue swap.
    """
    if binding.algorithm.get("observed") is True and (
        rate_limit_gate_algorithm_contract(binding.factory) != binding.algorithm
    ):
        return False
    gate = binding.gate
    if type(gate) is not _DECLARED_RATE_LIMIT_GATE_TYPE:
        return False
    try:
        bucket = object.__getattribute__(gate, "_bucket")
        if bucket is not binding.bucket or type(bucket) is not _DECLARED_SHARED_RATE_LIMIT_BUCKET_TYPE:
            return False
        if not _same_bound_callable(object.__getattribute__(gate, "acquire"), binding.acquire):
            return False
        if object.__getattribute__(gate, "_sleep") is not binding.sleep:
            return False
        if object.__getattribute__(bucket, "_time") is not binding.clock:
            return False
        if object.__getattribute__(bucket, "_lock") is not binding.lock:
            return False
        if object.__getattribute__(bucket, "_reservations") is not binding.reservations:
            return False
    except Exception:
        return False
    return _rate_limit_gate_matches_policy(
        gate,
        runtime_backend=binding.runtime_backend,
        request_limit=binding.request_limit,
        token_limit=binding.token_limit,
        window_seconds=binding.window_seconds,
        max_wait_seconds=binding.max_wait_seconds,
        heartbeat_seconds=binding.heartbeat_seconds,
    )


def rate_limit_token_estimator_contract(
    estimator: object | None = None,
) -> dict[str, object]:
    """Fingerprint a captured token estimator without exposing its labels."""
    target = estimate_runtime_request_tokens if estimator is None else estimator
    declared = _DECLARED_RATE_TOKEN_ESTIMATOR_IMPLEMENTATION
    if (
        target is not _DECLARED_RATE_TOKEN_ESTIMATOR
        or declared is None
        or not _declared_function_implementation_is_intact(target, declared)
    ):
        return _process_local_gate_algorithm_contract()
    try:
        identity = _gate_factory_function_identity(target, active=set(), depth=0)
        payload = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except Exception:
        return _process_local_gate_algorithm_contract()
    return {
        "version": 1,
        "observed": True,
        "identity_digest": "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "module": getattr(target, "__module__", None),
                    "qualname": getattr(target, "__qualname__", None),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest(),
        "digest": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def capture_authority_bound_rate_limit_token_estimator(
) -> tuple[Callable[[str], int], dict[str, object]]:
    """Capture the estimator that dispatch will actually call.

    The returned callable is stored by :class:`ParallelACExecutor`; later
    module-global rebinding cannot silently change its TPM accounting.
    """
    estimator = estimate_runtime_request_tokens
    if not callable(estimator):
        raise ValueError("dispatch token estimator is not callable")
    return estimator, rate_limit_token_estimator_contract(estimator)


def authority_bound_rate_limit_token_estimator_is_intact(
    estimator: object,
    contract: Mapping[str, object],
) -> bool:
    """Revalidate a portable estimator immediately before dispatch."""
    if contract.get("observed") is not True:
        return True
    return rate_limit_token_estimator_contract(estimator) == dict(contract)


__all__ = [
    "DEFAULT_ANTHROPIC_RPM_CEILING",
    "DEFAULT_ANTHROPIC_TPM_CEILING",
    "AuthorityBoundRateLimitGate",
    "RATE_LIMIT_HEARTBEAT_SECONDS",
    "RATE_LIMIT_GATE_ALGORITHM_VERSION",
    "RATE_LIMIT_MAX_WAIT_SECONDS",
    "RATE_LIMIT_TOKEN_ESTIMATION_VERSION",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RateLimitBackoff",
    "RateLimitGate",
    "RateLimitSnapshot",
    "ResolvedDispatchRatePolicy",
    "SharedRateLimitBucket",
    "authority_bound_rate_limit_gate_is_intact",
    "authority_bound_rate_limit_token_estimator_is_intact",
    "build_authority_bound_rate_limit_gate",
    "build_rate_limit_gate",
    "capture_authority_bound_rate_limit_token_estimator",
    "estimate_runtime_request_tokens",
    "rate_limit_gate_algorithm_contract",
    "rate_limit_token_estimator_contract",
]
