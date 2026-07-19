"""Provider-neutral execution capsule for one Ouroboros acceptance criterion.

The capsule is the runtime-owned boundary above Claude, Codex, and every other
``AgentRuntime`` driver.  It deliberately carries compact facts and references,
not a provider transcript: durable workflow state lives in the workspace, Seed,
event ledger, and verify-gate records.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import os

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.execution_runtime_scope import ACRuntimeIdentity
from ouroboros.orchestrator.level_context import LevelContext

AC_EXECUTION_CAPSULE_VERSION = 2
DEFAULT_AC_CONTEXT_BUDGET_CHARS = 12_000
MAX_AC_CONTEXT_REFERENCES = 256
_MAX_REFERENCE_LOCATOR_CHARS = 2_048
_MAX_REFERENCE_HINT_CHARS = 240
MAX_AC_SUCCESS_CONTRACT_ARTIFACTS = MAX_AC_CONTEXT_REFERENCES - 3
MAX_AC_SUCCESS_CONTRACT_CHARS = 64_000
_MAX_SUCCESS_CONTRACT_ARTIFACT_CHARS = _MAX_REFERENCE_LOCATOR_CHARS - len("workspace:")


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _validate_sha256_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != len("sha256:") + 64:
        raise ValueError(f"{field} is malformed")
    if not value.startswith("sha256:"):
        raise ValueError(f"{field} is malformed")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ValueError(f"{field} is malformed") from exc
    return value


def build_ac_dispatch_authority_scope(
    *,
    base_scope: str,
    dispatch_contract: Mapping[str, object],
    execution_policy: Mapping[str, object],
) -> str:
    """Fingerprint every authority-bearing input that can change AC dispatch."""
    if not isinstance(base_scope, str) or not base_scope:
        raise ValueError("AC dispatch authority base scope is missing")
    payload = {
        "version": 1,
        "base_scope": base_scope,
        "dispatch_contract": dict(dispatch_contract),
        "execution_policy": dict(execution_policy),
    }
    return _sha256_text(_canonical_json(payload))


class ACContextReferenceKind(StrEnum):
    """External context source named by an execution capsule."""

    WORKSPACE = "workspace"
    SEED = "seed"
    DEPENDENCY = "dependency"
    ARTIFACT = "artifact"
    GATE = "gate"


@dataclass(frozen=True, slots=True)
class ACContextReference:
    """A compact pointer to context that remains outside the model prompt."""

    kind: ACContextReferenceKind
    locator: str
    digest: str | None = None
    hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ACContextReferenceKind):
            raise ValueError("context reference kind is invalid")
        if not self.locator or len(self.locator) > _MAX_REFERENCE_LOCATOR_CHARS:
            raise ValueError("context reference locator is missing or oversized")
        if any(character in self.locator for character in ("\x00", "\r", "\n")):
            raise ValueError("context reference locator contains control characters")
        if len(self.hint) > _MAX_REFERENCE_HINT_CHARS:
            raise ValueError("context reference hint is oversized")
        if any(character in self.hint for character in ("\x00", "\r", "\n")):
            raise ValueError("context reference hint contains control characters")
        if self.digest is not None:
            _validate_sha256_digest(self.digest, field="context reference digest")

    def to_contract_data(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "locator": self.locator,
            "digest": self.digest,
            "hint": self.hint,
        }

    @classmethod
    def from_contract_data(cls, raw: object) -> ACContextReference:
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "locator", "digest", "hint"}:
            raise ValueError("context reference contract has an invalid shape")
        try:
            kind = ACContextReferenceKind(raw.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ValueError("context reference kind is invalid") from exc
        locator = raw.get("locator")
        digest = raw.get("digest")
        hint = raw.get("hint")
        if not isinstance(locator, str):
            raise ValueError("context reference locator is invalid")
        if digest is not None and not isinstance(digest, str):
            raise ValueError("context reference digest is invalid")
        if not isinstance(hint, str):
            raise ValueError("context reference hint is invalid")
        return cls(kind=kind, locator=locator, digest=digest, hint=hint)


@dataclass(frozen=True, slots=True)
class ACContextReferenceManifest:
    """Non-sensitive durable identity for one external context reference."""

    kind: ACContextReferenceKind
    locator_digest: str
    content_digest: str | None
    hint_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ACContextReferenceKind):
            raise ValueError("context reference manifest kind is invalid")
        _validate_sha256_digest(
            self.locator_digest,
            field="context reference manifest locator digest",
        )
        if self.content_digest is not None:
            _validate_sha256_digest(
                self.content_digest,
                field="context reference manifest content digest",
            )
        _validate_sha256_digest(
            self.hint_digest,
            field="context reference manifest hint digest",
        )

    def to_contract_data(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "locator_digest": self.locator_digest,
            "content_digest": self.content_digest,
            "hint_digest": self.hint_digest,
        }

    @classmethod
    def from_reference(cls, reference: ACContextReference) -> ACContextReferenceManifest:
        return cls(
            kind=reference.kind,
            locator_digest=_sha256_text(reference.locator),
            content_digest=reference.digest,
            hint_digest=_sha256_text(reference.hint),
        )

    @classmethod
    def from_contract_data(cls, raw: object) -> ACContextReferenceManifest:
        expected = {"kind", "locator_digest", "content_digest", "hint_digest"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("context reference manifest has an invalid shape")
        try:
            kind = ACContextReferenceKind(raw.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ValueError("context reference manifest kind is invalid") from exc
        locator_digest = raw.get("locator_digest")
        content_digest = raw.get("content_digest")
        hint_digest = raw.get("hint_digest")
        if content_digest is not None and not isinstance(content_digest, str):
            raise ValueError("context reference manifest content digest is invalid")
        return cls(
            kind=kind,
            locator_digest=locator_digest,  # type: ignore[arg-type]
            content_digest=content_digest,
            hint_digest=hint_digest,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ACSuccessContract:
    """Seed-authored acceptance gate projected into a capsule."""

    verify_command: str | None = None
    expected_artifacts: tuple[str, ...] = ()
    output_assertion: str | None = None

    def __post_init__(self) -> None:
        if self.verify_command is not None and not isinstance(self.verify_command, str):
            raise ValueError("success contract verify command is invalid")
        if self.output_assertion is not None and not isinstance(self.output_assertion, str):
            raise ValueError("success contract output assertion is invalid")
        try:
            artifact_count = len(self.expected_artifacts)
        except TypeError as exc:
            raise ValueError("success contract artifacts are invalid") from exc
        if artifact_count > MAX_AC_SUCCESS_CONTRACT_ARTIFACTS:
            raise ValueError(
                f"success contract artifact limit exceeded ({MAX_AC_SUCCESS_CONTRACT_ARTIFACTS})"
            )

        contract_chars = len(self.verify_command or "") + len(self.output_assertion or "")
        for path in self.expected_artifacts:
            if (
                not isinstance(path, str)
                or not path
                or len(path) > _MAX_SUCCESS_CONTRACT_ARTIFACT_CHARS
            ):
                raise ValueError("success contract artifacts are invalid")
            contract_chars += len(path)
        if contract_chars > MAX_AC_SUCCESS_CONTRACT_CHARS:
            raise ValueError(
                f"success contract character budget exceeded ({MAX_AC_SUCCESS_CONTRACT_CHARS})"
            )

    def to_contract_data(self) -> dict[str, object]:
        return {
            "verify_command": self.verify_command,
            "expected_artifacts": list(self.expected_artifacts),
            "output_assertion": self.output_assertion,
        }

    @property
    def has_success_contract(self) -> bool:
        return bool(self.verify_command or self.expected_artifacts or self.output_assertion)

    @classmethod
    def from_ac_spec(cls, spec: AcceptanceCriterionSpec | None) -> ACSuccessContract:
        if spec is None:
            return cls()
        return cls(
            verify_command=spec.verify_command,
            expected_artifacts=tuple(spec.expected_artifacts),
            output_assertion=spec.output_assertion,
        )

    @classmethod
    def from_contract_data(cls, raw: object) -> ACSuccessContract:
        expected = {"verify_command", "expected_artifacts", "output_assertion"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("success contract has an invalid shape")
        verify_command = raw.get("verify_command")
        expected_artifacts = raw.get("expected_artifacts")
        output_assertion = raw.get("output_assertion")
        if verify_command is not None and not isinstance(verify_command, str):
            raise ValueError("success contract verify command is invalid")
        if not isinstance(expected_artifacts, list) or any(
            not isinstance(path, str) or not path for path in expected_artifacts
        ):
            raise ValueError("success contract artifacts are invalid")
        if output_assertion is not None and not isinstance(output_assertion, str):
            raise ValueError("success contract output assertion is invalid")
        return cls(
            verify_command=verify_command,
            expected_artifacts=tuple(expected_artifacts),
            output_assertion=output_assertion,
        )


@dataclass(frozen=True, slots=True)
class ACExecutionCapsuleManifest:
    """Strict durable capsule identity with all free-form values hashed.

    Provider prompts still receive the in-memory :class:`ACExecutionCapsule`,
    but the event ledger stores only this manifest. Recovery can therefore
    validate exact authority without copying credentials, PII, prompts, or
    absolute workspace paths into another durable payload.
    """

    execution_id: str
    semantic_ac_key: str
    ac_id: str
    session_scope_id: str
    session_attempt_id: str
    node_id: str | None
    retry_attempt: int
    segment_index: int
    workspace_digest: str
    authority_scope_digest: str
    seed_goal_digest: str
    ac_content_digest: str
    success_contract_digest: str
    context_references: tuple[ACContextReferenceManifest, ...]
    omitted_context_count: int
    omitted_context_digest: str | None
    context_budget_chars: int
    fresh_session_required: bool = True
    version: int = AC_EXECUTION_CAPSULE_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("execution_id", self.execution_id),
            ("semantic_ac_key", self.semantic_ac_key),
            ("ac_id", self.ac_id),
            ("session_scope_id", self.session_scope_id),
            ("session_attempt_id", self.session_attempt_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"capsule manifest {name} is missing")
        if self.node_id is not None and (not isinstance(self.node_id, str) or not self.node_id):
            raise ValueError("capsule manifest node id is invalid")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != AC_EXECUTION_CAPSULE_VERSION
        ):
            raise ValueError("capsule manifest version is unsupported")
        for name, value in (
            ("retry attempt", self.retry_attempt),
            ("segment index", self.segment_index),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"capsule manifest {name} is invalid")
        if (
            not isinstance(self.context_budget_chars, int)
            or isinstance(self.context_budget_chars, bool)
            or self.context_budget_chars <= 0
        ):
            raise ValueError("capsule manifest context budget is invalid")
        if (
            not isinstance(self.omitted_context_count, int)
            or isinstance(self.omitted_context_count, bool)
            or self.omitted_context_count < 0
        ):
            raise ValueError("capsule manifest omitted context count is invalid")
        if self.omitted_context_count:
            _validate_sha256_digest(
                self.omitted_context_digest,
                field="capsule manifest omitted context digest",
            )
        elif self.omitted_context_digest is not None:
            raise ValueError("capsule manifest omitted context digest is unexpected")
        if self.fresh_session_required is not True:
            raise ValueError("capsule manifest must require a fresh session")
        if not self.context_references:
            raise ValueError("capsule manifest must contain context references")
        for name, value in (
            ("workspace digest", self.workspace_digest),
            ("authority scope digest", self.authority_scope_digest),
            ("seed goal digest", self.seed_goal_digest),
            ("AC content digest", self.ac_content_digest),
            ("success contract digest", self.success_contract_digest),
        ):
            _validate_sha256_digest(value, field=f"capsule manifest {name}")

    @property
    def fingerprint(self) -> str:
        return _sha256_text(_canonical_json(self.to_contract_data()))

    def to_contract_data(self) -> dict[str, object]:
        return {
            "version": self.version,
            "execution_id": self.execution_id,
            "semantic_ac_key": self.semantic_ac_key,
            "ac_id": self.ac_id,
            "session_scope_id": self.session_scope_id,
            "session_attempt_id": self.session_attempt_id,
            "node_id": self.node_id,
            "retry_attempt": self.retry_attempt,
            "segment_index": self.segment_index,
            "workspace_digest": self.workspace_digest,
            "authority_scope_digest": self.authority_scope_digest,
            "seed_goal_digest": self.seed_goal_digest,
            "ac_content_digest": self.ac_content_digest,
            "success_contract_digest": self.success_contract_digest,
            "context_references": [
                reference.to_contract_data() for reference in self.context_references
            ],
            "omitted_context_count": self.omitted_context_count,
            "omitted_context_digest": self.omitted_context_digest,
            "context_budget_chars": self.context_budget_chars,
            "fresh_session_required": self.fresh_session_required,
        }

    @classmethod
    def from_contract_data(cls, raw: object) -> ACExecutionCapsuleManifest:
        expected = {
            "version",
            "execution_id",
            "semantic_ac_key",
            "ac_id",
            "session_scope_id",
            "session_attempt_id",
            "node_id",
            "retry_attempt",
            "segment_index",
            "workspace_digest",
            "authority_scope_digest",
            "seed_goal_digest",
            "ac_content_digest",
            "success_contract_digest",
            "context_references",
            "omitted_context_count",
            "omitted_context_digest",
            "context_budget_chars",
            "fresh_session_required",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("AC execution capsule manifest has an invalid shape")
        references = raw.get("context_references")
        if not isinstance(references, list):
            raise ValueError("capsule manifest context references are invalid")
        node_id = raw.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise ValueError("capsule manifest node id is invalid")
        scalar_names = (
            "execution_id",
            "semantic_ac_key",
            "ac_id",
            "session_scope_id",
            "session_attempt_id",
            "workspace_digest",
            "authority_scope_digest",
            "seed_goal_digest",
            "ac_content_digest",
            "success_contract_digest",
        )
        scalars = {name: raw.get(name) for name in scalar_names}
        if any(not isinstance(value, str) for value in scalars.values()):
            raise ValueError("capsule manifest string field is invalid")
        return cls(
            version=raw.get("version"),  # type: ignore[arg-type]
            execution_id=scalars["execution_id"],  # type: ignore[arg-type]
            semantic_ac_key=scalars["semantic_ac_key"],  # type: ignore[arg-type]
            ac_id=scalars["ac_id"],  # type: ignore[arg-type]
            session_scope_id=scalars["session_scope_id"],  # type: ignore[arg-type]
            session_attempt_id=scalars["session_attempt_id"],  # type: ignore[arg-type]
            node_id=node_id,
            retry_attempt=raw.get("retry_attempt"),  # type: ignore[arg-type]
            segment_index=raw.get("segment_index"),  # type: ignore[arg-type]
            workspace_digest=scalars["workspace_digest"],  # type: ignore[arg-type]
            authority_scope_digest=scalars["authority_scope_digest"],  # type: ignore[arg-type]
            seed_goal_digest=scalars["seed_goal_digest"],  # type: ignore[arg-type]
            ac_content_digest=scalars["ac_content_digest"],  # type: ignore[arg-type]
            success_contract_digest=scalars["success_contract_digest"],  # type: ignore[arg-type]
            context_references=tuple(
                ACContextReferenceManifest.from_contract_data(reference) for reference in references
            ),
            omitted_context_count=raw.get("omitted_context_count"),  # type: ignore[arg-type]
            omitted_context_digest=raw.get("omitted_context_digest"),  # type: ignore[arg-type]
            context_budget_chars=raw.get("context_budget_chars"),  # type: ignore[arg-type]
            fresh_session_required=raw.get("fresh_session_required"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ACExecutionCapsule:
    """Versioned Ouroboros-owned contract for one physical AC session."""

    execution_id: str
    semantic_ac_key: str
    ac_id: str
    session_scope_id: str
    session_attempt_id: str
    node_id: str | None
    retry_attempt: int
    segment_index: int
    workspace: str
    authority_scope: str
    seed_goal: str
    ac_content: str
    success_contract: ACSuccessContract
    context_references: tuple[ACContextReference, ...]
    omitted_context_count: int
    omitted_context_digest: str | None
    context_budget_chars: int
    fresh_session_required: bool = True
    version: int = AC_EXECUTION_CAPSULE_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("execution_id", self.execution_id),
            ("semantic_ac_key", self.semantic_ac_key),
            ("ac_id", self.ac_id),
            ("session_scope_id", self.session_scope_id),
            ("session_attempt_id", self.session_attempt_id),
            ("authority_scope", self.authority_scope),
            ("seed_goal", self.seed_goal),
            ("ac_content", self.ac_content),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"capsule {name} is missing")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != AC_EXECUTION_CAPSULE_VERSION
        ):
            raise ValueError("capsule version is unsupported")
        if (
            not isinstance(self.retry_attempt, int)
            or isinstance(self.retry_attempt, bool)
            or self.retry_attempt < 0
        ):
            raise ValueError("capsule retry attempt is invalid")
        if (
            not isinstance(self.segment_index, int)
            or isinstance(self.segment_index, bool)
            or self.segment_index < 0
        ):
            raise ValueError("capsule segment index is invalid")
        if (
            not isinstance(self.context_budget_chars, int)
            or isinstance(self.context_budget_chars, bool)
            or self.context_budget_chars <= 0
        ):
            raise ValueError("capsule context budget is invalid")
        if (
            not isinstance(self.omitted_context_count, int)
            or isinstance(self.omitted_context_count, bool)
            or self.omitted_context_count < 0
        ):
            raise ValueError("capsule omitted context count is invalid")
        if self.omitted_context_count:
            _validate_sha256_digest(
                self.omitted_context_digest,
                field="capsule omitted context digest",
            )
        elif self.omitted_context_digest is not None:
            raise ValueError("capsule omitted context digest is unexpected")
        if self.fresh_session_required is not True:
            raise ValueError("an AC execution capsule must require a fresh session")
        if not os.path.isabs(self.workspace) or os.path.realpath(self.workspace) != self.workspace:
            raise ValueError("capsule workspace must be a canonical absolute path")
        if self.node_id is not None and (not isinstance(self.node_id, str) or not self.node_id):
            raise ValueError("capsule node id is invalid")
        if not self.context_references:
            raise ValueError("capsule must contain at least one context reference")
        if len(self.to_prompt_reference_block()) > self.context_budget_chars:
            raise ValueError("capsule context references exceed the declared budget")

    @property
    def manifest(self) -> ACExecutionCapsuleManifest:
        return ACExecutionCapsuleManifest(
            version=self.version,
            execution_id=self.execution_id,
            semantic_ac_key=self.semantic_ac_key,
            ac_id=self.ac_id,
            session_scope_id=self.session_scope_id,
            session_attempt_id=self.session_attempt_id,
            node_id=self.node_id,
            retry_attempt=self.retry_attempt,
            segment_index=self.segment_index,
            workspace_digest=_sha256_text(self.workspace),
            authority_scope_digest=_sha256_text(self.authority_scope),
            seed_goal_digest=_sha256_text(self.seed_goal),
            ac_content_digest=_sha256_text(self.ac_content),
            success_contract_digest=_sha256_text(
                _canonical_json(self.success_contract.to_contract_data())
            ),
            context_references=tuple(
                ACContextReferenceManifest.from_reference(reference)
                for reference in self.context_references
            ),
            omitted_context_count=self.omitted_context_count,
            omitted_context_digest=self.omitted_context_digest,
            context_budget_chars=self.context_budget_chars,
            fresh_session_required=self.fresh_session_required,
        )

    @property
    def fingerprint(self) -> str:
        return self.manifest.fingerprint

    def to_prompt_reference_block(self) -> str:
        """Render the bounded external-memory frontier given to the provider driver."""
        return _render_prompt_reference_block(
            self.context_references,
            fingerprint=self.fingerprint,
            omitted_context_count=self.omitted_context_count,
        )


def _render_prompt_reference_block(
    references: Sequence[ACContextReference],
    *,
    fingerprint: str,
    omitted_context_count: int = 0,
) -> str:
    """Render a reference-only provider block with a fixed-size fingerprint."""
    lines = [
        "## Ouroboros AC Runtime",
        "This AC runs in a fresh provider context. The shared workspace and "
        "Ouroboros event/gate records are authoritative; inspect referenced "
        "sources as needed instead of assuming prior chat history.",
        f"Capsule: {fingerprint}",
        "Context references:",
    ]
    for reference in references:
        hint = f" — {reference.hint}" if reference.hint else ""
        lines.append(f"- {reference.kind.value}: {reference.locator}{hint}")
    if omitted_context_count:
        lines.append(
            f"- bounded-retrieval: {omitted_context_count} optional references omitted; "
            "the workspace and Seed remain authoritative"
        )
    return "\n".join(lines)


def _bounded_context_references(
    *,
    required: Sequence[ACContextReference],
    optional: Iterable[ACContextReference],
    context_budget_chars: int,
) -> tuple[tuple[ACContextReference, ...], int, str | None]:
    """Select a bounded reference frontier and attest what was omitted.

    Omitted references are never represented as a page unless a real resolver
    exists. The capsule instead records a rolling digest and count, while the
    provider sees an explicit bounded-retrieval notice. A hard reference cap
    bounds compiler work independently of adversarial Seed/dependency size.
    """
    placeholder_fingerprint = "sha256:" + "0" * 64
    selected = list(required)
    if (
        len(
            _render_prompt_reference_block(
                selected,
                fingerprint=placeholder_fingerprint,
            )
        )
        > context_budget_chars
    ):
        raise ValueError("capsule context budget cannot fit required references")

    omitted_count = 0
    omitted_hasher = hashlib.sha256()
    reference_count = len(selected)
    for reference in optional:
        reference_count += 1
        if reference_count > MAX_AC_CONTEXT_REFERENCES:
            raise ValueError(
                f"capsule context reference limit exceeded ({MAX_AC_CONTEXT_REFERENCES})"
            )
        candidate = (*selected, reference)
        fits_with_omission_notice = (
            len(
                _render_prompt_reference_block(
                    candidate,
                    fingerprint=placeholder_fingerprint,
                    omitted_context_count=MAX_AC_CONTEXT_REFERENCES,
                )
            )
            <= context_budget_chars
        )
        if omitted_count == 0 and fits_with_omission_notice:
            selected.append(reference)
            continue
        omitted_count += 1
        omitted_hasher.update(_canonical_json(reference.to_contract_data()).encode("utf-8"))
        omitted_hasher.update(b"\n")

    omitted_digest = "sha256:" + omitted_hasher.hexdigest() if omitted_count else None
    return tuple(selected), omitted_count, omitted_digest


def build_ac_dependency_references(
    execution_id: str,
    level_contexts: Sequence[LevelContext],
) -> Iterator[ACContextReference]:
    """Yield the newest accepted dependencies first for bounded retrieval."""
    for context in reversed(level_contexts):
        for summary in reversed(context.completed_acs):
            if not summary.success:
                continue
            payload = {
                "level_number": context.level_number,
                "ac_index": summary.ac_index,
                "ac_content": summary.ac_content,
                "tools_used": list(summary.tools_used),
                "files_modified": list(summary.files_modified),
                "key_output": summary.key_output,
                "public_api": summary.public_api,
            }
            yield ACContextReference(
                kind=ACContextReferenceKind.DEPENDENCY,
                locator=f"execution:{execution_id}:ac:{summary.ac_index + 1}",
                digest=_sha256_text(_canonical_json(payload)),
                hint=f"accepted dependency from level {context.level_number + 1}",
            )


def compile_ac_execution_capsule(
    *,
    runtime_identity: ACRuntimeIdentity,
    execution_id: str,
    semantic_ac_key: str,
    workspace: str,
    authority_scope: str,
    seed_goal: str,
    ac_content: str,
    ac_spec: AcceptanceCriterionSpec | None,
    success_contract_override: ACSuccessContract | None = None,
    level_contexts: Sequence[LevelContext] = (),
    dependency_references: Iterable[ACContextReference] | None = None,
    segment_index: int = 0,
    context_budget_chars: int = DEFAULT_AC_CONTEXT_BUDGET_CHARS,
) -> ACExecutionCapsule:
    """Compile one deterministic capsule from existing orchestrator authority."""
    canonical_workspace = os.path.realpath(workspace)
    success_contract = success_contract_override or ACSuccessContract.from_ac_spec(ac_spec)
    required_references: list[ACContextReference] = [
        ACContextReference(
            kind=ACContextReferenceKind.WORKSPACE,
            locator=canonical_workspace,
            hint="authoritative mutable implementation state",
        ),
        ACContextReference(
            kind=ACContextReferenceKind.SEED,
            locator=f"seed-goal:{semantic_ac_key}",
            digest=_sha256_text(seed_goal),
            hint="goal and semantic AC authority",
        ),
    ]

    def _optional_references() -> Iterator[ACContextReference]:
        for path in success_contract.expected_artifacts:
            yield ACContextReference(
                kind=ACContextReferenceKind.ARTIFACT,
                locator=f"workspace:{path}",
                hint="seed-authored expected artifact",
            )
        yield from (
            dependency_references
            if dependency_references is not None
            else build_ac_dependency_references(execution_id, level_contexts)
        )

    if success_contract.has_success_contract:
        required_references.append(
            ACContextReference(
                kind=ACContextReferenceKind.GATE,
                locator=f"gate:{runtime_identity.ac_id}",
                digest=_sha256_text(_canonical_json(success_contract.to_contract_data())),
                hint="authoritative acceptance contract",
            )
        )
    references, omitted_context_count, omitted_context_digest = _bounded_context_references(
        required=required_references,
        optional=_optional_references(),
        context_budget_chars=context_budget_chars,
    )
    return ACExecutionCapsule(
        execution_id=execution_id,
        semantic_ac_key=semantic_ac_key,
        ac_id=runtime_identity.ac_id,
        session_scope_id=runtime_identity.session_scope_id,
        session_attempt_id=runtime_identity.session_attempt_id,
        node_id=runtime_identity.node_id,
        retry_attempt=runtime_identity.retry_attempt,
        segment_index=segment_index,
        workspace=canonical_workspace,
        authority_scope=authority_scope,
        seed_goal=seed_goal,
        ac_content=ac_content,
        success_contract=success_contract,
        context_references=references,
        omitted_context_count=omitted_context_count,
        omitted_context_digest=omitted_context_digest,
        context_budget_chars=context_budget_chars,
    )


def bind_capsule_to_runtime_handle(
    capsule: ACExecutionCapsule,
    runtime_handle: RuntimeHandle | None,
    *,
    restored_same_attempt: bool,
    expected_backend: str | None = None,
    expected_approval_mode: str | None = None,
) -> RuntimeHandle | None:
    """Bind a provider handle to exactly one AC capsule.

    A newly compiled AC may receive a handle-shaped configuration object (cwd,
    permissions, capability metadata), but it must not inherit any provider
    continuity identifier.  Crash recovery may reconnect only to the same AC
    attempt, and any already-bound handle must agree with the capsule fingerprint.
    """
    if runtime_handle is None:
        return None
    continuity_values = (
        runtime_handle.native_session_id,
        runtime_handle.conversation_id,
        runtime_handle.previous_response_id,
        runtime_handle.transcript_path,
        runtime_handle.server_session_id,
    )
    if not restored_same_attempt and any(continuity_values):
        raise ValueError("a fresh AC capsule cannot inherit provider session continuity")
    if restored_same_attempt:
        if not runtime_handle.cwd:
            raise ValueError("a resumed runtime handle must declare its workspace")
        restored_workspace = os.path.realpath(os.path.expanduser(runtime_handle.cwd))
        if restored_workspace != capsule.workspace:
            raise ValueError("runtime handle workspace disagrees with the AC capsule")
        if expected_backend is not None and runtime_handle.backend != expected_backend:
            raise ValueError("runtime handle backend disagrees with the AC capsule authority")
        if (
            expected_approval_mode is not None
            and runtime_handle.approval_mode != expected_approval_mode
        ):
            raise ValueError("runtime handle approval mode disagrees with the AC capsule authority")
    existing_fingerprint = runtime_handle.metadata.get("ac_capsule_fingerprint")
    if existing_fingerprint is not None and existing_fingerprint != capsule.fingerprint:
        raise ValueError("runtime handle is bound to a different AC capsule")
    metadata = dict(runtime_handle.metadata)
    metadata.update(
        {
            "ac_capsule_version": capsule.version,
            "ac_capsule_fingerprint": capsule.fingerprint,
            "ac_session_origin": ("restored_same_attempt" if restored_same_attempt else "fresh"),
        }
    )
    return replace(runtime_handle, metadata=metadata)


__all__ = [
    "AC_EXECUTION_CAPSULE_VERSION",
    "DEFAULT_AC_CONTEXT_BUDGET_CHARS",
    "MAX_AC_CONTEXT_REFERENCES",
    "MAX_AC_SUCCESS_CONTRACT_ARTIFACTS",
    "MAX_AC_SUCCESS_CONTRACT_CHARS",
    "ACContextReference",
    "ACContextReferenceManifest",
    "ACContextReferenceKind",
    "ACExecutionCapsule",
    "ACExecutionCapsuleManifest",
    "ACSuccessContract",
    "bind_capsule_to_runtime_handle",
    "build_ac_dependency_references",
    "build_ac_dispatch_authority_scope",
    "compile_ac_execution_capsule",
]
