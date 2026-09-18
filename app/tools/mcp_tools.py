"""MCP tool selectors.

This module keeps the public selector functions used by prompts while routing
selection through a small gateway layer. The gateway gives us capability tags
and risk filtering without changing the rest of the agent code.
"""

from typing import Iterable

from langchain_core.tools import BaseTool

from app.mcp_core.client import get_mcp_client
from app.mcp_core.gateway import MCPToolGateway
from app.utils.logger import app_logger


PLANNING_MCP_SERVERS = ["weather", "search", "amap", "aigohotel-mcp"]


async def _get_tools_by_capability(
    capabilities: Iterable[str] | None = None,
    servers: list[str] | None = None,
) -> list[BaseTool]:
    try:
        manager = await get_mcp_client(servers=servers)
        tools = await manager.get_tools()
    except Exception as exc:
        app_logger.warning(f"MCP tools unavailable for servers={servers}: {exc}")
        return []
    gateway = MCPToolGateway(tools)
    selected_tools = gateway.list_tools(capabilities=capabilities, max_risk_level="read")
    app_logger.info(
        f"Loaded {len(selected_tools)} MCP tools"
        + (f" for capabilities {list(capabilities)}" if capabilities else "")
    )
    return selected_tools


async def get_all_mcp_tools() -> list[BaseTool]:
    """Return all read-safe MCP tools."""

    return await _get_tools_by_capability()


async def get_hotel_tools() -> list[BaseTool]:
    """Return hotel and nearby POI tools."""

    return await _get_tools_by_capability(["hotel.search", "map.poi"], servers=PLANNING_MCP_SERVERS)


async def get_weather_tools() -> list[BaseTool]:
    """Return weather tools."""

    return await _get_tools_by_capability(["weather.realtime"], servers=PLANNING_MCP_SERVERS)


async def get_search_tools() -> list[BaseTool]:
    """Return travel search tools."""

    return await _get_tools_by_capability(["search.web"], servers=PLANNING_MCP_SERVERS)


async def get_date_tools() -> list[BaseTool]:
    """Return date tools using legacy name matching until date MCP metadata exists."""

    try:
        manager = await get_mcp_client(servers=PLANNING_MCP_SERVERS)
        all_tools = await manager.get_tools()
    except Exception as exc:
        app_logger.warning(f"Date MCP tools unavailable: {exc}")
        return []
    date_tools = [
        tool for tool in all_tools
        if any(keyword in tool.name.lower() for keyword in ["get-current-date", "gettodaydate", "current_date", "today"])
    ]
    app_logger.info(f"Loaded {len(date_tools)} date MCP tools")
    return date_tools
