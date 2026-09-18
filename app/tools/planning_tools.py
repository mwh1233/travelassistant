"""Structured planning tools used by the optimized travel workflow."""

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.core.state import TravelState
from app.planner.budget_estimator import estimate_budget, format_budget_summary
from app.planner.itinerary_planner import build_itinerary_plan, format_itinerary_summary
from app.utils.logger import app_logger


@tool
def generate_itinerary_tool(
    runtime: ToolRuntime[None, TravelState] = None,
) -> Command:
    """Generate a structured, constraint-aware itinerary plan."""

    app_logger.info("Start generating structured itinerary")
    state = runtime.state if runtime else {}
    required_fields = [
        "user_requirement",
        "selected_destination",
        "selected_transport",
        "selected_accommodation_types",
        "selected_food_types",
    ]
    missing = [field for field in required_fields if field not in state or state[field] is None]
    if missing:
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"信息不完整，缺少：{', '.join(missing)}",
                    tool_call_id=(runtime.tool_call_id if runtime else ""),
                )
            ]
        })

    plan = build_itinerary_plan(dict(state))

    return Command(update={
        "messages": [
            ToolMessage(
                content=format_itinerary_summary(plan),
                tool_call_id=(runtime.tool_call_id if runtime else ""),
            )
        ],
        "itinerary": plan.to_legacy_days(),
        "structured_itinerary": plan.model_dump(),
        "current_step": "budget_summarization",
    })


@tool
def summarize_budget_tool(
    runtime: ToolRuntime[None, TravelState] = None,
) -> Command:
    """Estimate a structured budget from selected planning state."""

    app_logger.info("Start estimating structured budget")
    state = runtime.state if runtime else {}
    if not state.get("user_requirement"):
        return Command(update={
            "messages": [
                ToolMessage(
                    content="信息不完整，缺少：user_requirement",
                    tool_call_id=(runtime.tool_call_id if runtime else ""),
                )
            ]
        })

    estimate = estimate_budget(dict(state))

    return Command(update={
        "messages": [
            ToolMessage(
                content=format_budget_summary(estimate),
                tool_call_id=(runtime.tool_call_id if runtime else ""),
            )
        ],
        "budget": estimate.to_legacy_breakdown(),
        "structured_budget": estimate.model_dump(),
        "current_step": "order_generation",
    })
