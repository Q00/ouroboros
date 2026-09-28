"""Check construction: one read-only model call that turns a Seed into a package.

The constructor runs after the Seed is frozen and before any worker starts. It
gives the run's configured agent runtime a read-only view of an isolated copy
of the base checkout plus the Seed's goal, constraints, and acceptance
criteria, and asks for one JSON object describing executable checks. The reply
is parsed into a ``CheckPackage`` linked to the Seed digest and criterion
keys. Criteria the reply does not link are recorded as uncovered obligations,
never dropped.

Isolation of the call itself:

- the runtime is created with permission mode ``default`` (the read-only
  sandbox class) and only the ``Read``/``Glob``/``Grep`` tools;
- its working directory is a throwaway copy of the base checkout, so even a
  runtime that ignores both restrictions cannot touch the user's files;
- the base checkout digest is taken before and after the call and a change
  fails construction.

Budget: one call per attempt, bounded by ``timeout_seconds`` wall clock and by
``max_output_chars`` of parsed reply. Turn and token limits are not uniform
across CLI runtimes, so none are claimed here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from ouroboros.boundary.constructor_session import disable_session_persistence
from ouroboros.boundary.incremental import construct_pieces, merge_pieces, restrict_reply_to
from ouroboros.boundary.oracle_build import (
    ReplyError,
    ReplyFailure,
    normalize_reply,
    package_from_reply,
    reply_failure_reason,
)
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    canonical_json_bytes,
    seed_criterion_keys,
    seed_digest,
    sha256_bytes,
)
from ouroboros.boundary.reference_check import OracleReference, references_from_reply
from ouroboros.boundary.tree import copy_checkout, tree_digest
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

CONSTRUCTOR_AGENT = "check-constructor"
CONSTRUCTOR_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
CONSTRUCTOR_PERMISSION_MODE = "default"
DEFAULT_CONSTRUCTOR_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_CHARS = 200_000
ALL_CRITERIA_UNCOVERED = "constructor_all_criteria_uncovered"


@dataclass(frozen=True, slots=True)
class ConstructionOutcome:
    """Result of one construction attempt: a package or a typed failure reason."""

    package: CheckPackage | None
    failure_reason: str | None
    input_digest: str
    generator: str
    references: Mapping[str, OracleReference | None] | None = field(default=None, repr=False)
    """Each oracle's reference implementation (memory only; ``reference_check.py``).

    ``None`` means this constructor wrote no references (a caller that
    assembles packages itself): no reference check runs. The product
    constructor always sets it, and an oracle without a usable reference is
    then uncovered (``reference_unavailable``).
    """


def load_constructor_system_prompt() -> str:
    """Return the packaged constructor prompt (``agents/check-constructor.md``)."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt(CONSTRUCTOR_AGENT)


def _criterion_lines(seed: Seed) -> list[str]:
    lines: list[str] = []
    for index, criterion in enumerate(seed.acceptance_criteria, start=1):
        if isinstance(criterion, AcceptanceCriterionSpec):
            lines.append(f"{index}. {criterion.description}")
            if criterion.verify_command:
                lines.append(f"   declared verify_command: {criterion.verify_command}")
            if criterion.output_assertion:
                lines.append(f"   declared output_assertion: {criterion.output_assertion}")
        else:
            lines.append(f"{index}. {str(criterion).strip()}")
    return lines


def build_constructor_prompt(
    seed: Seed, feedback: Sequence[str] = (), *, criterion: int | None = None
) -> str:
    """Render the user message: goal, constraints, numbered criteria, feedback.

    With ``criterion`` (1-based) the message asks for that criterion only;
    the other criteria are shown for context.
    """
    parts = [
        "Repository: the current working directory (read-only copy of the base).",
        "",
        f"Goal: {seed.goal}",
    ]
    if seed.constraints:
        parts += ["", "Constraints:", *(f"- {item}" for item in seed.constraints)]
    parts += ["", "Acceptance criteria:", *_criterion_lines(seed)]
    if feedback:
        parts += [
            "",
            "An earlier package for this Seed was not admitted on the base. Reasons:",
            *(f"- {reason}" for reason in feedback),
        ]
    if criterion is not None:
        parts += [
            "",
            f"Write the oracle (or script check, or uncovered entry) for criterion {criterion} "
            "only; the other criteria are context. Use check ids that start with "
            f"`c{criterion}_`.",
        ]
    parts += ["", "Reply with the JSON object only."]
    return "\n".join(parts)


