"""Durable execution contract construction, restoration and projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from ouroboros.config.models import EconomicsConfig
from ouroboros.core.execution_preferences import (
    ResolvedExecutionPreferences,
    execution_preferences_from_contract,
)
from ouroboros.core.project_identity import ProjectIdentity, ProjectIdentityError
from ouroboros.core.seed import Seed
from ouroboros.orchestrator.adapter import AgentRuntime, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.execution_authority import (
    _ProcessLocalAuthorityGeneration,
    valid_process_local_authority_contract,
)
from ouroboros.orchestrator.execution_guidance import ExecutionGuidanceBundle
from ouroboros.orchestrator.execution_semantics import (
    ExecutionSemanticsRejection,
)
from ouroboros.orchestrator.legacy_identity import note_legacy_identity_path
from ouroboros.orchestrator.mcp_tools import SessionToolCatalog, serialize_tool_catalog
from ouroboros.orchestrator.model_routing import ModelRouter
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.session import SESSION_START_IDENTITY_PROGRESS_KEY
from ouroboros.orchestrator.workflow_state import ActivityType

if TYPE_CHECKING:
    from ouroboros.orchestrator.runner import OrchestratorError


_MAX_EXECUTION_STRATEGY_TOOLS = 256

_MAX_EXECUTION_STRATEGY_TEXT_CHARS = 100_000

_MAX_EXECUTION_TOOL_CATALOG_CHARS = 1_000_000

_MAX_EXECUTION_ALLOWED_TOOLS = 1024

_MAX_EXECUTION_CONTEXT_FRAGMENT_CHARS = 100_000

_MAX_EXECUTION_PROFILE_CHARS = 100_000

_MAX_EXECUTION_RUNTIME_HANDLE_CHARS = 1_000_000

EXECUTION_CONTRACT_VERSION = 9

EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "foundation_a_authority",
        "execution_preferences",
        "execution_semantics",
        "execution_inputs",
        "model_routing",
        "frugality_proof",
        "guidance",
        "resume",
    }
)

PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION = 2

PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION = 3

PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION = 4

PRE_EXECUTION_INPUTS_EXECUTION_CONTRACT_VERSION = 5

PRE_RESOLVED_EFFECT_INPUTS_EXECUTION_CONTRACT_VERSION = 6

PRE_DURABLE_PAUSE_POLICY_EXECUTION_CONTRACT_VERSION = 7

PRE_RUNTIME_EFFECT_CAPABILITIES_EXECUTION_CONTRACT_VERSION = 8

FRUGALITY_PROOF_PROTOCOL_VERSION = 1

_MISSING = object()

EXECUTION_CONTRACT_PROGRESS_KEY = "execution_contract"


def _mapping_has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond a finite durable-contract schema."""

    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


@dataclass(frozen=True, slots=True)
class _PersistedExecutionStrategy:
    """Effect-bearing prompt/tool strategy restored without live config reads."""

    tools: tuple[str, ...]
    system_prompt_fragment: str
    task_prompt_suffix: str
    activity_map: tuple[tuple[str, ActivityType], ...]

    def get_tools(self) -> list[str]:
        return list(self.tools)

    def get_system_prompt_fragment(self) -> str:
        return self.system_prompt_fragment

    def get_task_prompt_suffix(self) -> str:
        return self.task_prompt_suffix

    def get_activity_map(self) -> dict[str, ActivityType]:
        return dict(self.activity_map)


def _require_exact_execution_contract_v9(
    raw_contract: object, *, _error_type: type[OrchestratorError]
) -> Mapping[str, Any]:
    """Return a current contract only when its complete top-level shape is canonical."""
    raw_version = raw_contract.get("version") if isinstance(raw_contract, Mapping) else None
    if (
        not isinstance(raw_contract, Mapping)
        or isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
        or raw_version != EXECUTION_CONTRACT_VERSION
    ):
        raise _error_type(
            message="Cannot resume with an invalid execution contract",
            details={"contract_version": raw_version},
        )

    actual_top_level_keys = frozenset(raw_contract)
    if actual_top_level_keys != EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS:
        missing_keys = sorted(EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS - actual_top_level_keys)
        unknown_keys = sorted(
            key if isinstance(key, str) else repr(key)
            for key in actual_top_level_keys - EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS
        )
        raise _error_type(
            message="Cannot resume with an invalid execution contract top-level schema",
            details={
                "contract_version": raw_version,
                "invalid": "top_level_schema",
                "missing": missing_keys,
                "unknown": unknown_keys,
            },
        )
    return raw_contract


@dataclass(slots=True)
class ContractStateCell[T]:
    """Read and immediately write the runner's existing state; never cache a value."""

    read: Callable[[], T]
    write: Callable[[T], None]

    @property
    def value(self) -> T:
        return self.read()

    @value.setter
    def value(self, value: T) -> None:
        self.write(value)


@dataclass(frozen=True, slots=True)
class ExecutionContractState:
    """Nine callback-backed fields whose partial restore mutations are observable."""

    _execution_contract: ContractStateCell[dict[str, Any] | None]
    _execution_guidance: ContractStateCell[ExecutionGuidanceBundle | None]
    _execution_preferences: ContractStateCell[ResolvedExecutionPreferences]
    _model_router: ContractStateCell[ModelRouter | None]
    _requested_model_tier: ContractStateCell[str | None]
    _route_economics: ContractStateCell[EconomicsConfig]
    _shadow_replay_enabled: ContractStateCell[bool]
    _transcript_verifier: ContractStateCell[Literal["decide", "record"]]
    _verify_shell_identity: ContractStateCell[dict[str, str] | None]


class AuthoritativeModelRouterCallback(Protocol):
    def __call__(
        self, preferences: ResolvedExecutionPreferences, *, requested_model_tier: str | None
    ) -> ModelRouter | None: ...


