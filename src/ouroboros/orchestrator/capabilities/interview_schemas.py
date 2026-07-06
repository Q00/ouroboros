"""Interview-related capability JSON schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _builtin_semantics_for(tool_name: str):  # noqa: ANN202
    from ouroboros.orchestrator.capabilities import _BUILTIN_SEMANTICS

    return _BUILTIN_SEMANTICS[tool_name]


def _interview_code_investigation_request_schema() -> dict[str, Any]:
    """Return the runtime request model for interview code-fact investigation."""
    target_schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [
            {
                "title": "WorkspaceTarget",
                "additionalProperties": False,
                "required": ["target_type", "scope"],
                "properties": {
                    "target_type": {"const": "workspace"},
                    "scope": {
                        "type": "string",
                        "enum": ["active", "selected_repositories", "all_available"],
                    },
                },
            },
            {
                "title": "RelativePathTarget",
                "additionalProperties": False,
                "required": ["target_type", "path"],
                "properties": {
                    "target_type": {"const": "relative_path"},
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Repository-relative file or directory path.",
                    },
                },
            },
            {
                "title": "GlobTarget",
                "additionalProperties": False,
                "required": ["target_type", "pattern"],
                "properties": {
                    "target_type": {"const": "glob"},
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Repository-relative glob pattern.",
                    },
                },
            },
            {
                "title": "SymbolTarget",
                "additionalProperties": False,
                "required": ["target_type", "name"],
                "properties": {
                    "target_type": {"const": "symbol"},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Function, class, module, command, or config symbol to locate.",
                    },
                    "path_hint": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional repository-relative search hint.",
                    },
                },
            },
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "question",
            "investigation_goal",
            "investigation_targets",
            "fact_categories",
            "allowed_capabilities",
            "repo_inspection_tool_capabilities",
            "confidence_policy",
            "answer_prefixes",
            "answer_contract",
            "mcp_tool_capability",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": (
                    "Stable identity derived from the originating interview "
                    "question using stable_code_investigation_question_identity()."
                ),
            },
            "question": {
                "type": "string",
                "description": "The MCP-generated interview question requiring code facts.",
            },
            "last_question": {
                "type": "string",
                "description": "Previously asked question text, when available.",
            },
            "investigation_goal": {
                "type": "string",
                "enum": ["describe_current_state_from_code"],
                "description": "Code investigation is descriptive only; decisions route to the user.",
            },
            "investigation_targets": {
                "type": "array",
                "minItems": 1,
                "items": target_schema,
                "description": "Repository-agnostic descriptors for the code facts to inspect.",
            },
            "fact_categories": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": [
                        "tech_stack",
                        "frameworks",
                        "dependencies",
                        "current_patterns",
                        "architecture",
                        "file_structure",
                        "configuration",
                    ],
                },
            },
            "allowed_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": ["inspect_code"]},
                "description": "Runtime capability used for local code facts.",
            },
            "repo_inspection_tool_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": [
                        "tool_name",
                        "stable_id",
                        "source_kind",
                        "source_name",
                        "input_schema",
                        "mutation_class",
                        "parallel_safety",
                        "interruptibility",
                        "approval_class",
                        "origin",
                        "scope",
                        "execution_mode",
                        "logical_capability",
                        "side_effects",
                        "fallback_used",
                    ],
                    "properties": {
                        "tool_name": {"type": "string", "enum": ["Read", "Glob", "Grep"]},
                        "source_kind": {"const": "builtin"},
                        "execution_mode": {"const": "repo_inspection"},
                        "logical_capability": {"const": "inspect_code"},
                        "fallback_used": {"const": False},
                    },
                },
                "description": (
                    "Concrete runtime repo-inspection tools a code-fact "
                    "subagent can use to satisfy allowed_capabilities=inspect_code."
                ),
            },
            "confidence_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "auto_confirm_when",
                    "confirmation_required_when",
                    "human_judgment_when",
                ],
                "properties": {
                    "auto_confirm_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confirmation_required_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "human_judgment_when": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "answer_prefixes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": ["[from-code]", "[from-code][auto-confirmed]"],
                },
            },
            "answer_contract": {
                "const": _interview_code_investigation_answer_contract(),
                "description": "Exact response contract attached to this investigation request.",
            },
            "mcp_tool_capability": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "tool_name",
                    "stable_id",
                    "source_kind",
                    "source_name",
                    "input_schema",
                    "mutation_class",
                    "execution_mode",
                    "companions",
                    "required_context_keys",
                    "mutation_targets",
                    "state_mutations",
                    "side_effects",
                    "retry",
                    "interrupt",
                    "cancel",
                    "fallback_used",
                    "orchestration",
                ],
                "properties": {
                    "tool_name": {"const": "ouroboros_interview"},
                    "fallback_used": {"const": False},
                },
                "description": (
                    "Explicit Ouroboros-owned MCP capability metadata for the "
                    "tool that emitted this investigation request."
                ),
            },
        },
    }


def _interview_code_investigation_answer_contract() -> dict[str, Any]:
    """Return the answer contract for one code-fact investigation request."""
    answer_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "answer_prefix",
            "answer_text",
            "confidence",
            "evidence",
            "requires_user_confirmation",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": "Matches the originating code investigation request.",
            },
            "answer_prefix": {
                "type": "string",
                "enum": ["[from-code]", "[from-code][auto-confirmed]"],
                "description": "Prefix to prepend when forwarding the answer to interview MCP.",
            },
            "answer_text": {
                "type": "string",
                "minLength": 1,
                "description": "Concise descriptive fact answer without prescription.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high_exact_match", "medium_inferred", "low_uncertain"],
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "claim"],
                    "properties": {
                        "source": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Repository-relative file, symbol, or manifest source.",
                        },
                        "claim": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The factual claim supported by this evidence.",
                        },
                        "locator": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Optional line, key, dependency, or symbol locator.",
                        },
                    },
                },
            },
            "requires_user_confirmation": {
                "type": "boolean",
                "description": "True when the answer must be confirmed before forwarding.",
            },
            "user_confirmation_prompt": {
                "type": "string",
                "minLength": 1,
                "description": "Prompt text to show when confirmation is required.",
            },
        },
        "allOf": [
            {
                "if": {
                    "properties": {"answer_prefix": {"const": "[from-code][auto-confirmed]"}},
                    "required": ["answer_prefix"],
                },
                "then": {
                    "properties": {
                        "confidence": {"const": "high_exact_match"},
                        "requires_user_confirmation": {"const": False},
                    }
                },
            },
            {
                "if": {
                    "properties": {"requires_user_confirmation": {"const": True}},
                    "required": ["requires_user_confirmation"],
                },
                "then": {"required": ["user_confirmation_prompt"]},
            },
            {
                "if": {
                    "properties": {"answer_prefix": {"const": "[from-code]"}},
                    "required": ["answer_prefix"],
                },
                "then": {
                    "properties": {"requires_user_confirmation": {"const": True}},
                    "required": ["user_confirmation_prompt"],
                },
            },
        ],
    }
    return {
        "contract_id": "code_fact_investigation_answer.v1",
        "scope": "single_code_fact_investigation_request",
        "response_model_schema": answer_schema,
        "prefix_semantics": {
            "[from-code][auto-confirmed]": {
                "confidence": "high_exact_match",
                "requires_user_confirmation": False,
                "forwarding": "send_to_mcp_immediately",
            },
            "[from-code]": {
                "confidence": "medium_or_low",
                "requires_user_confirmation": True,
                "forwarding": "confirm_with_user_before_mcp",
            },
        },
        "evidence_policy": {
            "minimum_items": 1,
            "source_format": "repository_relative_path_or_symbol",
            "server_local_paths_allowed": False,
        },
        "runtime_instruction": (
            "Produce exactly one structured answer payload for the originating "
            "question_identity. Use [from-code][auto-confirmed] only for an "
            "unambiguous manifest/config exact match; otherwise require user "
            "confirmation and use [from-code] after confirmation."
        ),
    }


def interview_code_investigation_answer_contract() -> dict[str, Any]:
    """Return the public code-fact answer contract for generated requests."""
    return _interview_code_investigation_answer_contract()


def _code_investigation_repo_inspection_tool_capabilities() -> tuple[dict[str, Any], ...]:
    """Return concrete repo-inspection tool capabilities for code-fact subagents."""
    tool_schemas: Mapping[str, Mapping[str, Any]] = {
        "Read": {
            "type": "object",
            "additionalProperties": True,
            "required": ["file_path"],
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Repository-local file path to inspect.",
                },
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
        },
        "Glob": {
            "type": "object",
            "additionalProperties": True,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Repository-local glob pattern to enumerate.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional repository-local search root.",
                },
            },
        },
        "Grep": {
            "type": "object",
            "additionalProperties": True,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Search pattern for repository-local evidence.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional repository-local file or directory scope.",
                },
                "glob": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional file glob narrowing the search.",
                },
            },
        },
    }
    capabilities: list[dict[str, Any]] = []
    for tool_name in ("Read", "Glob", "Grep"):
        semantics = _builtin_semantics_for(tool_name)
        capabilities.append(
            {
                "tool_name": tool_name,
                "stable_id": f"builtin:{tool_name}",
                "source_kind": "builtin",
                "source_name": "built-in",
                "input_schema": dict(tool_schemas[tool_name]),
                "mutation_class": semantics.mutation_class.value,
                "parallel_safety": semantics.parallel_safety.value,
                "interruptibility": semantics.interruptibility.value,
                "approval_class": semantics.approval_class.value,
                "origin": semantics.origin.value,
                "scope": semantics.scope.value,
                "execution_mode": "repo_inspection",
                "logical_capability": "inspect_code",
                "side_effects": ["side_effect_free"],
                "fallback_used": False,
            }
        )
    return tuple(capabilities)


def _interview_question_advisory_request_schema() -> dict[str, Any]:
    """Return the runtime request model for per-question answer assistance."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "session_id",
            "question_identity",
            "question",
            "phase",
            "user_question_first",
            "advisory_goal",
            "parallel_preference",
            "sequential_fallback",
            "allowed_capabilities",
            "lanes",
            "synthesis_contract",
            "mcp_tool_capability",
        ],
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current Ouroboros interview session ID.",
            },
            "question_identity": {
                "type": "string",
                "pattern": r"^interview-question:[0-9a-f]{16}$",
                "description": (
                    "Stable identity derived from the originating interview "
                    "question using stable_code_investigation_question_identity()."
                ),
            },
            "question": {
                "type": "string",
                "minLength": 1,
                "description": "The already user-visible MCP interview question.",
            },
            "last_question": {
                "type": "string",
                "description": "Previously asked question text, when available.",
            },
            "phase": {
                "type": "string",
                "enum": ["start", "resume_pending", "answer"],
            },
            "ambiguity_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
            },
            "milestone": {
                "type": ["string", "null"],
                "enum": ["initial", "progress", "refined", "ready", None],
            },
            "user_question_first": {
                "const": True,
                "description": (
                    "The parent runtime must surface the interview question before "
                    "or while advisory fanout runs; advisory must never hide the "
                    "question behind background research."
                ),
            },
            "advisory_goal": {
                "const": "help_human_answer_interview_question",
                "description": (
                    "Generate concise answer options, uncertainty notes, and a "
                    "recommended draft without mutating interview state."
                ),
            },
            "parallel_preference": {
                "const": "parallel_when_runtime_supports_subagents",
            },
            "sequential_fallback": {
                "type": "object",
                "additionalProperties": False,
                "required": ["supported", "mode", "trigger"],
                "properties": {
                    "supported": {"const": True},
                    "mode": {"const": "sequential_advisory_lane_dispatch"},
                    "trigger": {"const": "runtime_has_no_native_parallel_subagent_primitive"},
                },
            },
            "allowed_capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": ["inspect_code", "web_research", "run_lateral_review"],
                },
            },
            "lanes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["lane_id", "purpose", "capability", "required"],
                    "properties": {
                        "lane_id": {
                            "type": "string",
                            "enum": [
                                "code_context",
                                "web_context",
                                "ambiguity_contrarian",
                                "answer_simplifier",
                                "architecture_implications",
                            ],
                        },
                        "purpose": {"type": "string", "minLength": 1},
                        "capability": {
                            "type": "string",
                            "enum": ["inspect_code", "web_research", "run_lateral_review"],
                        },
                        "persona": {
                            "type": "string",
                            "enum": ["researcher", "contrarian", "simplifier", "architect"],
                        },
                        "required": {"type": "boolean"},
                    },
                },
            },
            "code_investigation_request": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Optional code-fact request emitted alongside this advisory; "
                    "reuse it for the code_context lane when present."
                ),
            },
            "synthesis_contract": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "output_shape",
                    "max_options",
                    "include_recommended_draft",
                    "preserve_user_agency",
                    "forward_to_mcp_only_after_user_or_auto_confirm",
                ],
                "properties": {
                    "output_shape": {
                        "const": "answer_advisory",
                    },
                    "max_options": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                    },
                    "include_recommended_draft": {"type": "boolean"},
                    "preserve_user_agency": {"const": True},
                    "forward_to_mcp_only_after_user_or_auto_confirm": {"const": True},
                },
            },
            "mcp_tool_capability": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "tool_name",
                    "stable_id",
                    "source_kind",
                    "source_name",
                    "input_schema",
                    "mutation_class",
                    "execution_mode",
                    "companions",
                    "required_context_keys",
                    "mutation_targets",
                    "state_mutations",
                    "side_effects",
                    "retry",
                    "interrupt",
                    "cancel",
                    "fallback_used",
                    "orchestration",
                ],
                "properties": {
                    "tool_name": {"const": "ouroboros_interview"},
                    "fallback_used": {"const": False},
                },
            },
        },
    }


