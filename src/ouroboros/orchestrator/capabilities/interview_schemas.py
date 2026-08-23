"""Interview-related capability JSON schemas."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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


#: Aggregations whose result is a tally.  A count of things cannot be negative
#: and cannot be fractional, so a schema that admits ``-1`` or ``1.5`` under one
#: of these is admitting a number no read produced -- and this lane's whole
#: output is numbers shown to a user as measured fact.
#:
#: Kept as a set rather than folded into a per-aggregation value schema: the
#: distinction is between "counts things" and "computes over them", and only the
#: first constrains the result's shape.  ``sum`` is deliberately absent -- a sum
#: over signed quantities is signed, and a sum over fractions is fractional.
DATA_COUNTING_AGGREGATIONS: frozenset[str] = frozenset({"count", "distinct_count"})

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


#: Why no measurement is carried.  A closed set, because the reasons are known
#: in advance -- and because a free-text reason is a channel for the observation
#: this lane must not carry.  A child that ran a lookup and wanted to report
#: "observed 41 accounts" had a sentence-shaped field to put it in; now it has a
#: choice of constants (Q00/ouroboros#1754).
#:
#: **Every reason is about the lane, never about the host.**  ``no_data_tool_``
#: ``available`` used to be here and was removed (Q00/ouroboros#1825): it is a
#: claim about the user's infrastructure, and a subagent is not positioned to
#: establish one.  It was reported for a question about EKS cost while five
#: observability MCP servers were connected and a Mimir store holding exactly
#: the container CPU/memory series sat described in the lane's own context --
#: the lane had no loadable tool schemas, read that as "no data path exists",
#: and the parent relayed it to the user as an actionable fact about their
#: infrastructure.  It was false, and it suppressed the only evidence that could
#: have bounded the answer.
#:
#: The two replacements split the claim by what the lane can decide for itself.
#: ``no_data_store_described`` says nothing in the descriptions it was given
#: holds this answer.  ``store_described_but_not_callable`` says one does and the
#: lane could not reach it -- which reaches the parent as "handle this", not as
#: "nothing exists", because the parent is the party that sees the environment.
DATA_NO_EVIDENCE_REASONS: tuple[str, ...] = (
    "not_a_measurement",
    "answer_would_not_be_an_aggregate",
    "question_too_ambiguous_to_measure",
    "no_data_store_described",
    "store_described_but_not_callable",
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

#: How many grouped numbers one measurement may carry. Even one grouping key can
#: produce arbitrarily many groups, and "an aggregate" stops being one somewhere
#: before the row count. The bound is where that line is drawn -- and it is
#: named in the field's description, because a limit the child cannot see is one
#: it discovers by being rejected for it.
_DATA_VALUES_MAX_ITEMS = 20


def _interview_data_read_request_schema() -> dict[str, Any]:
    """Return the schema for one measurement this lane took.

    The read still names *what was measured* rather than how it was fetched:
    there is no query string here, and the surface that shows the number beside
    the question renders these fields, so the user reads the measurement rather
    than a dialect they would have to interpret.

    ``values`` is what changed when the lane was allowed to execute
    (Q00/ouroboros#1825). Before, the absence of a value field was the
    barrier: a child that ran a lookup had nowhere to put the result. That
    barrier is retired because it was aimed at the wrong thing -- what #1754 set
    out to stop was a guess becoming the Seed's evidence, and execution was the
    heavy instrument reached for to get it. The guard that actually holds is
    downstream and unchanged: nothing here becomes an interview answer, a
    requirement, or durable state.

    There is no ``observed_at``. It was required here for two rounds and is
    removed by RFC #1754's second revision: ageing is accepted unconditionally,
    so a field restating it bought nothing a consumer read. The envelope that
    reasoning named was the interview session; RFC #2153 supersedes that with
    recency, on the reasoning that a measurement moves on the system's clock
    rather than on the clock of whoever is being interviewed. What is unchanged
    is why no field is needed: the aggregate is shown beside the question and
    the user answers in their own words, so its age is theirs to weigh. It also asked an LLM child with
    no clock to testify about time, which cost three rounds of validators —
    digits, then component ranges, then a wall clock with a skew allowance. The
    close is structural rather than another validator: a field that does not
    exist cannot carry an impossible time.

    **A grouped value carries its label, or the read is not grouped.** This is a
    ``oneOf`` over two closed shapes for the same reason the answer itself is:
    one object with an optional ``group`` accepted ``group_by=["region","plan"]``
    beside ``values=[{"value": 41}]`` -- every category lost, and two different
    aggregates rendered identically -- and equally accepted a label on a read
    that declared no grouping at all. A field that is optional where it is
    required is a field that is not required.

    ``group_by`` takes one key rather than four. Four keys were never
    representable: a single ``group`` string cannot say which key it labels, so
    the extra capacity only bought ways to be ambiguous. One key with a required
    label is what this shape can carry honestly, and a question needing two is a
    second read.
    """
    common: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "operation",
            "tool_name",
            "metric",
            "aggregation",
            "informs_decision",
            "values",
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
                "description": "Host tool you ran this read through.",
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
                "minItems": 1,
                "maxItems": 1,
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
                    "The interview decision this number informs. A read that "
                    "cannot name one is not worth the user's attention."
                ),
            },
            "values": {
                "type": "array",
                "maxItems": _DATA_VALUES_MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {
                        "group": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                            "description": (
                                "Category this number belongs to, one per "
                                "group_by key. Omit when the read is ungrouped."
                            ),
                        },
                        "value": {
                            "type": "number",
                            "description": (
                                "The aggregate. Only a number fits: a row, a "
                                "name, or an error is a NoMeasurementNeeded "
                                "answer instead."
                            ),
                        },
                    },
                },
                "description": (
                    f"What the aggregation returned, at most "
                    f"{_DATA_VALUES_MAX_ITEMS} entries. Empty means the read "
                    "ran and came back with nothing — that is a measurement, "
                    "and often the informative one. If more groups than that "
                    "came back, narrow the read rather than truncating it."
                ),
            },
        },
    }

    # A tally cannot be negative or fractional, and `value` alone cannot say so
    # because the constraint lives between two fields. Expressed as if/then
    # rather than as more `oneOf` branches: the criterion that made the answer
    # two closed states was about a *field of one state staying spellable in the
    # other*, and there is no field here to leak -- only a numeric range on a
    # field both branches already require. Four branches would also double a
    # contract the child must read whole.
    common["allOf"] = [
        {
            "if": {
                "properties": {"aggregation": {"enum": sorted(DATA_COUNTING_AGGREGATIONS)}},
                "required": ["aggregation"],
            },
            "then": {
                "properties": {
                    "values": {
                        "items": {
                            "properties": {
                                "value": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": (
                                        "A tally: whole and not negative. "
                                        "count and distinct_count cannot "
                                        "produce anything else."
                                    ),
                                }
                            }
                        }
                    }
                }
            },
        }
    ]

    ungrouped = deepcopy(common)
    ungrouped["title"] = "UngroupedMeasurement"
    ungrouped["properties"].pop("group_by")
    ungrouped["properties"]["values"]["items"]["properties"].pop("group")
    # One aggregate, because that is what an ungrouped read returns. Several
    # unlabelled numbers are a row list with no way to tell them apart -- the
    # thing categorical grouping exists to prevent, arriving through the door
    # where there is no grouping at all. Zero stays legal: a read that came back
    # empty is still a measurement.
    ungrouped["properties"]["values"]["maxItems"] = 1

    grouped = deepcopy(common)
    grouped["title"] = "GroupedMeasurement"
    grouped["required"] = [*common["required"], "group_by"]
    grouped["properties"]["values"]["items"]["required"] = ["group", "value"]

    return {"oneOf": [ungrouped, grouped]}


def _interview_data_evidence_answer_contract() -> dict[str, Any]:
    """Return the answer contract for the ``data_context`` advisory lane.

    Four properties this schema holds by shape rather than by rule.

    **The child does not classify the tool it names, and now runs it anyway.**
    There is no ``source_class`` here, and the reason is unchanged: whoever
    knows a tool is who classifies it. That is the rule the sibling lanes
    follow -- ``code_context`` runs on ``Read`` / ``Glob`` / ``Grep``, handed to
    the child *with* their ``mutation_class`` read out of ``_BUILTIN_SEMANTICS``
    so it picks from a list rather than rating anything, and ``web_context``
    names no tool at all. Only ``data_context`` reaches tools the server has
    never seen: a host's warehouse, its Metabase, its analytics MCP.

    That asymmetry used to be the argument for this lane proposing while the
    host executed. It no longer is (Q00/ouroboros#1825). ``operation: "read"``
    is still a label on the request rather than a constraint on the tool, and
    RFC #1754's finding still holds -- "a tool's name cannot prove it is
    read-only [...] a child judging 'obviously local, free, read-only' from
    names and descriptions is not a boundary." What changed is what follows
    from it. The confirmation gate it justified could not price what it was
    gating: MCP carries no cost or mutation metadata, so the host had nothing
    true to disclose, and a disclaimer attached to every read is a thing users
    learn to click through. It bought a round trip and a worse answer.

    So the risk is accepted rather than mitigated by ceremony, and stated here
    so it is accepted knowingly: a metered warehouse call or a write can arrive
    named like a free local read, and this lane will make it. The standing
    consent is the same one every other advisory lane already runs on -- the
    user registered the MCP server, and registering it is the willingness to
    have it called. What made this lane different was never the tools; it was
    that this lane alone was asked to hold the line in prose.

    **A measurement can be reported, and still cannot become the answer.**
    ``values`` exists now. A child that ran a lookup hands the aggregate back.

    The shape still refuses everything that is not an aggregate. ``value`` is a
    number, so a row, a name, an identifier, or an error message has no
    representation and a result that is one of those is a
    ``NoMeasurementNeeded`` answer instead. ``group_by`` keys are identifiers
    and ``values`` is bounded, because grouped numbers without a bound are a row
    list wearing an aggregate's name.

    ``values`` may be empty, and that is not a missing answer. A read that ran
    and came back with nothing is a measurement, frequently the informative one
    -- the observation that moved this decision was "zero cost-series metrics
    exist", which is exactly this shape. A minimum of one would have made the
    finding unspellable, and left the child nowhere to go: no
    ``no_evidence_reason`` means "I measured and it was empty", so it would have
    had to pick a reason that was false. An empty ``values`` is the read
    attesting that it ran and came back with nothing, which an absent answer
    cannot say.

    Every bound here is named in the field's own description for the same
    reason. This lane is ``required: true`` and a contract violation is popped
    from the aggregation, so a limit the child cannot see is not a limit -- it
    is a stall the user experiences as the lane having nothing to say.

    What the missing value field was protecting is not the schema, and never
    was. RFC #1754 opens on it: "an interview question whose honest answer is a
    number got a guess, and the guess became the Seed's evidence." The guard
    against that is downstream and untouched -- the user answers in their own
    words, there is no ``[from-data]`` answer path, ``[from-data]`` is withheld
    from requirement extraction on every generation path, and the record keeps
    no child-authored content. A number reaches the user's judgment and stops
    there. Withholding the number too was the heavier instrument, and it made
    the guess more likely rather than less.

    The free-text fields keep the property they had: ``metric``,
    ``informs_decision`` and a filter's ``value`` are read by a human, so they
    can contain a number, and looking for one is the search ten rounds of #1703
    showed does not converge -- a ``caveats`` array was removed rather than
    watched for exactly this reason. That was never the boundary. The boundary
    is that nothing here becomes an interview answer, a requirement, or durable
    state.

    **An answer is one of two states, and cannot be between them.** The schema
    is a ``oneOf`` over two closed objects rather than one object with
    conditions: ``NoMeasurementNeeded`` carries a reason and no measurements,
    ``MeasurementTaken`` carries measurements and has no field for a reason.

    It was the other shape first, and that shape leaked. One object with
    ``if``/``then`` pairs constrains only the fields each pair names, so a field
    belonging to one state stayed spellable in the other until someone forbade
    it by name -- and an answer could say ``data_needed: true`` with a concrete
    read *and* ``no_evidence_reason``, which is "this question is measurable"
    and "this question is not a measurement" in one payload. Nothing downstream
    resolves that; the host renders a measurement the same answer disowned. The
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
    output is a measurement shown beside the question, so ``question_identity``
    is
    the whole of what has to match (Q00/ouroboros#1754).
    """
    # Both question namespaces, because this contract is reused unchanged by the
    # PM interview (RFC #1937) and a pattern naming one tool would reject the
    # other's identities outright -- a lane rejected for its host's prefix has
    # nothing it could have submitted instead.
    #
    # Widening the shape does not widen the binding. Shape is not provenance:
    # ``_provenance_violations`` compares this value against the identity the
    # producer wrote into the fan-out record, so an answer still belongs to
    # exactly one question. What the pattern rules out is a free-text field, and
    # it goes on doing that.
    identity_property: dict[str, Any] = {
        "type": "string",
        "pattern": r"^(interview|pm)-question:[0-9a-f]{16}$",
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
                    "No measurement is carried. Either the question's honest "
                    "answer is not one, or nothing reachable here can take it "
                    "-- which is why the reason is a separate field, and why "
                    "the lane looks at what this host exposes before deciding."
                ),
            },
            "read_requests": {"type": "array", "maxItems": 0},
            "no_evidence_reason": {
                "type": "string",
                "enum": list(DATA_NO_EVIDENCE_REASONS),
                "description": (
                    "Why no measurement is carried. Chosen from a closed set: "
                    "the reasons this lane can have are known in advance, so "
                    "this is a choice rather than a sentence. Each one is about "
                    "you, never about the host."
                ),
            },
        },
    }
    measured_state: dict[str, Any] = {
        "title": "MeasurementTaken",
        "type": "object",
        "additionalProperties": False,
        "required": ["question_identity", "lane_id", "data_needed", "read_requests"],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "data_context"},
            "data_needed": {
                "const": True,
                "description": (
                    "The honest answer to this question is a measurement, and it was taken."
                ),
            },
            "read_requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": _interview_data_read_request_schema(),
            },
        },
    }
    answer_schema: dict[str, Any] = {"oneOf": [no_op_state, measured_state]}
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
            "Read what data stores are described to you before you judge, then "
            "decide whether the question's honest answer is a measurement one "
            "of them holds. The order is load-bearing: measurability is a "
            "property of the question and the environment together. "
            "Every reason here is about you, not about this host -- you see "
            "what reached you, not what is connected. If a store is described "
            "and you could not reach it, that is "
            "store_described_but_not_callable and only after an attempted call: "
            "an empty tool search is not evidence of absence. If nothing "
            "described holds the answer, that is no_data_store_described. "
            "Return data_needed=false with whichever reason is true and stop -- "
            "that is a complete answer. "
            "If it is reachable, take the measurement and return it: describe "
            "what you measured and carry the aggregate in values. "
            "Only aggregates can be carried: group by categories, never by an "
            "identifier, and when the honest answer would be a row list, a name, "
            "an identifier, or an error message, that is data_needed=false with "
            "a reason rather than evidence. "
            "Whatever the numbers show, the interview answer is the user's own "
            "words, never yours. Your numbers are material for their judgment "
            "and stop there -- that is the whole of the boundary now, so do not "
            "phrase a finding as the answer."
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
            "recent_findings": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    lane: {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["contract_id", "lane_id", "published_at"],
                            "properties": {
                                "contract_id": {"type": "string", "minLength": 1},
                                # const, not just string: an entry under one
                                # lane key naming a sibling lane would offer
                                # that sibling's output, so the pairing is made
                                # unrepresentable rather than trusted.
                                "lane_id": {"const": lane},
                                "published_at": {"type": "string", "minLength": 1},
                            },
                        },
                    }
                    for lane in ("code_context", "data_context")
                },
                "description": (
                    "Where this project's recent findings are, keyed by the lane "
                    "that produced them: a lane is offered only its own, and the "
                    "reasoning lanes are absent because a lane that produces no "
                    "fact that keeps consumes none either (RFC "
                    "Q00/ouroboros#2167). Each entry is a contract_id and the "
                    "lane_id that narrows it, both passed to "
                    "ouroboros_fetch_artifact, and when it was published. "
                    "Bodies do not travel: carried inline they were duplicated "
                    "into every lane of the turn, which outgrew what a host "
                    "accepts inline and cost the turn its fan-out. A lane with "
                    "none is absent, as is the whole field when the project has "
                    "published nothing recent."
                ),
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
                "Take the measurements that inform this question, so the "
                "user judges against numbers instead of memory."
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
        # What the fan-out builder used to hardcode for its only caller. These
        # move here so one builder serves every tool without either one's
        # emitted payloads or request changing by a byte.
        "question_identity_prefix": "interview-question",
        # This tool measures ambiguity, so its request always carries the score
        # and milestone -- ``None`` included, because "not scored yet" is a
        # state of a thing that exists. A tool with no such concept omits the
        # keys rather than reporting them empty.
        "scores_ambiguity": True,
        "advisory_goal": "help_human_answer_interview_question",
        "payload_title_prefix": "Interview advisory",
        "allowed_capabilities": [
            "inspect_code",
            "web_research",
            "run_lateral_review",
            "read_data",
        ],
        "question_heading": "## Interview Question",
        # Carried whole rather than composed from parts: this paragraph states
        # what a child may do with a clear finding, which is the one thing the
        # two tools genuinely disagree about, and assembling it from fragments
        # would put that disagreement in the builder instead of the catalog.
        "task_preamble": (
            "You are an Ouroboros interview advisory subagent.\n"
            "\n"
            "The parent session has already shown the interview question to the "
            "user. Your job\nis to help the user answer it; do not answer on "
            "behalf of the user unless the\nanswer is a descriptive fact with "
            "clear evidence."
        ),
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
            # The dotted path, because that is what re-entry compares against.
            # This read ``lane_id`` while the producer stamped and the registry
            # registered ``context.lane_id``, so a host following the advertised
            # contract was refused with ``correlation_mismatch`` — the sibling
            # panel advertises ``context.persona`` for the same reason.
            "result_correlation_key": "context.lane_id",
            "requires_prose_parsing": False,
            "synthesis_owner": "parent_session",
        },
        "runtime_instruction": (
            "Show the MCP interview question to the user first, then fan out "
            "advisory lanes for code context, current web facts when needed, "
            "measurements taken, ambiguity critique, simplification, and "
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