class BuildExecutionContractCallback(Protocol):
    def __call__(
        self,
        *,
        seed: Seed | None = None,
        seed_fingerprint: str | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
        execution_inputs_contract: Mapping[str, Any] | None = None,
        project_identity: ProjectIdentity | None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> dict[str, Any]: ...


class ExecutionInputsSnapshotCallback(Protocol):
    def __call__(
        self, execution_contract: Mapping[str, Any] | None, *, require_bound: bool
    ) -> dict[str, object]: ...


class NormalizeExecutionInputsContractCallback(Protocol):
    def __call__(self, value: object, *, require_bound: bool) -> dict[str, object] | None: ...


class ResolveGuidanceBundleCallback(Protocol):
    def __call__(
        self, guidance_ids: tuple[str, ...], *, expected_metadata: Mapping[str, Any] | None = None
    ) -> ExecutionGuidanceBundle: ...


class PreAdaptiveCallback(Protocol):
    def __call__(
        self,
        value: object,
        persisted_fingerprint: object,
        *,
        fingerprint: Callable[[Mapping[str, object]], str],
    ) -> ExecutionSemanticsRejection | None: ...


@dataclass(frozen=True, slots=True)
class ExecutionContractDependencies:
    """Late-bound runner operations and observations; no lifecycle ownership."""

    _authoritative_model_router: AuthoritativeModelRouterCallback
    _build_execution_contract: BuildExecutionContractCallback
    _build_execution_inputs_contract: Callable[[Seed | None], dict[str, object]]
    _constructor_model_contract: Callable[[], dict[str, Any]]
    _ensure_new_run_guidance: Callable[[], ExecutionGuidanceBundle]
    _execution_inputs_fingerprint: Callable[[Mapping[str, Any]], str]
    _execution_inputs_snapshot: ExecutionInputsSnapshotCallback
    _execution_semantics_contract: Callable[[], dict[str, object]]
    _execution_semantics_fingerprint: Callable[[Mapping[str, Any]], str]
    _guidance_contract: Callable[[ExecutionGuidanceBundle], dict[str, Any]]
    _legacy_proof_workspace_identity: Callable[[], dict[str, str] | None]
    _llm_backend_contract: Callable[[], str | None]
    _normalize_execution_inputs_contract: NormalizeExecutionInputsContractCallback
    _permission_mode_contract: Callable[[], dict[str, Any]]
    _process_local_authority_contract: Callable[
        [_ProcessLocalAuthorityGeneration], dict[str, object]
    ]
    _project_identity: Callable[[], ProjectIdentity | None]
    _project_identity_error: Callable[[ProjectIdentityError], OrchestratorError]
    _project_start_identity: Callable[[Mapping[str, Any]], tuple[bool, ProjectIdentity | None]]
    _proof_workspace_identity: Callable[[], dict[str, str] | None]
    _resolve_guidance_bundle: ResolveGuidanceBundleCallback
    _resolved_proof_workspace_identity: Callable[[ProjectIdentity | None], dict[str, str] | None]
    _resolved_shadow_replay_enabled: Callable[[], bool]
    _restore_guidance_contract: Callable[[Mapping[str, Any]], None]
    _resume_workspace_identity: Callable[[], dict[str, str] | None]
    _routing_fingerprint: Callable[[Mapping[str, Any]], str]
    _runtime_backend_contract: Callable[[], str | None]
    _runtime_execution_authorizes_automatic_codex_resume: Callable[[object], bool]
    _runtime_execution_identity_contract: Callable[[RuntimeHandle | None], dict[str, Any]]
    _runtime_execution_proves_effective_model: Callable[[object], bool]
    _seed_semantics_fingerprint: Callable[[Seed], str]
    _valid_constructor_model_contract: Callable[[object], bool]
    _valid_execution_semantics_contract: Callable[[object], bool]
    _valid_legacy_preflight_execution_semantics_contract: Callable[[object], bool]
    _valid_permission_mode_contract: Callable[[object], bool]
    _valid_runtime_execution_identity_contract: Callable[[object], bool]
    _adapter: Callable[[], AgentRuntime]
    _configured_route_economics: Callable[[], EconomicsConfig]
    _execution_preferences_override_explicit: Callable[[], bool]
    _model_routing_override_explicit: Callable[[], bool]
    _reasoning_effort: Callable[[], str | None]
    uuid4: Callable[[], UUID]
    _require_exact_execution_contract_v9: Callable[[object], Mapping[str, Any]]
    pre_adaptive_execution_semantics_rejection: PreAdaptiveCallback
    migrated_pre_verify_shell_execution_semantics: Callable[[object], dict[str, object] | None]
    migrated_pre_exec_sandbox_execution_semantics: Callable[[object], dict[str, object] | None]
    migrated_pre_transcript_verifier_execution_semantics: Callable[
        [object], dict[str, object] | None
    ]
    error_type: type[OrchestratorError]


class ExecutionContract:
    """Own durable contract transformations while the runner owns live execution."""

    def __init__(
        self, state: ExecutionContractState, dependencies: ExecutionContractDependencies
    ) -> None:
        self._state = state
        self._dependencies = dependencies

    @staticmethod
    def _routing_fingerprint(routing_contract: Mapping[str, Any]) -> str:
        """Hash a resolved routing contract into a stable cohort key."""
        encoded = json.dumps(
            dict(routing_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _execution_semantics_fingerprint(semantics_contract: Mapping[str, Any]) -> str:
        """Hash the complete scalar executor contract used by resume."""
        encoded = json.dumps(
            dict(semantics_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _execution_inputs_fingerprint(inputs_contract: Mapping[str, Any]) -> str:
        """Hash the resolved prompt and complete provider tool authority."""
        encoded = json.dumps(
            dict(inputs_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_execution_inputs_contract(
        value: object,
        *,
        require_bound: bool,
        _exact_keys: Callable[[object, frozenset[str]], bool],
    ) -> dict[str, object] | None:
        """Return a bounded plain copy of the exact durable input schema."""
        input_keys = frozenset(
            {
                "schema_version",
                "strategy",
                "allowed_tools",
                "tool_catalog_json",
                "tool_catalog_fingerprint",
                "context_pack_fragment",
                "execution_profile_json",
                "inherited_runtime_handle_json",
            }
        )
        strategy_keys = frozenset(
            {
                "schema_version",
                "resolver",
                "tools",
                "system_prompt_fragment",
                "task_prompt_suffix",
                "activity_map",
            }
        )
        try:
            if not _exact_keys(value, input_keys):
                return None
            assert isinstance(value, Mapping)
            if value.get("schema_version") != 2:
                return None
            raw_strategy = value.get("strategy")
            if not _exact_keys(raw_strategy, strategy_keys):
                return None
            assert isinstance(raw_strategy, Mapping)
            if raw_strategy.get("schema_version") != 1:
                return None
            resolver = raw_strategy.get("resolver")
            tools = raw_strategy.get("tools")
            system_fragment = raw_strategy.get("system_prompt_fragment")
            task_suffix = raw_strategy.get("task_prompt_suffix")
            raw_activity_map = raw_strategy.get("activity_map")
            context_pack_fragment = value.get("context_pack_fragment")
            execution_profile_json = value.get("execution_profile_json")
            inherited_runtime_handle_json = value.get("inherited_runtime_handle_json")
            if (
                resolver not in {"registry", "profile_backed"}
                or type(tools) is not list
                or not tools
                or len(tools) > _MAX_EXECUTION_STRATEGY_TOOLS
                or any(type(tool) is not str or not tool or len(tool) > 256 for tool in tools)
                or len(set(tools)) != len(tools)
                or type(system_fragment) is not str
                or not system_fragment
                or len(system_fragment) > _MAX_EXECUTION_STRATEGY_TEXT_CHARS
                or type(task_suffix) is not str
                or not task_suffix
                or len(task_suffix) > _MAX_EXECUTION_STRATEGY_TEXT_CHARS
                or type(raw_activity_map) is not list
                or len(raw_activity_map) > _MAX_EXECUTION_STRATEGY_TOOLS
                or type(context_pack_fragment) is not str
                or len(context_pack_fragment) > _MAX_EXECUTION_CONTEXT_FRAGMENT_CHARS
            ):
                return None
            normalized_activity: list[list[str]] = []
            activity_tools: set[str] = set()
            allowed_activity_values = {activity.value for activity in ActivityType}
            for row in raw_activity_map:
                if (
                    type(row) is not list
                    or len(row) != 2
                    or type(row[0]) is not str
                    or not row[0]
                    or len(row[0]) > 256
                    or row[0] in activity_tools
                    or row[0] not in tools
                    or type(row[1]) is not str
                    or row[1] not in allowed_activity_values
                ):
                    return None
                activity_tools.add(row[0])
                normalized_activity.append([row[0], row[1]])

            allowed_tools = value.get("allowed_tools")
            catalog_json = value.get("tool_catalog_json")
            catalog_fingerprint = value.get("tool_catalog_fingerprint")
            unbound = allowed_tools is None and catalog_json is None and catalog_fingerprint is None
            if unbound:
                if require_bound:
                    return None
                normalized_allowed_tools: list[str] | None = None
            else:
                if (
                    type(allowed_tools) is not list
                    or len(allowed_tools) > _MAX_EXECUTION_ALLOWED_TOOLS
                    or any(
                        type(tool) is not str or not tool or len(tool) > 256
                        for tool in allowed_tools
                    )
                    or len(set(allowed_tools)) != len(allowed_tools)
                    or type(catalog_json) is not str
                    or len(catalog_json) > _MAX_EXECUTION_TOOL_CATALOG_CHARS
                    or type(catalog_fingerprint) is not str
                    or len(catalog_fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in catalog_fingerprint)
                    or hashlib.sha256(catalog_json.encode("utf-8")).hexdigest()
                    != catalog_fingerprint
                ):
                    return None
                decoded_catalog = json.loads(catalog_json)
                if not isinstance(decoded_catalog, list | dict):
                    return None
                canonical_catalog = json.dumps(
                    decoded_catalog,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_catalog != catalog_json:
                    return None
                normalized_allowed_tools = list(allowed_tools)

            if execution_profile_json is None:
                normalized_profile_json: str | None = None
            else:
                if (
                    type(execution_profile_json) is not str
                    or len(execution_profile_json) > _MAX_EXECUTION_PROFILE_CHARS
                ):
                    return None
                decoded_profile = json.loads(execution_profile_json)
                if not isinstance(decoded_profile, dict):
                    return None
                normalized_profile = ExecutionProfile.model_validate(decoded_profile)
                canonical_profile = json.dumps(
                    normalized_profile.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_profile != execution_profile_json:
                    return None
                normalized_profile_json = execution_profile_json

            if inherited_runtime_handle_json is None:
                normalized_runtime_handle_json: str | None = None
            else:
                if (
                    type(inherited_runtime_handle_json) is not str
                    or len(inherited_runtime_handle_json) > _MAX_EXECUTION_RUNTIME_HANDLE_CHARS
                ):
                    return None
                decoded_runtime_handle = json.loads(inherited_runtime_handle_json)
                if not isinstance(decoded_runtime_handle, dict):
                    return None
                normalized_runtime_handle = RuntimeHandle.from_dict(decoded_runtime_handle)
                if normalized_runtime_handle is None:
                    return None
                canonical_runtime_handle = json.dumps(
                    normalized_runtime_handle.to_persisted_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_runtime_handle != inherited_runtime_handle_json:
                    return None
                normalized_runtime_handle_json = inherited_runtime_handle_json

            return {
                "schema_version": 2,
                "strategy": {
                    "schema_version": 1,
                    "resolver": resolver,
                    "tools": list(tools),
                    "system_prompt_fragment": system_fragment,
                    "task_prompt_suffix": task_suffix,
                    "activity_map": normalized_activity,
                },
                "context_pack_fragment": context_pack_fragment,
                "execution_profile_json": normalized_profile_json,
                "inherited_runtime_handle_json": normalized_runtime_handle_json,
                "allowed_tools": normalized_allowed_tools,
                "tool_catalog_json": catalog_json,
                "tool_catalog_fingerprint": catalog_fingerprint,
            }
        except Exception:
            return None

    def _execution_inputs_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> dict[str, object]:
        """Return the validated immutable provider-input population."""
        raw_inputs = (
            execution_contract.get("execution_inputs")
            if isinstance(execution_contract, Mapping)
            else None
        )
        normalized = self._dependencies._normalize_execution_inputs_contract(
            raw_inputs,
            require_bound=require_bound,
        )
        if normalized is None:
            raise self._dependencies.error_type(
                message="Cannot execute with an invalid prompt/tool input snapshot",
                details={"invalid": "execution_inputs"},
            )
        return normalized

    def _execution_strategy_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> _PersistedExecutionStrategy:
        normalized = self._dependencies._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        strategy = normalized["strategy"]
        assert isinstance(strategy, dict)
        raw_activity = strategy["activity_map"]
        assert isinstance(raw_activity, list)
        return _PersistedExecutionStrategy(
            tools=tuple(strategy["tools"]),
            system_prompt_fragment=strategy["system_prompt_fragment"],
            task_prompt_suffix=strategy["task_prompt_suffix"],
            activity_map=tuple((row[0], ActivityType(row[1])) for row in raw_activity),
        )

    def _execution_context_pack_fragment_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> str:
        """Return context text resolved before the durable session existed."""
        inputs = self._dependencies._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        fragment = inputs["context_pack_fragment"]
        assert isinstance(fragment, str)
        return fragment

    def _execution_profile_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> ExecutionProfile | None:
        """Restore the complete profile without rereading mutable YAML."""
        inputs = self._dependencies._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        raw_profile = inputs["execution_profile_json"]
        if raw_profile is None:
            return None
        assert isinstance(raw_profile, str)
        return ExecutionProfile.model_validate(json.loads(raw_profile))

    def _execution_inherited_runtime_handle_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> RuntimeHandle | None:
        """Restore parent conversation lineage from the durable snapshot."""
        inputs = self._dependencies._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        raw_handle = inputs["inherited_runtime_handle_json"]
        if raw_handle is None:
            return None
        assert isinstance(raw_handle, str)
        restored = RuntimeHandle.from_dict(json.loads(raw_handle))
        if restored is None:  # pragma: no cover - exact-schema validation owns this path
            raise self._dependencies.error_type(
                message="Cannot restore inherited runtime authority"
            )
        return restored

    def _bind_execution_tool_authority(
        self,
        execution_contract: Mapping[str, Any],
        *,
        merged_tools: list[str],
        tool_catalog: SessionToolCatalog,
    ) -> tuple[dict[str, Any], bool]:
        """Seal or validate the exact allowed tools and complete catalog."""
        normalized = self._dependencies._normalize_execution_inputs_contract(
            execution_contract.get("execution_inputs"),
            require_bound=False,
        )
        if normalized is None:
            raise self._dependencies.error_type(
                message="Cannot bind an invalid prompt/tool input snapshot",
                details={"invalid": "execution_inputs"},
            )
        serialized_catalog = serialize_tool_catalog(tool_catalog)
        try:
            catalog_json = json.dumps(
                serialized_catalog,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (TypeError, ValueError) as exc:
            raise self._dependencies.error_type(
                message="Cannot persist a non-canonical tool catalog",
                details={"cause": type(exc).__name__},
            ) from exc
        if len(catalog_json) > _MAX_EXECUTION_TOOL_CATALOG_CHARS:
            raise self._dependencies.error_type(message="Cannot persist an oversized tool catalog")
        current_allowed = list(merged_tools)
        if (
            len(current_allowed) > _MAX_EXECUTION_ALLOWED_TOOLS
            or any(type(tool) is not str or not tool or len(tool) > 256 for tool in current_allowed)
            or len(set(current_allowed)) != len(current_allowed)
        ):
            raise self._dependencies.error_type(
                message="Cannot persist an invalid allowed-tool catalog"
            )

        persisted_catalog = normalized["tool_catalog_json"]
        persisted_allowed = normalized["allowed_tools"]
        if persisted_catalog is not None:
            if persisted_catalog != catalog_json or persisted_allowed != current_allowed:
                raise self._dependencies.error_type(
                    message="Cannot resume with changed prompt/tool authority",
                    details={
                        "resume_blocked": "execution_tool_authority_drift",
                        "hint": "Restore the original tool catalog or start a new session.",
                    },
                )
            return dict(execution_contract), False

        normalized["allowed_tools"] = current_allowed
        normalized["tool_catalog_json"] = catalog_json
        normalized["tool_catalog_fingerprint"] = hashlib.sha256(
            catalog_json.encode("utf-8")
        ).hexdigest()
        bound_contract = deepcopy(dict(execution_contract))
        bound_contract["execution_inputs"] = normalized
        proof = bound_contract.get("frugality_proof")
        if not isinstance(proof, dict):
            raise self._dependencies.error_type(message="Cannot bind an invalid proof contract")
        proof["execution_inputs_fingerprint"] = self._dependencies._execution_inputs_fingerprint(
            normalized
        )
        return bound_contract, True

    def _execution_semantics_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Return one validated immutable-input snapshot from a durable contract."""
        raw_execution_semantics = (
            execution_contract.get("execution_semantics")
            if isinstance(execution_contract, Mapping)
            else None
        )
        if not self._dependencies._valid_execution_semantics_contract(raw_execution_semantics):
            raise self._dependencies.error_type(
                message="Cannot execute with an invalid execution-semantics snapshot",
                details={"invalid": "execution_semantics"},
            )
        return dict(raw_execution_semantics)

    @staticmethod
    def _seed_semantics_fingerprint(seed: Seed) -> str:
        """Hash executable Seed semantics while excluding volatile identity fields."""
        payload = seed.to_dict()
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata = dict(metadata)
            for key in ("seed_id", "created_at", "interview_id", "parent_seed_id"):
                metadata.pop(key, None)
            payload["metadata"] = metadata
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _valid_permission_mode_contract(value: object) -> bool:
        if not isinstance(value, Mapping) or value.get("observed") is not True:
            return False
        mode = value.get("mode")
        return set(value) == {"observed", "mode"} and isinstance(mode, str) and bool(mode.strip())

    @staticmethod
    def _guidance_contract(bundle: ExecutionGuidanceBundle) -> dict[str, Any]:
        return {
            "mode": "declared" if bundle.refs else "disabled",
            "provenance_scope": "ouroboros_declared_guidance_only",
            **bundle.to_metadata(),
        }

    def _restore_guidance_contract(self, raw_contract: Mapping[str, Any]) -> None:
        """Restore persisted guidance refs without consulting the current allowlist."""
        raw_guidance = raw_contract.get("guidance")
        if not isinstance(raw_guidance, Mapping):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance"},
            )

        mode = raw_guidance.get("mode")
        provenance_scope = raw_guidance.get("provenance_scope")
        items = raw_guidance.get("items")
        if (
            mode not in {"disabled", "declared"}
            or provenance_scope != "ouroboros_declared_guidance_only"
            or not isinstance(items, list)
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance metadata"},
            )

        guidance_ids: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "guidance item"},
                )
            guidance_id = item.get("id")
            if not isinstance(guidance_id, str) or not guidance_id.strip():
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "guidance id"},
                )
            guidance_ids.append(guidance_id.strip())
        if (mode == "disabled") != (not guidance_ids):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance mode"},
            )

        expected_metadata = {
            key: value
            for key, value in raw_guidance.items()
            if key not in {"mode", "provenance_scope"}
        }
        self._state._execution_guidance.value = self._dependencies._resolve_guidance_bundle(
            tuple(guidance_ids),
            expected_metadata=expected_metadata,
        )

    def _build_execution_contract(
        self,
        *,
        seed: Seed | None = None,
        seed_fingerprint: str | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
        execution_inputs_contract: Mapping[str, Any] | None = None,
        project_identity: ProjectIdentity | None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> dict[str, Any]:
        """Build the durable resolved inputs shared by resume and proof cohorting."""
        from ouroboros.orchestrator.model_routing import serialize_model_router
        from ouroboros.orchestrator.resolved_models import resolved_models_contract
        from ouroboros.orchestrator.route_compat import (
            build_route_compat_projection,
            serialize_route_compat_contract,
        )

        guidance_bundle = self._dependencies._ensure_new_run_guidance()
        execution_semantics = self._dependencies._execution_semantics_contract()
        if execution_inputs_contract is None:
            execution_inputs = self._dependencies._build_execution_inputs_contract(seed)
        else:
            normalized_inputs = self._dependencies._normalize_execution_inputs_contract(
                execution_inputs_contract,
                require_bound=True,
            )
            if normalized_inputs is None:
                raise self._dependencies.error_type(
                    message="Cannot preserve an invalid execution input contract",
                    details={"invalid": "execution_inputs"},
                )
            execution_inputs = normalized_inputs
        routing_contract = serialize_model_router(self._state._model_router.value)
        routing_contract["requested_model_tier"] = self._state._requested_model_tier.value
        # Effort routing is independent of model-tier routing. Persist it even
        # when the optional model router is dormant so resume cannot silently
        # adopt a different provider-effect policy.
        routing_contract["reasoning_effort"] = self._dependencies._reasoning_effort()
        route_projection = build_route_compat_projection(
            self._state._route_economics.value,
            model_router=self._state._model_router.value,
            runtime_backend=getattr(self._dependencies._adapter(), "runtime_backend", None),
            effort=self._dependencies._reasoning_effort(),
        )
        routing_contract["route_compat"] = serialize_route_compat_contract(route_projection)
        # This base effort reaches both direct runner dispatches and the
        # parallel executor. Persist it beside model routing so a config/env
        # change cannot silently alter ``model_reasoning_effort`` on resume.
        routing_contract["base_reasoning_effort"] = self._dependencies._reasoning_effort()
        routing_contract["constructor_model"] = self._dependencies._constructor_model_contract()
        routing_contract["resolved_models"] = resolved_models_contract(
            self._state._route_economics.value,
            runtime_backend=getattr(self._dependencies._adapter(), "runtime_backend", None),
            execute_model=routing_contract["constructor_model"].get("model"),
        )
        routing_contract["runtime_execution"] = (
            self._dependencies._runtime_execution_identity_contract(runtime_handle)
        )
        routing_contract["runtime_backend"] = self._dependencies._runtime_backend_contract()
        routing_contract["llm_backend"] = self._dependencies._llm_backend_contract()
        routing_contract["permission_mode"] = self._dependencies._permission_mode_contract()
        proof_contract: dict[str, Any] = {
            "protocol_version": FRUGALITY_PROOF_PROTOCOL_VERSION,
            "routing_fingerprint": self._dependencies._routing_fingerprint(routing_contract),
            "execution_semantics_fingerprint": self._dependencies._execution_semantics_fingerprint(
                execution_semantics
            ),
            "execution_inputs_fingerprint": self._dependencies._execution_inputs_fingerprint(
                execution_inputs
            ),
        }
        workspace_identity = self._dependencies._resolved_proof_workspace_identity(project_identity)
        if workspace_identity is not None:
            proof_contract.update(workspace_identity)
        resolved_seed_fingerprint = seed_fingerprint
        if resolved_seed_fingerprint is None and seed is not None:
            resolved_seed_fingerprint = self._dependencies._seed_semantics_fingerprint(seed)
        if resolved_seed_fingerprint is not None:
            proof_contract["seed_fingerprint"] = resolved_seed_fingerprint
        if authority_generation is None:
            # Diagnostics and contract-validation callers need attribution
            # shape, not a live capability. Do not mint a registry issuance
            # here: there is no session lifecycle that could register and
            # retire it. The random correlation remains evidence-only, and
            # cannot be claimed or registered without an explicit generation.
            authority_contract: dict[str, object] = {
                "version": 1,
                "scope": "process_local",
                "correlation_id": self._dependencies.uuid4().hex,
            }
        else:
            authority_contract = self._dependencies._process_local_authority_contract(
                authority_generation
            )
        contract = {
            "version": EXECUTION_CONTRACT_VERSION,
            "foundation_a_authority": authority_contract,
            "execution_preferences": self._state._execution_preferences.value.to_contract_data(),
            "execution_semantics": execution_semantics,
            "execution_inputs": execution_inputs,
            "model_routing": routing_contract,
            "frugality_proof": proof_contract,
            "guidance": self._dependencies._guidance_contract(guidance_bundle),
            "resume": {
                "workspace": self._dependencies._resume_workspace_identity(),
            },
        }
        if frozenset(contract) != EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS:
            raise AssertionError("execution contract v9 builder emitted a non-canonical shape")
        return contract

    def _restore_execution_contract(
        self,
        progress: Mapping[str, Any],
        *,
        seed: Seed | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
        require_bound_execution_inputs: bool = True,
        prepared_live_execution: bool = False,
        runtime_handle: RuntimeHandle | None = None,
    ) -> bool:
        """Restore the persisted router unless this invocation explicitly overrides it.

        Returns whether a replacement contract (an explicit override or one-time
        legacy migration) should be checkpointed for subsequent resumes. A present
        malformed contract blocks resume; it is never reinterpreted as a legacy
        session or allowed to change models silently. ``prepared_live_execution``
        admits only the explicit unobservable states emitted by the new-run builder;
        the caller must already hold and match that run's sealed process-local
        contract, while the normal durable resume path remains strict.
        """
        if EXECUTION_CONTRACT_PROGRESS_KEY not in progress:
            raise self._dependencies.error_type(
                message="Cannot resume a session without durable effect inputs",
                details={
                    "contract_version": None,
                    "resume_blocked": "execution_inputs_unavailable",
                    "hint": "Start a new session under the current execution contract.",
                },
            )
        raw_contract = progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)

        raw_version = raw_contract.get("version") if isinstance(raw_contract, Mapping) else None
        if (
            not isinstance(raw_contract, Mapping)
            or isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version
            not in {
                PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION,
                PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION,
                PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION,
                PRE_EXECUTION_INPUTS_EXECUTION_CONTRACT_VERSION,
                PRE_RESOLVED_EFFECT_INPUTS_EXECUTION_CONTRACT_VERSION,
                PRE_DURABLE_PAUSE_POLICY_EXECUTION_CONTRACT_VERSION,
                PRE_RUNTIME_EFFECT_CAPABILITIES_EXECUTION_CONTRACT_VERSION,
                EXECUTION_CONTRACT_VERSION,
            }
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"contract_version": raw_version},
            )

        if raw_version != EXECUTION_CONTRACT_VERSION:
            # Every older version may already have dispatched provider effects,
            # but none sealed the complete v9 effect population, including the
            # runtime capability/vocabulary that builds provider-call kwargs.
            # Reconstructing any missing field from current runtime state would
            # change replay authorization.
            raise self._dependencies.error_type(
                message="Cannot resume a session without durable effect inputs",
                details={
                    "contract_version": raw_version,
                    "resume_blocked": "execution_inputs_unavailable",
                    "hint": "Start a new session under the current execution contract.",
                },
            )

        raw_contract = self._dependencies._require_exact_execution_contract_v9(raw_contract)

        migrate_v2_contract = raw_version == PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION
        migrate_v3_contract = raw_version == PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION
        migrate_v4_contract = raw_version == PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION
        migrate_legacy_contract = migrate_v2_contract or migrate_v3_contract or migrate_v4_contract
        raw_proof = raw_contract.get("frugality_proof")
        raw_routing = raw_contract.get("model_routing")
        raw_resume = raw_contract.get("resume")
        raw_preferences = raw_contract.get("execution_preferences")
        preferences_migrated = "execution_preferences" not in raw_contract
        raw_execution_semantics = raw_contract.get("execution_semantics")
        raw_execution_inputs = raw_contract.get("execution_inputs")
        raw_authority = raw_contract.get("foundation_a_authority")
        if (
            not isinstance(raw_proof, Mapping)
            or not isinstance(raw_routing, Mapping)
            or not isinstance(raw_resume, Mapping)
            or not valid_process_local_authority_contract(raw_authority)
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={
                    "missing": "frugality_proof, model_routing, resume, or foundation_a_authority"
                },
            )

        pre_adaptive_rejection = self._dependencies.pre_adaptive_execution_semantics_rejection(
            raw_execution_semantics,
            raw_proof.get("execution_semantics_fingerprint"),
            fingerprint=self._dependencies._execution_semantics_fingerprint,
        )
        if pre_adaptive_rejection is not None:
            raise self._dependencies.error_type(
                message=pre_adaptive_rejection.message,
                details=pre_adaptive_rejection.details,
            )

        migrated_verify_shell_semantics = (
            self._dependencies.migrated_pre_verify_shell_execution_semantics(
                raw_execution_semantics
            )
        )
        if migrated_verify_shell_semantics is not None:
            persisted_v4_fingerprint = raw_proof.get("execution_semantics_fingerprint")
            if not isinstance(
                persisted_v4_fingerprint, str
            ) or persisted_v4_fingerprint != self._dependencies._execution_semantics_fingerprint(
                raw_execution_semantics
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid pre-verify-shell contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            migrated_contract = deepcopy(dict(raw_contract))
            migrated_proof = migrated_contract["frugality_proof"]
            assert isinstance(migrated_proof, dict)
            migrated_contract["execution_semantics"] = migrated_verify_shell_semantics
            migrated_proof["execution_semantics_fingerprint"] = (
                self._dependencies._execution_semantics_fingerprint(migrated_verify_shell_semantics)
            )
            raw_contract = migrated_contract
            raw_proof = migrated_proof
            raw_execution_semantics = migrated_verify_shell_semantics
            self._state._verify_shell_identity.value = None

        migrated_sandbox_semantics = (
            self._dependencies.migrated_pre_exec_sandbox_execution_semantics(
                raw_execution_semantics
            )
        )
        if migrated_sandbox_semantics is not None:
            if raw_proof.get("execution_semantics_fingerprint") != (
                self._dependencies._execution_semantics_fingerprint(raw_execution_semantics)
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid pre-exec-sandbox contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            raw_contract = deepcopy(dict(raw_contract))
            raw_proof = raw_contract["frugality_proof"]
            raw_contract["execution_semantics"] = migrated_sandbox_semantics
            raw_proof["execution_semantics_fingerprint"] = (
                self._dependencies._execution_semantics_fingerprint(migrated_sandbox_semantics)
            )
            raw_execution_semantics = migrated_sandbox_semantics

        migrated_verifier_semantics = (
            self._dependencies.migrated_pre_transcript_verifier_execution_semantics(
                raw_execution_semantics
            )
        )
        if migrated_verifier_semantics is not None:
            if raw_proof.get("execution_semantics_fingerprint") != (
                self._dependencies._execution_semantics_fingerprint(raw_execution_semantics)
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid pre-transcript-verifier contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            raw_contract = deepcopy(dict(raw_contract))
            raw_proof = raw_contract["frugality_proof"]
            raw_contract["execution_semantics"] = migrated_verifier_semantics
            raw_proof["execution_semantics_fingerprint"] = (
                self._dependencies._execution_semantics_fingerprint(migrated_verifier_semantics)
            )
            raw_execution_semantics = migrated_verifier_semantics

        migrate_preflight_contract = (
            self._dependencies._valid_legacy_preflight_execution_semantics_contract(
                raw_execution_semantics
            )
        )
        if migrate_preflight_contract:
            persisted_legacy_fingerprint = raw_proof.get("execution_semantics_fingerprint")
            if (
                not isinstance(persisted_legacy_fingerprint, str)
                or persisted_legacy_fingerprint
                != self._dependencies._execution_semantics_fingerprint(raw_execution_semantics)
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid legacy preflight contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            migrated_contract = deepcopy(dict(raw_contract))
            migrated_semantics = migrated_contract["execution_semantics"]
            migrated_proof = migrated_contract["frugality_proof"]
            assert isinstance(migrated_semantics, dict)
            assert isinstance(migrated_proof, dict)
            migrated_semantics["decomposition_mode"] = "bounce_only"
            migrated_proof["execution_semantics_fingerprint"] = (
                self._dependencies._execution_semantics_fingerprint(migrated_semantics)
            )
            raw_contract = migrated_contract
            raw_proof = migrated_proof
            raw_execution_semantics = migrated_semantics

        # Version 2 predates effort/Route B fields; version 3 predates the
        # independent requested-tier field; version 4 predates the complete
        # executor-semantics snapshot. Only those exact shapes migrate.
        # A malformed current contract must never fall through either path.
        if migrate_legacy_contract and (
            "execution_semantics" in raw_contract
            or "execution_semantics_fingerprint" in raw_proof
            or "execution_inputs" in raw_contract
            or "execution_inputs_fingerprint" in raw_proof
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": f"version {raw_version} execution semantics extension"},
            )
        if migrate_v2_contract and (
            "reasoning_effort" in raw_routing
            or "route_compat" in raw_routing
            or "requested_model_tier" in raw_routing
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "version 2 routing extension"},
            )
        if migrate_v3_contract and "requested_model_tier" in raw_routing:
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "version 3 routing extension"},
            )

        self._dependencies._restore_guidance_contract(raw_contract)

        protocol_version = raw_proof.get("protocol_version")
        persisted_project_root = raw_proof.get("project_root")
        persisted_workspace_path = raw_proof.get("workspace_path")
        persisted_routing_fingerprint = raw_proof.get("routing_fingerprint")
        persisted_execution_semantics_fingerprint = raw_proof.get("execution_semantics_fingerprint")
        persisted_execution_inputs_fingerprint = raw_proof.get("execution_inputs_fingerprint")
        persisted_seed_fingerprint = raw_proof.get("seed_fingerprint")
        from ouroboros.orchestrator import resolved_models as model_replay

        persisted_constructor_model = raw_routing.get("constructor_model")
        persisted_runtime_execution = raw_routing.get("runtime_execution")
        persisted_runtime_backend = raw_routing.get("runtime_backend")
        persisted_llm_backend = raw_routing.get("llm_backend")
        persisted_permission_mode = raw_routing.get("permission_mode")
        persisted_requested_model_tier = raw_routing.get("requested_model_tier")
        persisted_reasoning_effort = (
            self._dependencies._reasoning_effort()
            if migrate_v2_contract
            else raw_routing.get("reasoning_effort")
        )
        persisted_base_reasoning_effort = raw_routing.get("base_reasoning_effort", _MISSING)
        persisted_resume_workspace = raw_resume.get("workspace")
        base_reasoning_effort_missing = persisted_base_reasoning_effort is _MISSING
        if base_reasoning_effort_missing and not migrate_v2_contract:
            # Pre-base-effort contracts already persisted the same dispatch effort
            # under model_routing.reasoning_effort.  Treat that value as the
            # historical base effort instead of rejecting otherwise valid v9
            # checkpoints.
            persisted_base_reasoning_effort = persisted_reasoning_effort
        valid_base_reasoning_effort = (persisted_base_reasoning_effort is None) or (
            isinstance(persisted_base_reasoning_effort, str)
            and persisted_base_reasoning_effort in {"low", "medium", "high", "xhigh"}
        )
        valid_seed_fingerprint = (
            isinstance(persisted_seed_fingerprint, str)
            and len(persisted_seed_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in persisted_seed_fingerprint)
        )
        normalized_execution_inputs = (
            None
            if migrate_legacy_contract
            else self._dependencies._normalize_execution_inputs_contract(
                raw_execution_inputs,
                require_bound=require_bound_execution_inputs,
            )
        )
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version != FRUGALITY_PROOF_PROTOCOL_VERSION
            or not (
                isinstance(persisted_project_root, str)
                and bool(persisted_project_root.strip())
                and isinstance(persisted_workspace_path, str)
                and bool(persisted_workspace_path.strip())
                or prepared_live_execution
                and persisted_project_root is None
                and persisted_workspace_path is None
            )
            or not isinstance(persisted_routing_fingerprint, str)
            or persisted_routing_fingerprint != self._dependencies._routing_fingerprint(raw_routing)
            or (
                not migrate_legacy_contract
                and (
                    not self._dependencies._valid_execution_semantics_contract(
                        raw_execution_semantics
                    )
                    or not isinstance(persisted_execution_semantics_fingerprint, str)
                    or persisted_execution_semantics_fingerprint
                    != self._dependencies._execution_semantics_fingerprint(raw_execution_semantics)
                )
            )
            or (
                not migrate_legacy_contract
                and (
                    normalized_execution_inputs is None
                    or not isinstance(persisted_execution_inputs_fingerprint, str)
                    or persisted_execution_inputs_fingerprint
                    != self._dependencies._execution_inputs_fingerprint(normalized_execution_inputs)
                )
            )
            or (seed is not None and not valid_seed_fingerprint)
            or not (
                self._dependencies._valid_constructor_model_contract(persisted_constructor_model)
                or (prepared_live_execution and persisted_constructor_model == {"observed": False})
            )
            or not model_replay.valid_resolved_models(raw_routing.get("resolved_models"))
            or not self._dependencies._valid_runtime_execution_identity_contract(
                persisted_runtime_execution
            )
            or not (
                isinstance(persisted_runtime_backend, str)
                and bool(persisted_runtime_backend.strip())
                or prepared_live_execution
                and persisted_runtime_backend is None
            )
            or not (
                isinstance(persisted_llm_backend, str)
                and bool(persisted_llm_backend.strip())
                or prepared_live_execution
                and persisted_llm_backend is None
            )
            or not self._dependencies._valid_permission_mode_contract(persisted_permission_mode)
            or (
                not migrate_v2_contract
                and (
                    "reasoning_effort" not in raw_routing
                    or persisted_reasoning_effort not in {None, "low", "medium", "high", "xhigh"}
                )
            )
            or (
                not (migrate_v2_contract or migrate_v3_contract)
                and (
                    "requested_model_tier" not in raw_routing
                    or persisted_requested_model_tier
                    not in {None, "frugal", "standard", "frontier"}
                )
            )
            or not (
                isinstance(persisted_resume_workspace, Mapping)
                or prepared_live_execution
                and persisted_resume_workspace is None
            )
            or not valid_base_reasoning_effort
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "proof identity"},
            )

        persisted_preferences = execution_preferences_from_contract(raw_preferences)
        if persisted_preferences is None:
            raise self._dependencies.error_type(
                message="Cannot resume with invalid execution preferences",
                details={"invalid": "execution_preferences"},
            )
        if (
            self._dependencies._execution_preferences_override_explicit()
            and self._state._execution_preferences.value != persisted_preferences
        ):
            raise self._dependencies.error_type(
                message="Cannot change efficiency or frugality preferences on resume",
                details={
                    "persisted_preferences": persisted_preferences.to_contract_data(),
                    "requested_preferences": self._state._execution_preferences.value.to_contract_data(),
                    "hint": "Start a new successor execution for an intentional change.",
                },
            )

        if persisted_base_reasoning_effort != self._dependencies._reasoning_effort():
            raise self._dependencies.error_type(
                message="Cannot resume with a different reasoning effort",
                details={
                    "persisted_base_reasoning_effort": persisted_base_reasoning_effort,
                    "current_base_reasoning_effort": self._dependencies._reasoning_effort(),
                    "hint": "Start a new session after changing reasoning effort.",
                },
            )

        current_seed_fingerprint = (
            self._dependencies._seed_semantics_fingerprint(seed) if seed is not None else None
        )
        persisted_workspace = (
            None
            if persisted_project_root is None and persisted_workspace_path is None
            else {
                "project_root": persisted_project_root,
                "workspace_path": persisted_workspace_path,
            }
        )
        has_project_anchor, start_project_identity = self._dependencies._project_start_identity(
            progress
        )
        if has_project_anchor:
            assert start_project_identity is not None
            active_project_identity = self._dependencies._project_identity()
            if (
                persisted_workspace != start_project_identity.to_workspace_data()
                or active_project_identity != start_project_identity
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with conflicting project identity",
                    details={
                        "persisted_workspace": persisted_workspace,
                        "start_project_identity": start_project_identity.to_event_data(),
                        "current_project_identity": (
                            active_project_identity.to_event_data()
                            if active_project_identity is not None
                            else None
                        ),
                    },
                )
            active_workspace = active_project_identity.to_workspace_data()
        else:
            # Historical v9 session starts predate the additive project anchor.
            # Preserve their exact direct-cwd representation rather than
            # rewriting durable resume authority under the new resolver.
            # Transitional (#1799): this branch is a package-wide
            # project-identity support contract — see the removal criterion in
            # orchestrator/legacy_identity.py and docs/rfc/project-map-v1.md.
            # It does not bypass other resume gates. Removal is allowed only
            # after the documented identity-compatibility window ends and must
            # replace this path with a typed fail-closed rejection.
            #
            # Current prepared executions intentionally restore an anchorless
            # contract-only mapping; only a durable historical start snapshot
            # that still lacks the anchor counts as a legacy activation.
            raw_start_snapshot = progress.get(SESSION_START_IDENTITY_PROGRESS_KEY)
            if isinstance(raw_start_snapshot, Mapping):
                note_legacy_identity_path(
                    "resume_workspace_comparison",
                    prepared_live_execution=prepared_live_execution,
                )
            active_workspace = (
                self._dependencies._proof_workspace_identity()
                if prepared_live_execution
                else self._dependencies._legacy_proof_workspace_identity()
            )
        if active_workspace != persisted_workspace:
            raise self._dependencies.error_type(
                message="Cannot resume from a different project workspace",
                details={
                    "persisted_workspace": persisted_workspace,
                    "current_workspace": active_workspace,
                    "hint": "Resume from the original project/workspace.",
                },
            )
        replacement_project_identity = start_project_identity
        if replacement_project_identity is None and persisted_workspace is not None:
            # A pre-anchor session owns its historical proof representation in
            # the persisted contract.  Rebuilding for a routing override must
            # carry that representation forward; recomputing under the current
            # resolver would make the following resume disagree with the
            # immutable, still-unanchored start event.
            try:
                replacement_project_identity = ProjectIdentity.from_root(
                    persisted_workspace["project_root"],
                    workspace_path=persisted_workspace["workspace_path"],
                    require_exists=True,
                )
            except ProjectIdentityError as exc:
                raise self._dependencies._project_identity_error(exc) from exc
        active_resume_workspace = self._dependencies._resume_workspace_identity()
        normalized_persisted_resume_workspace = (
            dict(persisted_resume_workspace)
            if isinstance(persisted_resume_workspace, Mapping)
            else None
        )
        if active_resume_workspace != normalized_persisted_resume_workspace:
            raise self._dependencies.error_type(
                message="Cannot resume from a different execution workspace",
                details={
                    "persisted_workspace": normalized_persisted_resume_workspace,
                    "current_workspace": active_resume_workspace,
                    "hint": "Resume from the exact original worktree and branch.",
                },
            )
        current_runtime_backend = self._dependencies._runtime_backend_contract()
        if current_runtime_backend != persisted_runtime_backend:
            raise self._dependencies.error_type(
                message="Cannot resume with a different runtime backend",
                details={
                    "persisted_runtime_backend": persisted_runtime_backend,
                    "current_runtime_backend": current_runtime_backend,
                    "hint": "Resume with the original runtime, or start a new session.",
                },
            )
        current_llm_backend = self._dependencies._llm_backend_contract()
        if current_llm_backend != persisted_llm_backend:
            raise self._dependencies.error_type(
                message="Cannot resume with a different LLM backend",
                details={
                    "persisted_llm_backend": persisted_llm_backend,
                    "current_llm_backend": current_llm_backend,
                    "hint": "Restore the original LLM backend or start a new session.",
                },
            )
        current_permission_mode = self._dependencies._permission_mode_contract()
        if current_permission_mode != persisted_permission_mode:
            raise self._dependencies.error_type(
                message="Cannot resume with a different permission mode",
                details={
                    "persisted_permission_mode": dict(persisted_permission_mode),
                    "current_permission_mode": current_permission_mode,
                    "hint": "Restore the original permission mode or start a new session.",
                },
            )
        if (
            valid_seed_fingerprint
            and current_seed_fingerprint is not None
            and persisted_seed_fingerprint != current_seed_fingerprint
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with a modified Seed",
                details={
                    "persisted_seed_fingerprint": persisted_seed_fingerprint,
                    "current_seed_fingerprint": current_seed_fingerprint,
                    "hint": "Start a new session for changed goals, constraints, or ACs.",
                },
            )
        # Replay the models the run started on instead of resolving them again.
        persisted_resolved = raw_routing.get("resolved_models") or {}
        runtime_backend = getattr(self._dependencies._adapter(), "runtime_backend", None)
        if not self._dependencies._model_routing_override_explicit():
            model_replay.replay_constructor_model(
                self._dependencies._adapter(),
                runtime_backend=runtime_backend,
                persisted=persisted_resolved.get(
                    "execute", persisted_constructor_model.get("model")
                ),
            )
        current_constructor_model = self._dependencies._constructor_model_contract()
        if persisted_constructor_model != current_constructor_model:
            raise self._dependencies.error_type(
                message="Cannot resume with a different constructor model",
                details={
                    "persisted_constructor_model": dict(persisted_constructor_model),
                    "current_constructor_model": current_constructor_model,
                    "hint": (
                        "Resume with the original runtime model, or start a new session "
                        "for an intentional model change."
                    ),
                },
            )
        current_runtime_execution = self._dependencies._runtime_execution_identity_contract(
            runtime_handle
        )
        if persisted_runtime_execution != current_runtime_execution:
            raise self._dependencies.error_type(
                message="Cannot resume with a different runtime execution profile",
                details={
                    "persisted_runtime_execution": dict(persisted_runtime_execution),
                    "current_runtime_execution": current_runtime_execution,
                    "hint": (
                        "Restore the original runtime/model profile, or start a new "
                        "session for an intentional execution-profile change."
                    ),
                },
            )

        from ouroboros.orchestrator.model_routing import (
            deserialize_model_router,
            is_legacy_quality_first_router,
        )

        recognized, restored_router = deserialize_model_router(raw_routing)
        if not recognized:
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "model_routing"},
            )
        if (
            not (migrate_v2_contract or migrate_v3_contract)
            and restored_router is None
            and persisted_requested_model_tier is not None
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "requested_model_tier"},
            )

        if (
            restored_router is not None
            and persisted_runtime_backend != restored_router.runtime_backend
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with an inconsistent runtime backend contract",
                details={
                    "persisted_runtime_backend": restored_router.runtime_backend,
                    "execution_runtime_backend": persisted_runtime_backend,
                },
            )
        if is_legacy_quality_first_router(
            raw_routing, persisted_preferences.efficiency_mode.value, restored_router
        ):
            raise self._dependencies.error_type(
                message="Cannot resume a legacy quality-first routing contract",
                details={
                    "reason": "The saved child-routing behavior differs from the corrected quality-first policy",
                    "hint": "Start a successor or new run under the corrected policy.",
                },
            )
        if migrate_v2_contract or migrate_v3_contract:
            persisted_requested_model_tier = (
                restored_router.base_tier if restored_router is not None else None
            )
        replayed_economics = model_replay.replay_route_economics(
            self._state._route_economics.value,
            self._dependencies._configured_route_economics(),
            runtime_backend=runtime_backend,
            persisted_tiers=persisted_resolved.get(
                "tiers", restored_router.tier_models if restored_router is not None else {}
            ),
        )
        if (
            replayed_economics is not None
            and not self._dependencies._model_routing_override_explicit()
        ):
            self._state._route_economics.value = replayed_economics
        authoritative_router = self._dependencies._authoritative_model_router(
            persisted_preferences,
            requested_model_tier=persisted_requested_model_tier,
        )
        if (
            not self._dependencies._model_routing_override_explicit()
            and restored_router is not None
            and restored_router != authoritative_router
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with a changed model-routing policy",
                details={
                    "runtime_backend": persisted_runtime_backend,
                    "hint": "Restore the current route policy or start a new session.",
                },
            )
        raw_route_compat = raw_routing.get("route_compat")
        if migrate_v2_contract:
            # The v2 router has already been compared with the router rebuilt
            # from current config/backend/preferences. Its replacement v3
            # contract below adds the independently derived Route B projection.
            pass
        elif raw_route_compat is None:
            raise self._dependencies.error_type(
                message="Cannot resume without an explicit route compatibility contract",
                details={"invalid": "route_compat", "reason": "missing"},
            )
        else:
            from ouroboros.orchestrator.route_compat import (
                deserialize_route_compat_contract,
                validate_route_compat_projection,
            )

            route_compat_recognized, restored_projection = deserialize_route_compat_contract(
                raw_route_compat
            )
            if not route_compat_recognized:
                raise self._dependencies.error_type(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "route_compat"},
                )
            if restored_router is not None and restored_projection is None:
                raise self._dependencies.error_type(
                    message="Cannot resume without an enabled route compatibility contract",
                    details={"invalid": "route_compat", "reason": "dormant"},
                )
            if restored_router is None and restored_projection is not None:
                raise self._dependencies.error_type(
                    message="Cannot resume with an enabled route compatibility contract",
                    details={"invalid": "route_compat", "reason": "router_dormant"},
                )
            if (
                restored_projection is not None
                and not self._dependencies._model_routing_override_explicit()
                and not validate_route_compat_projection(
                    restored_projection,
                    self._state._route_economics.value,
                    model_router=authoritative_router,
                    runtime_backend=persisted_runtime_backend,
                    current_effort=self._dependencies._reasoning_effort(),
                )
            ):
                raise self._dependencies.error_type(
                    message="Cannot resume with a changed route compatibility catalog",
                    details={
                        "runtime_backend": persisted_runtime_backend,
                        "hint": "Restore the original route catalog or start a new session.",
                    },
                )
        if (
            not migrate_v2_contract
            and persisted_reasoning_effort != self._dependencies._reasoning_effort()
        ):
            raise self._dependencies.error_type(
                message="Cannot resume with a changed reasoning-effort contract",
                details={
                    "persisted_reasoning_effort": persisted_reasoning_effort,
                    "current_reasoning_effort": self._dependencies._reasoning_effort(),
                    "hint": "Restore the original reasoning effort or start a new session.",
                },
            )
        constructor_model_value = persisted_constructor_model.get("model")
        effective_model_observed = self._dependencies._runtime_execution_proves_effective_model(
            persisted_runtime_execution
        )
        process_local_authority = (
            prepared_live_execution
            and valid_process_local_authority_contract(raw_contract.get("foundation_a_authority"))
        )
        model_override_support = getattr(
            getattr(self._dependencies._adapter(), "capabilities", None),
            "model_override_support",
            ParamSupport.IGNORED,
        )
        automatic_codex_default_authorized = (
            self._dependencies._runtime_execution_authorizes_automatic_codex_resume(
                persisted_runtime_execution
            )
        )
        if (
            constructor_model_value is None
            and not effective_model_observed
            and not automatic_codex_default_authorized
            and not (restored_router is not None and model_override_support is ParamSupport.NATIVE)
            and not process_local_authority
        ):
            raise self._dependencies.error_type(
                message="Cannot resume because the effective runtime model is unverifiable",
                details={
                    "runtime_backend": persisted_runtime_backend,
                    "constructor_model": None,
                    "effective_model_observed": False,
                    "model_routing_enforced": (
                        restored_router is not None
                        and model_override_support is ParamSupport.NATIVE
                    ),
                    "automatic_codex_default_authorized": automatic_codex_default_authorized,
                    "hint": ("Pin the original runtime model/profile, or start a new session."),
                },
            )
        self._state._execution_preferences.value = persisted_preferences
        self._state._shadow_replay_enabled.value = (
            self._dependencies._resolved_shadow_replay_enabled()
        )
        persisted_verifier = (
            raw_execution_semantics.get("transcript_verifier")
            if isinstance(raw_execution_semantics, Mapping)
            else None
        )
        if persisted_verifier in ("decide", "record"):
            # The run's own sealed mode, never the live config: a run that
            # started deciding (every run sealed before the setting existed)
            # resumes deciding, one that started recording keeps recording.
            self._state._transcript_verifier.value = persisted_verifier
        current_execution_semantics = self._dependencies._execution_semantics_contract()
        if (
            not migrate_legacy_contract
            and dict(raw_execution_semantics) != current_execution_semantics
        ):
            runtime_capability_drift = raw_execution_semantics.get(
                "runtime_effect_capabilities"
            ) != current_execution_semantics.get("runtime_effect_capabilities")
            raise self._dependencies.error_type(
                message="Cannot resume with changed execution semantics",
                details={
                    "persisted_execution_semantics": dict(raw_execution_semantics),
                    "current_execution_semantics": current_execution_semantics,
                    "resume_blocked": (
                        "runtime_effect_capability_drift"
                        if runtime_capability_drift
                        else "execution_semantics_drift"
                    ),
                    "retryable": True,
                    "hint": (
                        "Restore the original verification, retry, decomposition, and "
                        "executor settings or start a new session."
                    ),
                },
            )
        if self._dependencies._model_routing_override_explicit():
            replacement = self._dependencies._build_execution_contract(
                seed=seed,
                seed_fingerprint=(persisted_seed_fingerprint if valid_seed_fingerprint else None),
                authority_generation=authority_generation,
                execution_inputs_contract=normalized_execution_inputs,
                project_identity=replacement_project_identity,
                runtime_handle=runtime_handle,
            )
            # Only the public resume path reaches this branch with a live,
            # registry-issued generation.  Preserve the persisted diagnostics
            # in direct contract-validation calls so those calls cannot mint a
            # replacement correlation id by accident.
            if authority_generation is None:
                replacement["foundation_a_authority"] = dict(raw_contract["foundation_a_authority"])
            self._state._execution_contract.value = replacement
            return self._state._execution_contract.value != raw_contract

        self._state._model_router.value = restored_router
        self._state._requested_model_tier.value = persisted_requested_model_tier
        if migrate_legacy_contract:
            replacement = self._dependencies._build_execution_contract(
                seed=seed,
                seed_fingerprint=(persisted_seed_fingerprint if valid_seed_fingerprint else None),
                authority_generation=authority_generation,
                project_identity=replacement_project_identity,
                runtime_handle=runtime_handle,
            )
            if authority_generation is None:
                replacement["foundation_a_authority"] = dict(raw_contract["foundation_a_authority"])
            self._state._execution_contract.value = replacement
            return True
        if migrate_preflight_contract:
            self._state._execution_contract.value = dict(raw_contract)
            return True
        # Preserve the exact persisted proof identity alongside the restored
        # router. Recomputing it from a resumed throwaway worktree would make the
        # same execution appear to be a different experiment.
        self._state._execution_contract.value = dict(raw_contract)
        if preferences_migrated or base_reasoning_effort_missing:
            self._state._execution_contract.value["execution_preferences"] = (
                persisted_preferences.to_contract_data()
            )
            migrated_routing = dict(raw_routing)
            if base_reasoning_effort_missing:
                migrated_routing["base_reasoning_effort"] = persisted_base_reasoning_effort
            self._state._execution_contract.value["model_routing"] = migrated_routing
            migrated_proof = dict(raw_proof)
            migrated_proof["routing_fingerprint"] = self._dependencies._routing_fingerprint(
                migrated_routing
            )
            self._state._execution_contract.value["frugality_proof"] = migrated_proof
            return True
        return False