def build_replacement_prompt(seed: Seed, targets: Mapping[int, str]) -> str:
    """The user message of the one replacement call (1-based criterion to why).

    Only the listed criteria get checks; each is told why the earlier package
    has no admitted check for it (reasons only, never case values), and
    check ids start with ``r<number>_`` so they cannot collide with the
    admitted checks that are kept.
    """
    parts = [
        "Repository: the current working directory (read-only copy of the base).",
        "",
        f"Goal: {seed.goal}",
    ]
    if seed.constraints:
        parts += ["", "Constraints:", *(f"- {item}" for item in seed.constraints)]
    parts += ["", "Acceptance criteria:", *_criterion_lines(seed)]
    parts += [
        "",
        "An earlier check package for this Seed was admitted, but the criteria below have "
        "no admitted check. Write replacement checks for these criteria only; the other "
        "criteria are context and already have checks:",
        *(f"- criterion {number}: {why}" for number, why in sorted(targets.items())),
        "",
        "Use check ids that start with `r<criterion number>_` (for example "
        f"`r{min(targets) if targets else 1}_repro`). A criterion you still cannot check "
        'goes under "uncovered" with its reason.',
        "",
        "Reply with the JSON object only.",
    ]
    return "\n".join(parts)


def constructor_input_digest(
    seed: Seed,
    *,
    system_prompt: str,
    user_prompt: str,
    base_tree_digest: str,
    generator: str,
) -> str:
    """Digest of everything the constructor was given."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "seed_digest": seed_digest(seed),
                "system_prompt_sha256": sha256_bytes(system_prompt.encode("utf-8")),
                "user_prompt_sha256": sha256_bytes(user_prompt.encode("utf-8")),
                "base_tree_digest": base_tree_digest,
                "generator": generator,
            }
        )
    )


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text`` (raw or inside a code fence)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start : text.rfind("}") + 1])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value, _ = decoder.raw_decode(candidate.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ReplyError(ReplyFailure.REPLY_NOT_JSON)


RuntimeFactory = Callable[..., Any]


def _concrete_model(value: object) -> str | None:
    """A model id, or None for an unset or ``default`` placeholder."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if candidate and candidate.lower() != "default" else None


