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
from dataclasses import dataclass
from datetime import datetime
import inspect
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from pydantic import ValidationError

from ouroboros.boundary.incremental import construct_pieces, merge_pieces
from ouroboros.boundary.oracle import is_oracle_file
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import (
    AssertionLink,
    CheckPackage,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    canonical_json_bytes,
    seed_criterion_keys,
    seed_digest,
    sha256_bytes,
)
from ouroboros.boundary.tree import copy_checkout, tree_digest
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed

CONSTRUCTOR_AGENT = "check-constructor"
CONSTRUCTOR_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
CONSTRUCTOR_PERMISSION_MODE = "default"
CHECK_DIR = ".ouroboros_checks"
CHECK_INTERPRETERS = frozenset({"python3", "python"})
DEFAULT_CONSTRUCTOR_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_CHARS = 200_000
ALL_CRITERIA_UNCOVERED = "constructor_all_criteria_uncovered"
_CHECK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MIN_SIGNATURE_CHARS = 12


@dataclass(frozen=True, slots=True)
class ConstructionOutcome:
    """Result of one construction attempt: a package or a typed failure reason."""

    package: CheckPackage | None
    failure_reason: str | None
    input_digest: str
    generator: str
    reply_sha256: str | None = None


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
    raise CheckPackageError("constructor reply contains no JSON object")


def _criterion_key(keys: Sequence[str], raw: object, where: str) -> str:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= len(keys):
        raise CheckPackageError(f"{where}: criterion must be a number from 1 to {len(keys)}")
    return keys[raw - 1]


def _check_file_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw.startswith(f"{CHECK_DIR}/"):
        raise CheckPackageError(f"check files must live under {CHECK_DIR}/: {raw!r}")
    if is_oracle_file(raw):
        raise CheckPackageError(f"the oracle directory is reserved for product files: {raw!r}")
    return raw


