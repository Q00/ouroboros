"""MCP input schema for structured first-question presentation."""

from typing import Any

from ouroboros.mcp.types import MCPToolParameter, ToolInputType


def first_question_parameters() -> tuple[MCPToolParameter, ...]:
    return (
        MCPToolParameter(
            name="opening_revision",
            type=ToolInputType.STRING,
            required=False,
            description="Revision of the exact opening and decisions shown to the person.",
        ),
        MCPToolParameter(
            name="opening_confirm_item_id",
            type=ToolInputType.STRING,
            required=False,
            description="One opening item explicitly confirmed by the person; never infer from a recommendation.",
        ),
        MCPToolParameter(
            name="opening_user_confirmed",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="True only after explicit individual confirmation of the exact item.",
        ),
        MCPToolParameter(
            name="opening_confirmation_text",
            type=ToolInputType.STRING,
            required=False,
            description="The person's exact response to the individual confirmation or neither question.",
        ),
        MCPToolParameter(
            name="opening_confirmation_prompt",
            type=ToolInputType.STRING,
            required=False,
            description="The exact server-issued confirmation prompt shown to the person.",
        ),
        MCPToolParameter(
            name="opening_neither",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="True only when the person explicitly rejects both opening interpretations.",
        ),
        MCPToolParameter(
            name="propose_opening_interpretations",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="Opt in to two unconfirmed opening interpretations; explain or reject both in free text.",
        ),
        MCPToolParameter(
            name="structured_first_batch",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="Opt in to a proposed first batch of questions, requiring explicit independence confirmation.",
        ),
        MCPToolParameter(
            name="batch_page_size",
            type=ToolInputType.INTEGER,
            required=False,
            description="Optional native-host question capacity per presentation (1–5); repeat on resume and answers.",
        ),
        MCPToolParameter(
            name="batch_reject",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="Discard unanswered batch questions while keeping recorded answers.",
        ),
        MCPToolParameter(
            name="batch_answers",
            type=ToolInputType.ARRAY,
            required=False,
            items={"type": "object"},
            max_items=5,
            description="ID-addressed answers to a confirmed batch; do not combine with other answer fields.",
        ),
        MCPToolParameter(
            name="inline_material_text",
            type=ToolInputType.STRING,
            required=False,
            description=(
                "Optional material supplied with a first-batch reply (up to 4000 characters). "
                "It is stored in the local interview state and literal excerpts are returned "
                "to the host only with explicit inline_material_consent. It is not user intent."
            ),
        ),
        MCPToolParameter(
            name="inline_material_consent",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="True only when the person explicitly agrees to local storage and display of excerpts to the calling host.",
        ),
        MCPToolParameter(
            name="inline_material_retry",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="Explicitly retry local extraction of pending or unavailable material excerpts. This operation does not send the stored source to a model.",
        ),
        MCPToolParameter(
            name="inline_material_user_followup_question",
            type=ToolInputType.STRING,
            required=False,
            description="For reopening an interview with optional material and no pending question: the exact new question explicitly authored by the person, not material or a model suggestion. Do not combine with last_question.",
        ),
        MCPToolParameter(
            name="material_evidence_action",
            type=ToolInputType.STRING,
            required=False,
            description="Explicit local evidence action: include, remove, or resolve. Requires a session ID and cannot be combined with other interview input.",
        ),
        MCPToolParameter(
            name="material_evidence_kind",
            type=ToolInputType.STRING,
            required=False,
            description="Item kind: source_quote or model_assumption; inclusion does not confirm truth or user intent.",
        ),
        MCPToolParameter(
            name="material_evidence_index",
            type=ToolInputType.INTEGER,
            required=False,
            description="Zero-based item index displayed with the material draft.",
        ),
        MCPToolParameter(
            name="material_evidence_revision",
            type=ToolInputType.STRING,
            required=False,
            description="Current revision displayed with the material draft.",
        ),
        MCPToolParameter(
            name="material_evidence_prompt",
            type=ToolInputType.STRING,
            required=False,
            description="Exact server-provided decision or retrieval prompt shown to the person.",
        ),
        MCPToolParameter(
            name="material_evidence_response",
            type=ToolInputType.STRING,
            required=False,
            description="Person's exact INCLUDE, REMOVE, or SHOW response, asserted by the host.",
        ),
        MCPToolParameter(
            name="material_evidence_source_id",
            type=ToolInputType.STRING,
            required=False,
            description="Opaque local source ID from a Seed reference, required for resolution.",
        ),
        MCPToolParameter(
            name="material_evidence_id",
            type=ToolInputType.STRING,
            required=False,
            description="Opaque local evidence ID from a Seed reference, required for resolution.",
        ),
        MCPToolParameter(
            name="batch_answers_text",
            type=ToolInputType.STRING,
            required=False,
            description=(
                "Numbered answers such as 1: 2 / 2: a,c / 3: free text; '-' selects none. "
                "Use JSON quotes for literal labels, numbers or text containing '/ 2:'."
            ),
        ),
        MCPToolParameter(
            name="batch_confirm_digest",
            type=ToolInputType.STRING,
            required=False,
            description="Digest of the exact question batch shown to the user.",
        ),
        MCPToolParameter(
            name="batch_independence_confirmed",
            type=ToolInputType.BOOLEAN,
            required=False,
            description="True only after the user confirms that the proposed questions are independent.",
        ),
        MCPToolParameter(
            name="selected_first_choice",
            type=ToolInputType.INTEGER,
            description="Explicit 1-based selection from a pending structured first question; omit answer.",
            required=False,
        ),
        MCPToolParameter(
            name="selected_first_choices",
            type=ToolInputType.ARRAY,
            description="Explicit 1-based multi-selection; an empty array selects none. Omit answer.",
            required=False,
            items={"type": "integer"},
            max_items=4,
        ),
        MCPToolParameter(
            name="selected_first_choices_text",
            type=ToolInputType.STRING,
            description="Explicit multi-selection as comma-separated a-d labels, or '-' for none; omit answer.",
            required=False,
        ),
        MCPToolParameter(
            name="multi_select_first_question",
            type=ToolInputType.BOOLEAN,
            description="Opt in to multi-selection on the structured first question.",
            required=False,
        ),
        MCPToolParameter(
            name="structured_first_question",
            type=ToolInputType.BOOLEAN,
            description=(
                "Opt in on start to one structured first question. "
                "In-process mode only; subsequent questions remain free text."
            ),
            required=False,
        ),
    )


def normalize_first_selections(arguments: dict[str, Any]) -> list[int] | None:
    from ouroboros.interview_adapters.first_question import parse_selection_text

    selections = arguments.get("selected_first_choices")
    selection_text = arguments.get("selected_first_choices_text")
    if selection_text is not None:
        if not isinstance(selection_text, str) or selections is not None:
            raise ValueError("Invalid selection text")
        selections = parse_selection_text(selection_text)
        arguments["selected_first_choices"] = selections
    multi = arguments.get("multi_select_first_question", False)
    if type(multi) is not bool or (multi and not arguments.get("structured_first_question", False)):
        raise ValueError("Multi-select requires a structured first question")
    if selections is not None and (
        not isinstance(selections, list)
        or any(type(index) is not int for index in selections)
        or arguments.get("answer") is not None
        or arguments.get("selected_first_choice") is not None
    ):
        raise ValueError("Invalid multi-selection")
    return selections
