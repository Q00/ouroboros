"""Incremental construction: one constructor call per criterion, kept as produced.

The product constructor asks for each criterion's oracle in its own read-only
call, a few at a time, under one shared wall-clock budget. Each criterion's
reply is kept in memory as soon as it is parsed, so a construction that runs
out of time keeps every oracle already produced; nothing of a reply is
written to disk (it holds held-out inputs and expected values). Every reply
goes through ``oracle_build.normalize_reply`` before it is restricted to its
criterion. When the budget ends, the criteria still pending are listed as
uncovered with reason ``construction_timeout`` (tier ``U``); a criterion
whose call failed or whose reply was refused is uncovered with its typed
reason (a refused reply: ``constructor_reply_invalid:<code>``, a closed code
that carries no text from the reply). Only when no criterion produced anything is the whole construction a
failure (``constructor_timeout`` or the first typed failure), which lets the
product regenerate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.constructor_session import disable_session_persistence
from ouroboros.boundary.oracle_build import normalize_reply, reply_failure_reason
from ouroboros.boundary.package import CheckPackageError
from ouroboros.boundary.target_commands import declared_test_command

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed

CONSTRUCTION_TIMEOUT = "construction_timeout"
DEFAULT_CONCURRENCY = 3
_REASON_CHARS = 160


@dataclass(frozen=True, slots=True)
class CriterionPiece:
    """What one criterion's call produced."""

    criterion: int
    status: str  # "ok", "timeout", or "failed"
    reply: dict[str, Any] | None = None
    reason: str | None = None
    input_digest: str | None = None
    generator: str | None = None
    test_command: str | None = None
    """The test command template the reply declared (``target_commands``)."""


def restrict_reply_to(reply: Mapping[str, Any], criteria: Collection[int]) -> dict[str, Any]:
    """The part of a normalized reply (``oracle_build.normalize_reply``) for ``criteria``.

    One pass against the whole allowed set: a script check is kept when it
    links at least one criterion and every criterion it links is allowed, so
    a script asserting several allowed criteria keeps all its links, and one
    reaching any other criterion is dropped with its files.
    """
    allowed = frozenset(criteria)

    def number(entry: Any) -> Any:
        return entry.get("criterion") if isinstance(entry, Mapping) else None

    oracles = [item for item in reply.get("oracles") or () if number(item) in allowed]
    checks = [
        item
        for item in reply.get("checks") or ()
        if isinstance(item, Mapping)
        and item.get("assertions")
        and all(number(link) in allowed for link in item.get("assertions") or ())
    ]
    paths = {arg for item in checks for arg in (item.get("argv") or [])[1:] if isinstance(arg, str)}
    files = [
        item
        for item in reply.get("files") or ()
        if isinstance(item, Mapping) and item.get("path") in paths
    ]
    uncovered = [item for item in reply.get("uncovered") or () if number(item) in allowed]
    return {"oracles": oracles, "checks": checks, "files": files, "uncovered": uncovered}


def restrict_reply(reply: Mapping[str, Any], criterion: int) -> dict[str, Any]:
    """The part of a normalized reply for ``criterion`` only (``restrict_reply_to``)."""
    return restrict_reply_to(reply, (criterion,))


def merge_pieces(pieces: Sequence[CriterionPiece]) -> tuple[dict[str, Any], dict[int, str]]:
    """One reply from the pieces, plus the product's reason for every missing criterion.

    The reasons stay apart from the merged reply (``package_from_reply``'s
    ``product_uncovered``), so they are never mistaken for the constructor's
    own ``uncovered`` declarations.

    A piece whose file path collides with an earlier piece is dropped (its
    criterion becomes uncovered, ``constructor_conflict``). Check ids cannot
    collide: ``package_from_reply`` re-mints them over the merged reply.
    """
    merged: dict[str, list[Any]] = {"oracles": [], "checks": [], "files": [], "uncovered": []}
    missing: dict[int, str] = {}
    paths: set[str] = set()
    for piece in sorted(pieces, key=lambda item: item.criterion):
        if piece.status != "ok" or piece.reply is None:
            missing[piece.criterion] = (
                CONSTRUCTION_TIMEOUT
                if piece.status == "timeout"
                else f"constructor_failed:{(piece.reason or 'unknown')[:_REASON_CHARS]}"
            )
            continue
        piece_paths = {str(item.get("path")) for item in piece.reply["files"]}
        if piece_paths & paths:
            missing[piece.criterion] = "constructor_conflict"
            continue
        paths |= piece_paths
        for key in merged:
            merged[key].extend(piece.reply.get(key) or ())
    return merged, missing


