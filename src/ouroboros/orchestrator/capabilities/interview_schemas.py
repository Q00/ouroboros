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


#: Aggregations a read request may ask for.  The list is closed on purpose: an
#: aggregate is the only answer shape this lane can carry, so a lookup that
#: cannot be phrased as one has no way to be requested and becomes a no-op
#: finding instead.  See ``_interview_data_evidence_answer_contract``.
#:
#: Every member here names a complete operation.  ``percentile`` did not -- it
#: needed a rank the request had no field for, so ``percentile`` of latency was
#: a request the user could approve and the parent still had to finish, picking
#: between p50 and p99 after the approval that was supposed to cover it.  The
#: ranks are members instead of a parameter, which keeps the ambiguity out
#: without adding a conditional field that only some aggregations use
#: (Q00/ouroboros#1754).
DATA_AGGREGATIONS: tuple[str, ...] = (
    "count",
    "distinct_count",
    "sum",
    "average",
    "median",
    "p90",
    "p95",
    "p99",
    "min",
    "max",
    "rate",
)


#: Why a data-driven read is not being proposed.  A closed set, because the
#: reasons are known in advance -- and because a free-text reason is a channel
#: for the observation this lane must not carry.  A child that ran a lookup and
#: wanted to report "observed 41 accounts" had a sentence-shaped field to put it
#: in; now it has a choice of four constants (Q00/ouroboros#1754).
DATA_NO_EVIDENCE_REASONS: tuple[str, ...] = (
    "not_a_measurement",
    "no_data_tool_available",
    "answer_would_not_be_an_aggregate",
    "question_too_ambiguous_to_measure",
)

#: Names of things in the data source -- a tool, a column, a grouping key.  They
#: are identifiers, so they are constrained like identifiers: no whitespace, no
#: quotes, no parentheses.  A query cannot be spelled in a field shaped this way,
#: which is cheaper than looking for one in a field that has no shape.
#:
#: What this costs, stated rather than discovered later: a source whose column
#: names contain spaces has to be named by its physical identifier here.  That
#: is a real restriction, and it is the one taken deliberately -- allowing
#: whitespace back is what makes a sentence, and a query, spellable again.
_DATA_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:\-]{1,128}$"


