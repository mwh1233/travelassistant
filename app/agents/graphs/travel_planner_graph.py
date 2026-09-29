"""Explicit graph workflow for the travel planner."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from jinja2 import Template
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from app.agents.handoffs.step_config import get_step_config
from app.core.state import TravelState
from app.core.store import get_user_memory_service
from app.observability.tracing import trace_operation
from app.utils.logger import app_logger


STEP_SEQUENCE = [
    "requirement_collection",
    "destination_recommendation",
    "transport_planning",
    "accommodation_planning",
    "food_planning",
    "itinerary_generation",
    "budget_summarization",
    "order_generation",
]


def _route_current_step(state: TravelState) -> str:
    current_step = state.get("current_step") or "requirement_collection"
    if current_step not in STEP_SEQUENCE:
        app_logger.warning(f"Unknown planning step {current_step}; falling back to requirement_collection")
        return "requirement_collection"
    return current_step


async def _load_memory_prompt(user_id: str | None) -> str:
    if not user_id:
        return ""
    try:
        service = await get_user_memory_service()
        return await service.format_memory_for_prompt(user_id)
    except Exception as exc:
        app_logger.warning(f"Failed to load user memory for prompt: {exc}")
        return ""


async def _render_system_prompt(step_config: dict[str, Any], state: TravelState) -> str:
    state_dict = dict(state) if hasattr(state, "items") else {}
    memory_prompt = await _load_memory_prompt(state_dict.get("user_id"))
    state_dict["user_memory"] = memory_prompt

    try:
        prompt = Template(step_config["prompt"]).render(**state_dict)
    except Exception as exc:
        app_logger.warning(f"Failed to render step prompt, using raw prompt: {exc}")
        prompt = step_config["prompt"]

    if memory_prompt:
        prompt = f"{prompt}\n\n{memory_prompt}"
    return prompt


def _missing_requirements(step_config: dict[str, Any], state: TravelState) -> list[str]:
    return [
        field
        for field in step_config.get("requires", [])
        if field not in state or state[field] is None
    ]


def _build_step_node(
    step_name: str,
    step_config: dict[str, Any],
    model_factory: Callable[[], Any],
) -> Callable[[TravelState], Awaitable[dict[str, Any]]]:
    async def _node(state: TravelState) -> dict[str, Any]:
        app_logger.info(f"Executing travel planner step: {step_name}")
        missing = _missing_requirements(step_config, state)
        if missing:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "当前规划信息还不完整，缺少："
                            f"{', '.join(missing)}。请先补充这些信息。"
                        )
                    )
                ],
                "current_step": step_name,
            }

        with trace_operation(
            "travel_planner_step",
            step=step_name,
            user_id=state.get("user_id"),
            session_id=state.get("session_id"),
        ):
            agent = create_agent(
                model=model_factory(),
                tools=step_config.get("tools", []),
                state_schema=TravelState,
                system_prompt=await _render_system_prompt(step_config, state),
            )
            result = await agent.ainvoke(dict(state))
        if isinstance(result, dict):
            # `_route_current_step` 对非法 current_step 会回落到 requirement_collection，
            # 但状态里仍残留那个非法值，下一轮会再次回落。
            # 这里把节点自身的合法步骤写回：若工具没有推进步骤，则纠正为当前步骤；
            # 若工具已推进（Command.update 携带合法 current_step），则保留工具的结果。
            if result.get("current_step") not in STEP_SEQUENCE:
                result = {**result, "current_step": step_name}
            return result
        return {"messages": [AIMessage(content=str(result))]}

    return _node


async def create_travel_planner_graph(
    model_factory: Callable[[], Any],
    checkpointer: Any = None,
):
    """Create the explicit graph-based travel planner."""

    step_config = await get_step_config()
    workflow = StateGraph(TravelState)

    for step_name in STEP_SEQUENCE:
        workflow.add_node(
            step_name,
            _build_step_node(step_name, step_config[step_name], model_factory),
        )
        workflow.add_edge(step_name, END)

    workflow.add_conditional_edges(
        START,
        _route_current_step,
        {step_name: step_name for step_name in STEP_SEQUENCE},
    )

    compile_kwargs = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    graph = workflow.compile(**compile_kwargs)
    app_logger.info("Structured travel planner graph created")
    return graph