class CheckConstructor:
    """One read-only model call per attempt through the run's runtime backend."""

    def __init__(
        self,
        *,
        runtime_backend: str,
        model: str | None,
        runtime_factory: RuntimeFactory | None = None,
        timeout_seconds: int = DEFAULT_CONSTRUCTOR_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        system_prompt: str | None = None,
        per_criterion: bool = True,
        concurrency: int = 3,
    ) -> None:
        self._backend = runtime_backend
        self._model = model
        self._factory = runtime_factory
        self._timeout = timeout_seconds
        self._max_output_chars = max_output_chars
        self._system_prompt = system_prompt
        self._per_criterion = per_criterion
        self._concurrency = concurrency

    @property
    def generator(self) -> str:
        """The configured label; ``construct`` records the runtime's resolved model."""
        return f"{self._backend}:{self._model or 'default'}"

    def _resolved_generator(self, runtime: Any) -> str:
        """``<backend>:<model>`` for the model requested from the runtime.

        Without an explicit pin this is the model the runtime resolved from
        Ouroboros' own role/profile configuration, when it resolved one.
        """
        model = self._model
        for attribute in ("_resolved_fallback_model", "_model"):
            if model:
                break
            model = _concrete_model(getattr(runtime, attribute, None))
        return f"{self._backend}:{model or 'default'}"

    def _observed_generator(self, messages: Sequence[Any], requested: str) -> str:
        """The model the runtime reported it used, else ``requested``.

        Codex reports its effective model on lifecycle events, surfaced as a
        ``model.observed`` message; that is evidence of the author, whereas a
        requested ``default`` only means "whatever the CLI's own config picks".
        """
        if self._model:
            return requested
        for message in messages:
            data = getattr(message, "data", None) or {}
            if data.get("subtype") != "model.observed":
                continue
            observation = data.get("model_observation") or {}
            model = _concrete_model(observation.get("effective_model"))
            if model:
                return f"{self._backend}:{model}"
        return requested

    def _create_runtime(self, cwd: Path) -> Any:
        factory = self._factory
        if factory is None:
            from ouroboros.orchestrator.runtime_factory import create_agent_runtime

            factory = create_agent_runtime
        return factory(
            backend=self._backend,
            model=self._model,
            permission_mode=CONSTRUCTOR_PERMISSION_MODE,
            cwd=cwd,
        )

    async def construct(
        self,
        seed: Seed,
        base_checkout: Path,
        *,
        feedback: Sequence[str] = (),
    ) -> ConstructionOutcome:
        """Run one attempt and return a package or a typed failure reason.

        By default (``per_criterion``) the attempt is incremental: one call
        per criterion under one shared deadline, each reply kept as produced
        (``boundary/incremental.py``).
        """
        if self._per_criterion:
            return await self._construct_incremental(seed, base_checkout, feedback=feedback)
        return await self._construct_single(
            seed, base_checkout, build_constructor_prompt(seed, feedback)
        )

    async def construct_replacements(
        self, seed: Seed, base_checkout: Path, *, targets: Mapping[int, str]
    ) -> ConstructionOutcome:
        """One call for replacement checks of ``targets`` (1-based criterion to why).

        Always a single read-only call, whatever ``per_criterion`` says, under
        the same timeout and isolation as ``construct``. The reply is kept
        only for the target criteria, restricted once against the whole set
        (``incremental.restrict_reply_to``).
        """
        return await self._construct_single(
            seed, base_checkout, build_replacement_prompt(seed, targets), only=frozenset(targets)
        )

    async def _construct_single(
        self,
        seed: Seed,
        base_checkout: Path,
        user_prompt: str,
        *,
        only: frozenset[int] | None = None,
    ) -> ConstructionOutcome:
        system_prompt = self._system_prompt or load_constructor_system_prompt()
        base = base_checkout.resolve()
        base_before = await asyncio.to_thread(tree_digest, base)
        scratch = Path(tempfile.mkdtemp(prefix="ouroboros-constructor-"))
        try:
            view = scratch / "repo"
            await asyncio.to_thread(copy_checkout, base, view)
            runtime = await asyncio.to_thread(self._create_runtime, view)
            generator = self._resolved_generator(runtime)
            input_digest = constructor_input_digest(
                seed,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_tree_digest=base_before,
                generator=generator,
            )

            def failed(reason: str) -> ConstructionOutcome:
                return ConstructionOutcome(None, reason, input_digest, generator)

            from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

            # The reply holds every held-out case: no session may reach disk.
            refusal = disable_session_persistence(runtime)
            if refusal is not None:
                return failed(refusal)
            blocker = preflight_agent_runtime(runtime)
            if blocker is not None:
                return failed(f"constructor_runtime_unavailable:{blocker}")
            try:
                result = await asyncio.wait_for(
                    runtime.execute_task_to_result(
                        user_prompt,
                        tools=list(CONSTRUCTOR_TOOLS),
                        system_prompt=system_prompt,
                    ),
                    timeout=self._timeout,
                )
            except TimeoutError:
                return failed("constructor_timeout")
            except Exception as exc:  # noqa: BLE001 - a failed call is a typed construction failure
                return failed(f"constructor_call_failed:{type(exc).__name__}")
            finally:
                closer = getattr(runtime, "aclose", None)
                if closer is not None and inspect.iscoroutinefunction(closer):
                    await closer()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        base_after = await asyncio.to_thread(tree_digest, base)
        if base_after != base_before:
            return failed("constructor_mutated_base")
        if result.is_err:
            return failed(f"constructor_call_failed:{type(result.error).__name__}")
        # The input digest keeps the requested label; the package names the
        # model that actually answered when the runtime reported it.
        generator = self._observed_generator(result.value.messages or (), generator)
        reply = result.value.final_message or ""
        if len(reply) > self._max_output_chars:
            return failed("constructor_reply_too_large")
        try:
            parsed = normalize_reply(extract_json_object(reply))
            if only is not None:
                parsed = restrict_reply_to(parsed, only)
            package = package_from_reply(
                parsed,
                seed,
                input_digest=input_digest,
                generator=generator,
            )
        except CheckPackageError as exc:
            return failed(reply_failure_reason(exc))
        if not package.checks:
            if package.uncovered and all(
                item.reason != "constructor_omitted" for item in package.uncovered
            ):
                # Every criterion was declared not executable: nothing for the
                # package to decide, and regenerating would not change that.
                return failed(ALL_CRITERIA_UNCOVERED)
            return failed("constructor_produced_no_checks")
        return ConstructionOutcome(
            package,
            None,
            input_digest,
            generator,
            references=references_from_reply(parsed),
        )

    async def _construct_incremental(
        self,
        seed: Seed,
        base_checkout: Path,
        *,
        feedback: Sequence[str] = (),
    ) -> ConstructionOutcome:
        system_prompt = self._system_prompt or load_constructor_system_prompt()
        base = base_checkout.resolve()
        base_before = await asyncio.to_thread(tree_digest, base)

        def input_digest_for(prompt: str, generator: str) -> str:
            return constructor_input_digest(
                seed,
                system_prompt=system_prompt,
                user_prompt=prompt,
                base_tree_digest=base_before,
                generator=generator,
            )

        def validate(piece: dict[str, Any]) -> None:
            package_from_reply(
                piece,
                seed,
                input_digest="0" * 64,
                generator="validation",
            )

        scratch = Path(tempfile.mkdtemp(prefix="ouroboros-constructor-"))
        try:
            view = scratch / "repo"
            await asyncio.to_thread(copy_checkout, base, view)
            pieces = await construct_pieces(
                self,
                seed,
                view,
                system_prompt=system_prompt,
                prompt_for=lambda number: build_constructor_prompt(
                    seed, feedback, criterion=number
                ),
                input_digest_for=input_digest_for,
                extract=extract_json_object,
                validate=validate,
                concurrency=self._concurrency,
                tools=CONSTRUCTOR_TOOLS,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        digests = [piece.input_digest for piece in pieces if piece.input_digest]
        input_digest = sha256_bytes(canonical_json_bytes({"per_criterion": digests}))
        generator = next(
            (piece.generator for piece in pieces if piece.status == "ok" and piece.generator),
            next((piece.generator for piece in pieces if piece.generator), self.generator),
        )

        def failed(reason: str) -> ConstructionOutcome:
            return ConstructionOutcome(None, reason, input_digest, generator)

        if await asyncio.to_thread(tree_digest, base) != base_before:
            return failed("constructor_mutated_base")
        if not any(piece.status == "ok" for piece in pieces):
            if all(piece.status == "timeout" for piece in pieces):
                return failed("constructor_timeout")
            first = next(piece for piece in pieces if piece.status != "ok")
            return failed(first.reason or "constructor_failed")
        merged, missing = merge_pieces(pieces)
        keys = seed_criterion_keys(seed)
        try:
            package = package_from_reply(
                merged,
                seed,
                input_digest=input_digest,
                generator=generator,
                product_uncovered={keys[number - 1]: reason for number, reason in missing.items()},
            )
        except CheckPackageError as exc:
            return failed(reply_failure_reason(exc))
        if not package.checks:
            if package.uncovered and all(
                item.reason != "constructor_omitted" for item in package.uncovered
            ):
                return failed(ALL_CRITERIA_UNCOVERED)
            return failed("constructor_produced_no_checks")
        return ConstructionOutcome(
            package,
            None,
            input_digest,
            generator,
            references=references_from_reply(merged),
        )
