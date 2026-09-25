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
from datetime import UTC, datetime
import inspect
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from pydantic import ValidationError

from ouroboros.boundary.package import (
    AssertionLink,
    CheckPackage,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
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
DEFAULT_CONSTRUCTOR_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_CHARS = 200_000
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


def build_constructor_prompt(seed: Seed, feedback: Sequence[str] = ()) -> str:
    """Render the user message: goal, constraints, numbered criteria, feedback."""
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
    return raw


def package_from_reply(
    reply: Mapping[str, Any],
    seed: Seed,
    *,
    input_digest: str,
    generator: str,
    generated_at: datetime | None = None,
) -> CheckPackage:
    """Map the constructor's JSON reply onto a ``CheckPackage`` for ``seed``.

    Criteria the reply neither links nor lists are added as uncovered with
    reason ``constructor_omitted``. Raises ``CheckPackageError`` on any schema
    problem, so the caller records a construction failure.
    """
    keys = seed_criterion_keys(seed)
    try:
        files = tuple(
            PackageFile.from_content(_check_file_path(item.get("path")), str(item["content"]))
            for item in reply.get("files") or ()
        )
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
            if not isinstance(argv, list) or not argv:
                raise CheckPackageError(f"{check_id}: argv must be a non-empty list")
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
                    cwd=str(raw.get("cwd") or "."),
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
        for key in keys:
            if key not in linked and key not in uncovered:
                uncovered[key] = "constructor_omitted"
        return CheckPackage(
            seed_digest=seed_digest(seed),
            criterion_keys=keys,
            input_digest=input_digest,
            generated_at=generated_at or datetime.now(UTC),
            generator=generator,
            checks=tuple(checks),
            files=files,
            uncovered=tuple(
                UncoveredObligation(criterion_key=key, reason=reason)
                for key, reason in uncovered.items()
            ),
        )
    except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, CheckPackageError):
            raise
        raise CheckPackageError(f"constructor reply does not match the schema: {exc}") from exc


RuntimeFactory = Callable[..., Any]


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
    ) -> None:
        self._backend = runtime_backend
        self._model = model
        self._factory = runtime_factory
        self._timeout = timeout_seconds
        self._max_output_chars = max_output_chars
        self._system_prompt = system_prompt

    @property
    def generator(self) -> str:
        return f"{self._backend}:{self._model or 'default'}"

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
        """Run one attempt and return a package or a typed failure reason."""
        system_prompt = self._system_prompt or load_constructor_system_prompt()
        user_prompt = build_constructor_prompt(seed, feedback)
        base = base_checkout.resolve()
        base_before = await asyncio.to_thread(tree_digest, base)
        input_digest = constructor_input_digest(
            seed,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            base_tree_digest=base_before,
            generator=self.generator,
        )

        def failed(reason: str, reply_sha: str | None = None) -> ConstructionOutcome:
            return ConstructionOutcome(None, reason, input_digest, self.generator, reply_sha)

        scratch = Path(tempfile.mkdtemp(prefix="ouroboros-constructor-"))
        try:
            view = scratch / "repo"
            await asyncio.to_thread(copy_checkout, base, view)
            runtime = await asyncio.to_thread(self._create_runtime, view)
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
        reply = result.value.final_message or ""
        reply_sha = sha256_bytes(reply.encode("utf-8"))
        if len(reply) > self._max_output_chars:
            return failed("constructor_reply_too_large", reply_sha)
        try:
            package = package_from_reply(
                extract_json_object(reply),
                seed,
                input_digest=input_digest,
                generator=self.generator,
            )
        except CheckPackageError as exc:
            return failed(f"constructor_reply_invalid:{exc}", reply_sha)
        if not package.checks:
            return failed("constructor_produced_no_checks", reply_sha)
        return ConstructionOutcome(package, None, input_digest, self.generator, reply_sha)
