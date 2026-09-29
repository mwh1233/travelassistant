"""Factory for the main graph-based travel planning agent."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

from app.agents.graphs.travel_planner_graph import create_travel_planner_graph
from app.config import settings
from app.core.checkpointer import get_checkpointer
from app.utils.logger import app_logger


def get_llm() -> ChatOpenAI:
    """Return the configured chat model."""

    return ChatOpenAI(
        model=settings.qwen_model_name,
        base_url=settings.qwen_base_url,
        api_key=settings.dashscope_api_key,
        temperature=settings.qwen_temperature,
        max_tokens=settings.qwen_max_tokens,
        streaming=True,
    )


async def resolve_checkpointer(force_memory: bool = False) -> tuple[Any, str]:
    """Resolve the checkpointer and report which backend was actually used.

    Returns a ``(checkpointer, kind)`` tuple where ``kind`` is one of
    ``"postgres"`` or ``"memory"``. Evaluation runs must record ``kind`` because
    a silent fallback to ``MemorySaver`` is a common source of "green locally,
    red in CI" behaviour (see docs/agent-eval-design.md, 6.3).
    """

    if force_memory:
        app_logger.info("Using in-memory checkpointer (forced by caller)")
        return MemorySaver(), "memory"

    try:
        checkpointer = await asyncio.wait_for(get_checkpointer(), timeout=3)
        app_logger.info("Using Postgres checkpointer for Travel Agent")
        return checkpointer, "postgres"
    except Exception as exc:
        app_logger.warning(
            f"Postgres checkpointer unavailable, falling back to memory: {exc}"
        )
        return MemorySaver(), "memory"


async def create_travel_agent(
    model_factory: Callable[[], Any] | None = None,
    checkpointer: Any = None,
    force_memory_checkpointer: bool = False,
):
    """Create the explicit LangGraph travel planning agent.

    Injection points (added for evaluation):

    - ``model_factory``: overrides the hard-coded ``get_llm`` so tests can run a
      deterministic scripted model (Mock mode) or a recorded one (Replay mode).
    - ``checkpointer``: pass an isolated ``MemorySaver`` per evaluation case so
      that persisted conversation state cannot leak between cases.
    - ``force_memory_checkpointer``: skip the Postgres probe entirely.
    """

    app_logger.info("Creating graph-based Travel Agent...")

    factory = model_factory or get_llm

    if checkpointer is None:
        checkpointer, checkpointer_kind = await resolve_checkpointer(force_memory_checkpointer)
    else:
        checkpointer_kind = type(checkpointer).__name__

    app_logger.info(f"Travel Agent checkpointer backend: {checkpointer_kind}")

    agent = await create_travel_planner_graph(
        model_factory=factory,
        checkpointer=checkpointer,
    )

    app_logger.info("Travel Agent graph created")
    return agent
