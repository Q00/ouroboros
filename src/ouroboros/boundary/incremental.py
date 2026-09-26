"""Incremental construction: one constructor call per criterion, kept as produced.

The product constructor asks for each criterion's oracle in its own read-only
call, a few at a time, under one shared wall-clock budget. Each criterion's
reply is written to ``partial_dir`` (outside every checkout) the moment it is
parsed, so a construction that runs out of time keeps every oracle already
produced. When the budget ends, the criteria still pending are listed as
uncovered with reason ``construction_timeout`` (tier ``U``); a criterion
whose call failed or whose reply did not parse is uncovered with its typed
reason. Only when no criterion produced anything is the whole construction a
failure (``constructor_timeout`` or the first typed failure), which lets the
product regenerate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.package import CheckPackageError, sha256_bytes

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
    reply_sha256: str | None = None
    input_digest: str | None = None
    generator: str | None = None


def restrict_reply(reply: Mapping[str, Any], criterion: int) -> dict[str, Any]:
    """The part of a reply that concerns ``criterion`` (1-based) only."""

    def number(entry: Any) -> Any:
        return entry.get("criterion") if isinstance(entry, Mapping) else None

    oracles = [item for item in reply.get("oracles") or () if number(item) == criterion]
    checks = [
        item
        for item in reply.get("checks") or ()
        if isinstance(item, Mapping)
        and item.get("assertions")
        and all(number(link) == criterion for link in item.get("assertions") or ())
    ]
    paths = {arg for item in checks for arg in (item.get("argv") or [])[1:] if isinstance(arg, str)}
    files = [
        item
        for item in reply.get("files") or ()
        if isinstance(item, Mapping) and item.get("path") in paths
    ]
    uncovered = [item for item in reply.get("uncovered") or () if number(item) == criterion]
    return {"oracles": oracles, "checks": checks, "files": files, "uncovered": uncovered}


def merge_pieces(pieces: Sequence[CriterionPiece]) -> tuple[dict[str, Any], dict[int, str]]:
    """One reply from the pieces, plus the uncovered reason of every missing criterion.

    A piece whose check id or file path collides with an earlier piece is
    dropped (its criterion becomes uncovered, ``constructor_conflict``).
    """
    merged: dict[str, list[Any]] = {"oracles": [], "checks": [], "files": [], "uncovered": []}
    missing: dict[int, str] = {}
    check_ids: set[str] = set()
    paths: set[str] = set()
    for piece in sorted(pieces, key=lambda item: item.criterion):
        if piece.status != "ok" or piece.reply is None:
            missing[piece.criterion] = (
                CONSTRUCTION_TIMEOUT
                if piece.status == "timeout"
                else f"constructor_failed:{(piece.reason or 'unknown')[:_REASON_CHARS]}"
            )
            continue
        ids = {
            str(item.get("check_id") or f"oracle_{piece.criterion}")
            for item in piece.reply["oracles"]
        } | {str(item.get("check_id")) for item in piece.reply["checks"]}
        piece_paths = {str(item.get("path")) for item in piece.reply["files"]}
        if ids & check_ids or piece_paths & paths:
            missing[piece.criterion] = "constructor_conflict"
            continue
        check_ids |= ids
        paths |= piece_paths
        for key in merged:
            merged[key].extend(piece.reply[key])
    merged["uncovered"].extend(
        {"criterion": number, "reason": reason} for number, reason in sorted(missing.items())
    )
    return merged, missing


def persist_piece(partial_dir: Path | None, piece: CriterionPiece) -> None:
    """Write one criterion's parsed reply as soon as it exists."""
    if partial_dir is None:
        return
    partial_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "criterion": piece.criterion,
        "status": piece.status,
        "reason": piece.reason,
        "reply_sha256": piece.reply_sha256,
        "input_digest": piece.input_digest,
        "generator": piece.generator,
        "reply": piece.reply,
    }
    target = partial_dir / f"criterion-{piece.criterion:03d}.json"
    target.write_text(json.dumps(body, sort_keys=True, indent=1), encoding="utf-8")


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
    partial_dir: Path | None,
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
            blocker = preflight_agent_runtime(runtime)
            if blocker is not None:
                return CriterionPiece(
                    number,
                    "failed",
                    reason=f"constructor_runtime_unavailable:{blocker}",
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
        reply_sha = sha256_bytes(reply.encode("utf-8"))
        if len(reply) > constructor._max_output_chars:
            piece = CriterionPiece(
                number,
                "failed",
                reason="constructor_reply_too_large",
                reply_sha256=reply_sha,
                input_digest=digest,
                generator=label,
            )
        else:
            try:
                restricted = restrict_reply(extract(reply), number)
                validate(restricted)
                piece = CriterionPiece(
                    number,
                    "ok",
                    reply=restricted,
                    reply_sha256=reply_sha,
                    input_digest=digest,
                    generator=label,
                )
            except CheckPackageError as exc:
                piece = CriterionPiece(
                    number,
                    "failed",
                    reason=f"constructor_reply_invalid:{exc}",
                    reply_sha256=reply_sha,
                    input_digest=digest,
                    generator=label,
                )
        persist_piece(partial_dir, piece)
        return piece

    return list(await asyncio.gather(*(one(number) for number in range(1, total + 1))))


__all__ = [
    "CONSTRUCTION_TIMEOUT",
    "CriterionPiece",
    "construct_pieces",
    "merge_pieces",
    "persist_piece",
    "restrict_reply",
]