def _interview_question_advisory_fanout_metadata() -> dict[str, Any]:
    """Return structured metadata for parent-session interview answer help."""
    lanes = [
        {
            "lane_id": "code_context",
            "purpose": "Find repo-local facts that may answer or constrain the question.",
            "capability": "inspect_code",
            "required": False,
        },
        {
            "lane_id": "web_context",
            "purpose": (
                "Check current external facts only when the question depends on "
                "third-party APIs, pricing, standards, security, or recent changes."
            ),
            "capability": "web_research",
            "required": False,
        },
        {
            "lane_id": "ambiguity_contrarian",
            "purpose": "Name hidden assumptions, missing decisions, and risky vague words.",
            "capability": "run_lateral_review",
            "persona": "contrarian",
            "required": True,
        },
        {
            "lane_id": "answer_simplifier",
            "purpose": "Turn the question into easy choices or a concise answer draft.",
            "capability": "run_lateral_review",
            "persona": "simplifier",
            "required": True,
        },
        {
            "lane_id": "architecture_implications",
            "purpose": (
                "Check whether the answer would change system shape, ownership, "
                "interfaces, or rollout strategy."
            ),
            "capability": "run_lateral_review",
            "persona": "architect",
            "required": False,
        },
    ]
    return {
        "contract_id": "interview_question_advisory_fanout.v1",
        "mcp_tool": "ouroboros_interview",
        "companion_tool": "ouroboros_lateral_think",
        "dispatch_timing": "after_question_is_visible_to_user",
        "parallel_preference": "parallel_when_runtime_supports_subagents",
        "sequential_fallback": {
            "supported": True,
            "mode": "sequential_advisory_lane_dispatch",
            "trigger": "runtime_has_no_native_parallel_subagent_primitive",
        },
        "request_model_schema": _interview_question_advisory_request_schema(),
        "lanes": lanes,
        "synthesis_contract": {
            "output_shape": "answer_advisory",
            "max_options": 3,
            "include_recommended_draft": True,
            "preserve_user_agency": True,
            "forward_to_mcp_only_after_user_or_auto_confirm": True,
        },
        "response_payload_refs": {
            "plugin": "parent_runtime.ouroboros_dispatch.children",
            "result_correlation_key": "lane_id",
            "requires_prose_parsing": False,
            "synthesis_owner": "parent_session",
        },
        "runtime_instruction": (
            "Show the MCP interview question to the user first, then fan out "
            "advisory lanes for code context, current web facts when needed, "
            "ambiguity critique, simplification, and architecture implications. "
            "Read child task results as they complete and synthesize them into "
            "two or three answer options or one recommended draft. Do not forward advisory text to "
            "ouroboros_interview until the user approves, edits, or explicitly "
            "chooses auto-confirm."
        ),
    }


__all__ = [
    "_code_investigation_repo_inspection_tool_capabilities",
    "_interview_code_investigation_answer_contract",
    "_interview_code_investigation_request_schema",
    "_interview_question_advisory_fanout_metadata",
    "_interview_question_advisory_request_schema",
    "interview_code_investigation_answer_contract",
]