async def construct_pieces(
    constructor: Any,
    seed: Seed,
    view: Path,
    *,
    system_prompt: str,
    prompt_for: Callable[[int], str],
    input_digest_for: Callable[[str, str], str],
    extract: Callable[[str], dict[str, Any]],
    validate: Callable[[dict[str, Any]], None],
    concurrency: int = DEFAULT_CONCURRENCY,
    tools: Sequence[str] = (),
) -> list[CriterionPiece]:
    """Run one read-only call per criterion under one shared deadline.

    ``constructor`` supplies ``_create_runtime(cwd)``, ``_timeout``,
    ``_max_output_chars`` and ``generator`` (and, when present,
    ``_resolved_generator(runtime)`` and ``_observed_generator(messages,
    requested)``). ``validate`` raises ``CheckPackageError`` on a piece that
    does not parse.
    """
    from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(constructor._timeout)
    gate = asyncio.Semaphore(max(1, concurrency))
    total = len(seed.acceptance_criteria)

    async def one(number: int) -> CriterionPiece:
        async with gate:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return CriterionPiece(number, "timeout", reason=CONSTRUCTION_TIMEOUT)
            prompt = prompt_for(number)
            runtime = await asyncio.to_thread(constructor._create_runtime, view)
            resolver = getattr(constructor, "_resolved_generator", None)
            label = resolver(runtime) if resolver is not None else constructor.generator
            digest = input_digest_for(prompt, label)
            # The reply holds every held-out case: no session may reach disk.
            refusal = disable_session_persistence(runtime)
            blocker = preflight_agent_runtime(runtime) if refusal is None else None
            if refusal is not None or blocker is not None:
                return CriterionPiece(
                    number,
                    "failed",
                    reason=refusal or f"constructor_runtime_unavailable:{blocker}",
                    input_digest=digest,
                    generator=label,
                )
            try:
                result = await asyncio.wait_for(
                    runtime.execute_task_to_result(
                        prompt, tools=list(tools), system_prompt=system_prompt
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                return CriterionPiece(
                    number,
                    "timeout",
                    reason=CONSTRUCTION_TIMEOUT,
                    input_digest=digest,
                    generator=label,
                )
            except Exception as exc:  # noqa: BLE001 - a failed call is a typed reason
                return CriterionPiece(
                    number,
                    "failed",
                    reason=f"constructor_call_failed:{type(exc).__name__}",
                    input_digest=digest,
                    generator=label,
                )
            finally:
                closer = getattr(runtime, "aclose", None)
                if closer is not None and inspect.iscoroutinefunction(closer):
                    await closer()
        if result.is_err:
            return CriterionPiece(
                number,
                "failed",
                reason=f"constructor_call_failed:{type(result.error).__name__}",
                input_digest=digest,
                generator=label,
            )
        observe = getattr(constructor, "_observed_generator", None)
        if observe is not None:
            label = observe(result.value.messages or (), label)
        reply = result.value.final_message or ""
        if len(reply) > constructor._max_output_chars:
            return CriterionPiece(
                number,
                "failed",
                reason="constructor_reply_too_large",
                input_digest=digest,
                generator=label,
            )
        try:
            raw = extract(reply)
            restricted = restrict_reply(normalize_reply(raw), number)
            validate(restricted)
            return CriterionPiece(
                number,
                "ok",
                reply=restricted,
                input_digest=digest,
                generator=label,
                test_command=declared_test_command(raw),
            )
        except CheckPackageError as exc:
            return CriterionPiece(
                number,
                "failed",
                reason=reply_failure_reason(exc),
                input_digest=digest,
                generator=label,
            )

    return list(await asyncio.gather(*(one(number) for number in range(1, total + 1))))


__all__ = [
    "CONSTRUCTION_TIMEOUT",
    "CriterionPiece",
    "construct_pieces",
    "merge_pieces",
    "restrict_reply",
    "restrict_reply_to",
]