def _interview_data_read_request_schema() -> dict[str, Any]:
    """Return the schema for one proposed, unexecuted data read.

    The request names *what to measure* rather than how to fetch it: there is no
    query string here, and the confirmation surface renders these fields so the
    user sees the measurement before approving it rather than a dialect they
    would have to read.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "operation",
            "tool_name",
            "metric",
            "aggregation",
            "informs_decision",
        ],
        "properties": {
            "operation": {
                "const": "read",
                "description": (
                    "The only operation this lane can express. A write, a "
                    "migration, or a schema change has no representation here."
                ),
            },
            "tool_name": {
                "type": "string",
                "pattern": _DATA_IDENTIFIER_PATTERN,
                "description": "Host tool the parent session would run this read through.",
            },
            "metric": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "What is being measured, in the source's own vocabulary.",
            },
            "aggregation": {"type": "string", "enum": list(DATA_AGGREGATIONS)},
            "group_by": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "string",
                    "pattern": _DATA_IDENTIFIER_PATTERN,
                    "description": (
                        "Categorical key only. Grouping by an identifier turns "
                        "an aggregate back into a row list, which this lane "
                        "cannot carry."
                    ),
                },
            },
            "filters": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "comparator", "value"],
                    "properties": {
                        "field": {"type": "string", "pattern": _DATA_IDENTIFIER_PATTERN},
                        "comparator": {
                            "type": "string",
                            # Scalar comparators only, because `value` is one
                            # value. `between` and `in` each took two or more
                            # operands and were handed a single string to pack
                            # them into, so `"2026-01-01..2026-03-31"` was a
                            # filter the user approved and the parent still had
                            # to parse. A range is two filters here -- `gte`
                            # and `lte` -- which says the same thing and leaves
                            # nothing to interpret. A set has no unambiguous
                            # form in this slice: group by the field instead
                            # and let the user read the categories.
                            "enum": ["eq", "neq", "gt", "gte", "lt", "lte"],
                        },
                        "value": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                },
            },
            "time_window": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "Period the measurement covers, e.g. 'last 90 days'.",
            },
            "informs_decision": {
                "type": "string",
                "minLength": 1,
                "maxLength": 400,
                "description": (
                    "The interview decision this number would inform. A read "
                    "that cannot name one is not worth the user's confirmation."
                ),
            },
        },
    }


def _interview_data_evidence_answer_contract() -> dict[str, Any]:
    """Return the answer contract for the ``data_context`` advisory lane.

    Four properties this schema holds by shape rather than by rule.

    **The child does not classify the tool it names.** There is no
    ``source_class`` here. Whoever knows a tool is who classifies it, and that
    is the rule the sibling lanes already follow: ``code_context`` runs on
    ``Read`` / ``Glob`` / ``Grep``, which the server hands the child *with*
    their ``mutation_class`` read out of ``_BUILTIN_SEMANTICS``, so the child
    picks from a list rather than rating anything; ``web_context`` names no tool
    at all. Only ``data_context`` reaches tools the server has never seen --
    a host's warehouse, its Metabase, its analytics MCP -- which is exactly why
    this lane proposes and the host executes.

    A child-asserted class had the untrusted party rating the risk of a tool it
    knows only by name, and the host was told to warn about cost from that
    rating. ``operation: "read"`` is a label on the request, not a constraint on
    the tool, so a metered warehouse call could arrive labelled a free local
    read and the warning would simply not be given. RFC #1754 already ruled on
    this: "a tool's name cannot prove it is read-only [...] a child judging
    'obviously local, free, read-only' from names and descriptions is not a
    boundary."

    Removing the field does not move the rating to the host, because there is
    nowhere to move it to: MCP carries no cost or mutation metadata, so a host
    told to disclose one would be manufacturing it, and a disclaimer on every
    read is a thing users learn to click through. What is left is what was
    load-bearing all along -- the user installed these tools, the whole request
    including the tool name is rendered, and nothing runs until they confirm
    that specific request. ``skills/interview/SKILL.md`` carries that duty.

    **A measurement cannot be reported as a measurement.** There is no field for
    an observed value, a row, or a timestamp of observation -- only proposals --
    so a child that ran a lookup has no way to hand the result back *as a
    result*, and no consumer can read one out of this shape.

    Say that precisely, because the stronger sentence is not true. Every field
    that had a shape now has one: ``no_evidence_reason`` is a choice from
    ``DATA_NO_EVIDENCE_REASONS`` rather than a sentence, and ``tool_name``,
    ``group_by`` and a filter's ``field`` are identifiers, so a query cannot be
    spelled in them. What is left free is ``metric``, ``informs_decision`` and a
    filter's ``value`` -- the fields whose entire purpose is to be read by the
    user before they approve the read. Any string a human must read can contain
    a number, and looking for one is the search that ten rounds of #1703 showed
    does not converge; a ``caveats`` array was removed rather than watched for
    exactly this reason.

    So the boundary is not that prose cannot mention a number. It is that
    nothing here becomes an interview answer, a requirement, or durable state:
    the user answers in their own words, ``[from-data]`` is withheld from
    extraction, and the record keeps no child-authored content
    (Q00/ouroboros#1754).

    **An answer is one of two states, and cannot be between them.** The schema
    is a ``oneOf`` over two closed objects rather than one object with
    conditions: ``NoMeasurementNeeded`` carries a reason and no reads,
    ``MeasurementProposed`` carries reads and has no field for a reason.

    It was the other shape first, and that shape leaked. One object with
    ``if``/``then`` pairs constrains only the fields each pair names, so a field
    belonging to one state stayed spellable in the other until someone forbade
    it by name -- and an answer could say ``data_needed: true`` with a concrete
    read *and* ``no_evidence_reason``, which is "this question is measurable"
    and "this question is not a measurement" in one payload. Nothing downstream
    resolves that; the host renders a proposal the same answer disowned. The
    fix is not to forbid that pair but to stop having a place where a pair from
    two states can meet, so the next field added to one state cannot leak into
    the other by nobody remembering to exclude it.

    **A no-op is an answer, not an absence.** ``NoMeasurementNeeded`` is a
    complete, valid response. The lane is ``required: true`` precisely because
    this response always exists, so a question that is not data-driven
    completes the fan-out rather than stalling it.

    **The answer names the question it was drafted for, and nothing else.**
    There is no ``session_id`` here. The session is settled by the submission
    envelope, which rejects a fan-out submitted under a different session before
    any content is examined; a session the child asserts about itself restates
    that check in the weaker of the two directions, since the assertion and the
    envelope arrive in the same call and only one of them was written by the
    producer. The sibling ``code_investigation`` contract carries a session
    because its output becomes a ``[from-code]`` interview answer; this lane's
    output is a proposal shown beside the question, so ``question_identity`` is
    the whole of what has to match (Q00/ouroboros#1754).
    """
    identity_property: dict[str, Any] = {
        "type": "string",
        "pattern": r"^interview-question:[0-9a-f]{16}$",
        "description": "Matches the originating advisory request.",
    }
    no_op_state: dict[str, Any] = {
        "title": "NoMeasurementNeeded",
        "type": "object",
        "additionalProperties": False,
        "required": ["question_identity", "lane_id", "data_needed", "no_evidence_reason"],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "data_context"},
            "data_needed": {
                "const": False,
                "description": (
                    "The honest answer to this question is not a measurement. "
                    "Decided from the question text before any tool call."
                ),
            },
            "read_requests": {"type": "array", "maxItems": 0},
            "no_evidence_reason": {
                "type": "string",
                "enum": list(DATA_NO_EVIDENCE_REASONS),
                "description": (
                    "Why no read is proposed. Chosen from a closed set: the "
                    "reasons this lane can have are known in advance, so this "
                    "is a choice rather than a sentence."
                ),
            },
        },
    }
    proposal_state: dict[str, Any] = {
        "title": "MeasurementProposed",
        "type": "object",
        "additionalProperties": False,
        "required": ["question_identity", "lane_id", "data_needed", "read_requests"],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "data_context"},
            "data_needed": {
                "const": True,
                "description": "The honest answer to this question is a measurement.",
            },
            "read_requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": _interview_data_read_request_schema(),
            },
        },
    }
    answer_schema: dict[str, Any] = {"oneOf": [no_op_state, proposal_state]}
    return {
        "contract_id": "data_evidence_answer.v1",
        "scope": "single_interview_question_data_evidence",
        # Exactly two things, and deliberately no third. The schema is what the
        # child must satisfy and what re-entry enforces; the instruction is what
        # the child must do. A claim ABOUT the system -- an ``evidence_policy``
        # or an ``execution_semantics`` block -- reads as authoritative as
        # either, is enforced by nothing, and is addressed to a reader who
        # cannot act on it. Three of this PR's findings were guarantees stated
        # somewhere nothing made them true, so the field where a fourth would
        # live is not kept (Q00/ouroboros#1825).
        #
        # Where the deleted claims went: ``aggregates_only`` was a restatement
        # of the schema, which already admits only an aggregation and holds no
        # field for a value. Categorical grouping and "a row list is not
        # evidence" are undecidable over an open value space -- the class that
        # cost #1703 ten rounds -- so they are instruction, and say so below.
        # ``runs_after`` / ``run_by`` are host duties and live in
        # skills/interview/SKILL.md, which is the document the host reads.
        "response_model_schema": answer_schema,
        "runtime_instruction": (
            "Find out which data tools this host exposes before you judge, then "
            "decide whether the question's honest answer is a measurement you "
            "could reach through them. The order is load-bearing: measurability "
            "is a property of the question and the environment together, and "
            "no_data_tool_available is a fact about the host that the question's "
            "wording cannot establish. If the answer is not a measurement, or "
            "nothing available can measure it, return data_needed=false with "
            "whichever reason is true and stop -- that is a complete answer. If "
            "it is reachable, name the reads you "
            "would run and return them as read_requests. Do not run them: the "
            "parent session runs a read only after the user confirms it, and "
            "there is no field in this contract for a value you fetched. "
            "Only aggregates can be carried: group by categories, never by an "
            "identifier, and when the honest answer would be a row list, a name, "
            "an identifier, or an error message, that is data_needed=false with "
            "a reason rather than evidence. "
            "Whatever the numbers show, the interview answer is the user's own "
            "words, never yours."
        ),
    }


def interview_data_evidence_answer_contract() -> dict[str, Any]:
    """Return the public ``data_context`` answer contract."""
    return _interview_data_evidence_answer_contract()


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
            "contract_id",
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
            "contract_id": {
                "const": "interview_question_advisory_fanout.v1",
                "description": "Versioned wire contract for this advisory request.",
            },
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
                    "enum": ["inspect_code", "web_research", "run_lateral_review", "read_data"],
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
                                "data_context",
                                "ambiguity_contrarian",
                                "answer_simplifier",
                                "architecture_implications",
                            ],
                        },
                        "purpose": {"type": "string", "minLength": 1},
                        "capability": {
                            "type": "string",
                            "enum": [
                                "inspect_code",
                                "web_research",
                                "run_lateral_review",
                                "read_data",
                            ],
                        },
                        "persona": {
                            "type": "string",
                            "enum": ["researcher", "contrarian", "simplifier", "architect"],
                        },
                        "required": {"type": "boolean"},
                        "answer_contract": {
                            "type": "object",
                            "additionalProperties": True,
                            "description": (
                                "Versioned response contract this lane's output "
                                "is validated against at re-entry. A lane "
                                "without one completes on the generic advisory "
                                "shape."
                            ),
                        },
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
            "lane_id": "data_context",
            "purpose": (
                "Propose the measurements that would inform this question, so "
                "the user judges against numbers instead of memory."
            ),
            "capability": "read_data",
            # Required because its no-op answer always exists: a question that is
            # not data-driven still completes this lane. Optional would let a
            # data-driven question lose its evidence silently, which is the
            # defect the lane exists to remove (#1754).
            "required": True,
            "answer_contract": _interview_data_evidence_answer_contract(),
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
            "proposed measurements, ambiguity critique, simplification, and "
            "architecture implications. "
            "Read child task results as they complete and synthesize them into "
            "two or three answer options or one recommended draft. Do not forward advisory text to "
            "ouroboros_interview until the user approves, edits, or explicitly "
            "chooses auto-confirm."
        ),
    }


__all__ = [
    "DATA_AGGREGATIONS",
    "DATA_NO_EVIDENCE_REASONS",
    "_code_investigation_repo_inspection_tool_capabilities",
    "_interview_code_investigation_answer_contract",
    "_interview_code_investigation_request_schema",
    "_interview_data_evidence_answer_contract",
    "_interview_data_read_request_schema",
    "_interview_question_advisory_fanout_metadata",
    "_interview_question_advisory_request_schema",
    "interview_code_investigation_answer_contract",
    "interview_data_evidence_answer_contract",
]
