"""Safe access layer for MCP tools."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Iterable

from langchain_core.tools import BaseTool

from app.mcp_core.registry import ToolCapability, resolve_tool_capability
from app.utils.logger import app_logger


@dataclass
class GatewayTool:
    """A tool plus operational metadata."""

    tool: BaseTool
    capability: ToolCapability


class MCPToolGateway:
    """Normalizes MCP tool discovery and invocation policy."""

    def __init__(self, tools: Iterable[BaseTool]):
        self._tools = list(tools)
        self._metadata = {
            tool.name: resolve_tool_capability(tool.name)
            for tool in self._tools
        }

    def list_tools(
        self,
        capabilities: Iterable[str] | None = None,
        max_risk_level: str = "read",
    ) -> list[BaseTool]:
        """Return tools filtered by capability and risk."""

        allowed_capabilities = set(capabilities or [])
        risk_order = {"read": 0, "write": 1, "purchase": 2, "payment": 3}
        max_risk = risk_order.get(max_risk_level, 0)

        selected: list[BaseTool] = []
        for tool in self._tools:
            metadata = self._metadata[tool.name]
            if not metadata.enabled:
                continue
            if allowed_capabilities and metadata.capability not in allowed_capabilities:
                continue
            if risk_order.get(metadata.risk_level, 99) > max_risk:
                continue
            selected.append(tool)
        return selected

    def describe_tools(self) -> list[GatewayTool]:
        """Return tools with gateway metadata for diagnostics."""

        return [
            GatewayTool(tool=tool, capability=self._metadata[tool.name])
            for tool in self._tools
        ]

    async def ainvoke(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        """Invoke an MCP tool with timeout and audit logging."""

        tool = next((candidate for candidate in self._tools if candidate.name == tool_name), None)
        if tool is None:
            raise ValueError(f"Unknown MCP tool: {tool_name}")

        metadata = self._metadata[tool.name]
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                tool.ainvoke(tool_input),
                timeout=metadata.timeout_seconds,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            app_logger.info(
                f"MCP tool success: name={tool.name}, capability={metadata.capability}, elapsed_ms={elapsed_ms}"
            )
            return result
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            app_logger.warning(
                f"MCP tool failed: name={tool.name}, capability={metadata.capability}, elapsed_ms={elapsed_ms}, error={exc}"
            )
            raise
