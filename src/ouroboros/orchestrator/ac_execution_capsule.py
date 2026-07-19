"""Provider-neutral execution capsule for one Ouroboros acceptance criterion.

The capsule is the runtime-owned boundary above Claude, Codex, and every other
``AgentRuntime`` driver.  It deliberately carries compact facts and references,
not a provider transcript: durable workflow state lives in the workspace, Seed,
event ledger, and verify-gate records.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import os

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.execution_runtime_scope import ACRuntimeIdentity
from ouroboros.orchestrator.level_context import LevelContext

AC_EXECUTION_CAPSULE_VERSION = 1
DEFAULT_AC_CONTEXT_BUDGET_CHARS = 12_000
_MAX_REFERENCE_LOCATOR_CHARS = 2_048
_MAX_REFERENCE_HINT_CHARS = 240


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


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
        if self.digest is not None and (
            len(self.digest) != len("sha256:") + 64 or not self.digest.startswith("sha256:")
        ):
            raise ValueError("context reference digest is malformed")
        if self.digest is not None:
            try:
                int(self.digest.removeprefix("sha256:"), 16)
            except ValueError as exc:
                raise ValueError("context reference digest is malformed") from exc

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
class ACSuccessContract:
    """Seed-authored acceptance gate projected into a capsule."""

    verify_command: str | None = None
    expected_artifacts: tuple[str, ...] = ()
    output_assertion: str | None = None

    def __post_init__(self) -> None:
        if self.verify_command is not None and not isinstance(self.verify_command, str):
            raise ValueError("success contract verify command is invalid")
        if any(not isinstance(path, str) or not path for path in self.expected_artifacts):
            raise ValueError("success contract artifacts are invalid")
        if self.output_assertion is not None and not isinstance(self.output_assertion, str):
            raise ValueError("success contract output assertion is invalid")

    def to_contract_data(self) -> dict[str, object]:
        return {
            "verify_command": self.verify_command,
            "expected_artifacts": list(self.expected_artifacts),
            "output_assertion": self.output_assertion,
        }

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
        if self.fresh_session_required is not True:
            raise ValueError("an AC execution capsule must require a fresh session")
        if not os.path.isabs(self.workspace) or os.path.realpath(self.workspace) != self.workspace:
            raise ValueError("capsule workspace must be a canonical absolute path")
        if self.node_id is not None and (not isinstance(self.node_id, str) or not self.node_id):
            raise ValueError("capsule node id is invalid")
        if not self.context_references:
            raise ValueError("capsule must contain at least one context reference")

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
            "workspace": self.workspace,
            "authority_scope": self.authority_scope,
            "seed_goal": self.seed_goal,
            "ac_content": self.ac_content,
            "success_contract": self.success_contract.to_contract_data(),
            "context_references": [
                reference.to_contract_data() for reference in self.context_references
            ],
            "context_budget_chars": self.context_budget_chars,
            "fresh_session_required": self.fresh_session_required,
        }

    @classmethod
    def from_contract_data(cls, raw: object) -> ACExecutionCapsule:
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
            "workspace",
            "authority_scope",
            "seed_goal",
            "ac_content",
            "success_contract",
            "context_references",
            "context_budget_chars",
            "fresh_session_required",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("AC execution capsule has an invalid shape")
        references = raw.get("context_references")
        if not isinstance(references, list):
            raise ValueError("capsule context references are invalid")
        scalar_fields = {
            name: raw.get(name)
            for name in (
                "execution_id",
                "semantic_ac_key",
                "ac_id",
                "session_scope_id",
                "session_attempt_id",
                "workspace",
                "authority_scope",
                "seed_goal",
                "ac_content",
            )
        }
        if any(not isinstance(value, str) for value in scalar_fields.values()):
            raise ValueError("capsule string field is invalid")
        node_id = raw.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise ValueError("capsule node id is invalid")
        return cls(
            version=raw.get("version"),  # type: ignore[arg-type]
            execution_id=scalar_fields["execution_id"],  # type: ignore[arg-type]
            semantic_ac_key=scalar_fields["semantic_ac_key"],  # type: ignore[arg-type]
            ac_id=scalar_fields["ac_id"],  # type: ignore[arg-type]
            session_scope_id=scalar_fields["session_scope_id"],  # type: ignore[arg-type]
            session_attempt_id=scalar_fields["session_attempt_id"],  # type: ignore[arg-type]
            node_id=node_id,
            retry_attempt=raw.get("retry_attempt"),  # type: ignore[arg-type]
            segment_index=raw.get("segment_index"),  # type: ignore[arg-type]
            workspace=scalar_fields["workspace"],  # type: ignore[arg-type]
            authority_scope=scalar_fields["authority_scope"],  # type: ignore[arg-type]
            seed_goal=scalar_fields["seed_goal"],  # type: ignore[arg-type]
            ac_content=scalar_fields["ac_content"],  # type: ignore[arg-type]
            success_contract=ACSuccessContract.from_contract_data(raw.get("success_contract")),
            context_references=tuple(
                ACContextReference.from_contract_data(reference) for reference in references
            ),
            context_budget_chars=raw.get("context_budget_chars"),  # type: ignore[arg-type]
            fresh_session_required=raw.get("fresh_session_required"),  # type: ignore[arg-type]
        )

    def to_prompt_reference_block(self) -> str:
        """Render the small external-memory index given to the provider driver."""
        lines = [
            "## Ouroboros AC Runtime",
            "This AC runs in a fresh provider context. The shared workspace and "
            "Ouroboros event/gate records are authoritative; inspect referenced "
            "sources as needed instead of assuming prior chat history.",
            f"Capsule: {self.fingerprint}",
            "Context references:",
        ]
        for reference in self.context_references:
            hint = f" — {reference.hint}" if reference.hint else ""
            lines.append(f"- {reference.kind.value}: {reference.locator}{hint}")
        return "\n".join(lines)


def _dependency_references(
    execution_id: str,
    level_contexts: Sequence[LevelContext],
) -> list[ACContextReference]:
    references: list[ACContextReference] = []
    for context in level_contexts:
        for summary in context.completed_acs:
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
            references.append(
                ACContextReference(
                    kind=ACContextReferenceKind.DEPENDENCY,
                    locator=f"execution:{execution_id}:ac:{summary.ac_index + 1}",
                    digest=_sha256_text(_canonical_json(payload)),
                    hint=f"accepted dependency from level {context.level_number + 1}",
                )
            )
    return references


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
    level_contexts: Sequence[LevelContext] = (),
    segment_index: int = 0,
    context_budget_chars: int = DEFAULT_AC_CONTEXT_BUDGET_CHARS,
) -> ACExecutionCapsule:
    """Compile one deterministic capsule from existing orchestrator authority."""
    canonical_workspace = os.path.realpath(workspace)
    success_contract = ACSuccessContract.from_ac_spec(ac_spec)
    references: list[ACContextReference] = [
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
    references.extend(_dependency_references(execution_id, level_contexts))
    references.extend(
        ACContextReference(
            kind=ACContextReferenceKind.ARTIFACT,
            locator=f"workspace:{path}",
            hint="seed-authored expected artifact",
        )
        for path in success_contract.expected_artifacts
    )
    if ac_spec is not None and ac_spec.has_success_contract:
        references.append(
            ACContextReference(
                kind=ACContextReferenceKind.GATE,
                locator=f"gate:{runtime_identity.ac_id}",
                digest=_sha256_text(_canonical_json(success_contract.to_contract_data())),
                hint="authoritative acceptance contract",
            )
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
        context_references=tuple(references),
        context_budget_chars=context_budget_chars,
    )


def bind_capsule_to_runtime_handle(
    capsule: ACExecutionCapsule,
    runtime_handle: RuntimeHandle | None,
    *,
    restored_same_attempt: bool,
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
    "ACContextReference",
    "ACContextReferenceKind",
    "ACExecutionCapsule",
    "ACSuccessContract",
    "bind_capsule_to_runtime_handle",
    "compile_ac_execution_capsule",
]
