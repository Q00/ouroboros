"""MCP input schema for structured first-question presentation."""

from typing import Any

from ouroboros.mcp.types import MCPToolParameter, ToolInputType


def first_question_parameters() -> tuple[MCPToolParameter, ...]:
    return (
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
