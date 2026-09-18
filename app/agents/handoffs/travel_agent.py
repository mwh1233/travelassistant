"""Factory for the main graph-based travel planning agent."""

from __future__ import annotations

import asyncio

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


async def create_travel_agent():
    """Create the explicit LangGraph travel planning agent."""

    app_logger.info("Creating graph-based Travel Agent...")

    try:
        checkpointer = await asyncio.wait_for(get_checkpointer(), timeout=3)
        app_logger.info("Using Postgres checkpointer for Travel Agent")
    except Exception as exc:
        app_logger.warning(f"Postgres checkpointer unavailable, falling back to memory: {exc}")
        checkpointer = MemorySaver()

    agent = await create_travel_planner_graph(
        model_factory=get_llm,
        checkpointer=checkpointer,
    )

    app_logger.info("Travel Agent graph created")
    return agent