def package_from_reply(
    reply: Mapping[str, Any],
    seed: Seed,
    *,
    input_digest: str,
    generator: str,
    generated_at: datetime | None = None,
    base_checkout: Path | None = None,
) -> CheckPackage:
    """Map the constructor's JSON reply onto a ``CheckPackage`` for ``seed``.

    ``oracles`` become frozen oracle checks run by the product harness
    (``boundary/oracle.py``); ``checks`` are model-written scripts. Criteria
    the reply neither links nor lists are added as uncovered with reason
    ``constructor_omitted``. ``base_checkout`` decides whether each oracle's
    default binding resolves (tier ``A``). Raises ``CheckPackageError`` on any
    schema problem, so the caller records a construction failure.
    """
    keys = seed_criterion_keys(seed)
    try:
        oracles = []
        for raw in reply.get("oracles") or ():
            number = raw.get("criterion")
            _criterion_key(keys, number, "oracle")
            check_id = str(raw.get("check_id") or f"oracle_{number}")
            if not _CHECK_ID.fullmatch(check_id):
                raise CheckPackageError(f"invalid check_id: {check_id!r}")
            oracles.append(
                (
                    build_oracle_spec(
                        seed,
                        criterion_index=number - 1,
                        check_id=check_id,
                        call_kind=str(raw.get("call_kind") or "function"),
                        params=tuple(str(name) for name in raw.get("params") or ()),
                        default_binding=dict(raw.get("default_binding") or {}),
                        cases=list(raw.get("cases") or ()),
                        base_checkout=base_checkout,
                    ),
                    CheckRole(str(raw.get("role"))),
                )
            )
        files = tuple(
            PackageFile.from_content(_check_file_path(item.get("path")), str(item["content"]))
            for item in reply.get("files") or ()
        )
        file_paths = {item.path for item in files}
        checks: list[CheckSpec] = []
        signatures: set[str] = set()
        for raw in reply.get("checks") or ():
            check_id = str(raw.get("check_id", ""))
            if not _CHECK_ID.fullmatch(check_id):
                raise CheckPackageError(f"invalid check_id: {check_id!r}")
            role = CheckRole(str(raw.get("role")))
            signature = raw.get("failure_signature") or None
            if role is CheckRole.REPRODUCTION:
                if not isinstance(signature, str) or len(signature) < _MIN_SIGNATURE_CHARS:
                    raise CheckPackageError(f"{check_id}: failure_signature is too short")
                if signature in signatures:
                    raise CheckPackageError(f"{check_id}: failure_signature is not unique")
                signatures.add(signature)
            else:
                signature = None
            argv = raw.get("argv")
            if (
                not isinstance(argv, list)
                or len(argv) != 2
                or argv[0] not in CHECK_INTERPRETERS
                or argv[1] not in file_paths
                or str(raw.get("cwd") or ".") != "."
            ):
                # The only accepted shape is the one the prompt prescribes: a
                # packaged script run by the Python interpreter from the root.
                raise CheckPackageError(
                    f"{check_id}: argv must be [python3, <packaged script>] with cwd '.'"
                )
            assertions = tuple(
                AssertionLink(
                    assertion_id=str(link.get("assertion_id") or f"{check_id}.a{number}"),
                    criterion_key=_criterion_key(keys, link.get("criterion"), check_id),
                    file=None,
                    locator=(str(link["locator"]) if link.get("locator") else None),
                )
                for number, link in enumerate(raw.get("assertions") or (), start=1)
            )
            checks.append(
                CheckSpec(
                    check_id=check_id,
                    role=role,
                    argv=tuple(str(arg) for arg in argv),
                    cwd=".",
                    assertions=assertions,
                    failure_signature=signature,
                )
            )
        linked = {link.criterion_key for check in checks for link in check.assertions}
        uncovered: dict[str, str] = {}
        for raw in reply.get("uncovered") or ():
            key = _criterion_key(keys, raw.get("criterion"), "uncovered")
            if key not in linked:
                uncovered[key] = str(raw.get("reason") or "unspecified").strip() or "unspecified"
        linked |= {spec.criterion_key for spec, _role in oracles}
        uncovered = {key: reason for key, reason in uncovered.items() if key not in linked}
        return assemble_package(
            seed,
            input_digest=input_digest,
            generator=generator,
            oracles=oracles,
            script_checks=checks,
            script_files=files,
            uncovered=uncovered,
            generated_at=generated_at,
        )
    except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, CheckPackageError):
            raise
        raise CheckPackageError(f"constructor reply does not match the schema: {exc}") from exc


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
        self._partial_dir: Path | None = None

    def persist_partials_to(self, directory: Path | None) -> None:
        """Write each criterion's reply under ``directory`` as it is produced."""
        self._partial_dir = directory

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
        system_prompt = self._system_prompt or load_constructor_system_prompt()
        user_prompt = build_constructor_prompt(seed, feedback)
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

            def failed(reason: str, reply_sha: str | None = None) -> ConstructionOutcome:
                return ConstructionOutcome(None, reason, input_digest, generator, reply_sha)

            from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

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
        reply_sha = sha256_bytes(reply.encode("utf-8"))
        if len(reply) > self._max_output_chars:
            return failed("constructor_reply_too_large", reply_sha)
        try:
            package = package_from_reply(
                extract_json_object(reply),
                seed,
                input_digest=input_digest,
                generator=generator,
                base_checkout=base,
            )
        except CheckPackageError as exc:
            return failed(f"constructor_reply_invalid:{exc}", reply_sha)
        if not package.checks:
            if package.uncovered and all(
                item.reason != "constructor_omitted" for item in package.uncovered
            ):
                # Every criterion was declared not executable: nothing for the
                # package to decide, and regenerating would not change that.
                return failed(ALL_CRITERIA_UNCOVERED, reply_sha)
            return failed("constructor_produced_no_checks", reply_sha)
        return ConstructionOutcome(package, None, input_digest, generator, reply_sha)

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
                base_checkout=base,
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
                partial_dir=self._partial_dir,
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
        reply_sha = sha256_bytes(canonical_json_bytes([piece.reply_sha256 for piece in pieces]))

        def failed(reason: str) -> ConstructionOutcome:
            return ConstructionOutcome(None, reason, input_digest, generator, reply_sha)

        if await asyncio.to_thread(tree_digest, base) != base_before:
            return failed("constructor_mutated_base")
        if not any(piece.status == "ok" for piece in pieces):
            if all(piece.status == "timeout" for piece in pieces):
                return failed("constructor_timeout")
            first = next(piece for piece in pieces if piece.status != "ok")
            return failed(first.reason or "constructor_failed")
        merged, _missing = merge_pieces(pieces)
        try:
            package = package_from_reply(
                merged,
                seed,
                input_digest=input_digest,
                generator=generator,
                base_checkout=base,
            )
        except CheckPackageError as exc:
            return failed(f"constructor_reply_invalid:{exc}")
        if not package.checks:
            if package.uncovered and all(
                item.reason != "constructor_omitted" for item in package.uncovered
            ):
                return failed(ALL_CRITERIA_UNCOVERED)
            return failed("constructor_produced_no_checks")
        return ConstructionOutcome(package, None, input_digest, generator, reply_sha)
