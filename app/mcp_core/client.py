"""MCP client manager.

The manager keeps the existing public API while making server selection safer:
internal config keys are stripped before creating the MCP client, missing env
vars are skipped, and requesting a different server set rebuilds the singleton.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.utils.logger import app_logger

load_dotenv()


class MCPClientManager:
    """Singleton manager for MCP server connections."""

    _instance: Optional["MCPClientManager"] = None
    _lock = asyncio.Lock()
    _servers: set[str] = set()
    _requested_servers: set[str] = set()

    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
    ENV_VARS = os.environ.copy()
    ENV_VARS["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + ENV_VARS.get("PYTHONPATH", "")

    SERVER_CONFIGS = {
        "weather": {
            "command": sys.executable,
            "args": ["-m", "app.mcp_core.servers.weather_server"],
            "transport": "stdio",
            "env": ENV_VARS,
        },
        "search": {
            "command": sys.executable,
            "args": ["-m", "app.mcp_core.servers.search_server"],
            "transport": "stdio",
            "env": ENV_VARS,
        },
        "amap": {
            "url": f"https://mcp.amap.com/mcp?key={os.getenv('AMAP_API_KEY', '')}",
            "transport": "http",
            "requires_env": ["AMAP_API_KEY"],
        },
        "12306-mcp": {
            "url": "https://mcp.api-inference.modelscope.net/215d3cfb299e47/mcp",
            "transport": "streamable_http",
        },
        "VariFlight-Aviation": {
            "url": f"https://ai.variflight.com/servers/aviation/mcp/?api_key={os.getenv('VARIFLIGHT_API_KEY', '')}",
            "transport": "streamable_http",
            "requires_env": ["VARIFLIGHT_API_KEY"],
        },
        "aigohotel-mcp": {
            "url": "https://mcp.aigohotel.com/mcp",
            "transport": "streamable_http",
            "headers": {
                "Authorization": f"Bearer {os.getenv('AIGOHOTEL_MCP_API')}",
                "Content-Type": "application/json",
            },
            "requires_env": ["AIGOHOTEL_MCP_API"],
        },
    }

    def __init__(self) -> None:
        self._client: Optional[MultiServerMCPClient] = None
        self._tools: Optional[list] = None
        self.enabled_servers: set[str] = set()

    @classmethod
    async def get_instance(cls, servers: list[str] | None = None) -> "MCPClientManager":
        """Return a manager initialized for the requested server set."""

        requested_servers = set(servers or cls.SERVER_CONFIGS.keys())
        if cls._instance is None or not requested_servers.issubset(cls._requested_servers):
            async with cls._lock:
                if cls._instance is None or not requested_servers.issubset(cls._requested_servers):
                    if cls._instance is not None:
                        await cls._instance.close()
                    instance = cls()
                    await instance.initialize(sorted(requested_servers))
                    cls._instance = instance
                    cls._servers = instance.enabled_servers
                    cls._requested_servers = requested_servers
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton for tests."""

        cls._instance = None
        cls._servers = set()
        cls._requested_servers = set()

    async def initialize(self, servers: list[str] | None = None) -> None:
        """Initialize the MCP client."""

        if self._client is not None:
            app_logger.warning("MCP client is already initialized; skipping")
            return

        requested_servers = servers or list(self.SERVER_CONFIGS.keys())
        unknown_servers = [name for name in requested_servers if name not in self.SERVER_CONFIGS]
        if unknown_servers:
            app_logger.warning(f"Ignoring unknown MCP servers: {unknown_servers}")

        configs = {}
        for name in requested_servers:
            cfg = self.SERVER_CONFIGS.get(name)
            if not cfg:
                continue

            required_envs = cfg.get("requires_env", [])
            missing = [key for key in required_envs if not os.getenv(key)]
            if missing:
                app_logger.warning(f"Skipping MCP server {name}; missing env vars: {missing}")
                continue

            configs[name] = {
                key: value
                for key, value in cfg.items()
                if key != "requires_env"
            }

        if not configs:
            raise ValueError("No available MCP server configs")

        app_logger.info(f"Initializing MCP servers: {list(configs.keys())}")
        self.enabled_servers = set(configs.keys())
        self._client = MultiServerMCPClient(configs)

        try:
            self._tools = await self._load_tools_best_effort()
            app_logger.info(f"Loaded {len(self._tools)} MCP tools")
        except Exception as exc:
            app_logger.warning(f"Failed to preload MCP tools: {exc}")
            self._tools = []

    async def close(self) -> None:
        """Close the MCP client."""

        self._client = None
        self._tools = None
        self.enabled_servers = set()
        app_logger.info("MCP client closed")

    async def get_tools(self) -> list:
        """Return available MCP tools."""

        if self._client is None:
            raise RuntimeError("MCP client is not initialized")
        if self._tools is not None:
            return self._tools
        self._tools = await self._load_tools_best_effort()
        return self._tools

    async def _load_tools_best_effort(self) -> list:
        """Load tools per server; one failing server does not break the rest."""

        if self._client is None:
            raise RuntimeError("MCP client is not initialized")

        all_tools = []
        failed_servers = []
        timeout_s = float(os.getenv("MCP_TOOL_LOAD_TIMEOUT", "20"))

        async def _load_one(server_name: str):
            try:
                tools = await asyncio.wait_for(
                    self._client.get_tools(server_name=server_name),
                    timeout=timeout_s,
                )
                return server_name, tools, None
            except Exception as exc:
                return server_name, [], exc

        tasks = [
            asyncio.create_task(_load_one(server_name))
            for server_name in self._client.connections.keys()
        ]
        results = await asyncio.gather(*tasks)

        for server_name, tools, error in results:
            if error is None:
                all_tools.extend(self._normalize_tool_schemas(tools))
                continue
            failed_servers.append(server_name)
            app_logger.warning(f"MCP server {server_name} failed to load tools: {error}")

        if failed_servers and not all_tools:
            raise RuntimeError(f"All MCP servers failed to load tools: {failed_servers}")

        return all_tools

    @staticmethod
    def _normalize_tool_schemas(tools: list) -> list:
        """Fill missing JSON-schema fields on third-party MCP tools."""

        for tool in tools:
            args_schema = getattr(tool, "args_schema", None)
            if not isinstance(args_schema, dict):
                continue
            if "properties" in args_schema:
                continue
            normalized_schema = {
                **args_schema,
                "type": args_schema.get("type", "object"),
                "properties": {},
            }
            tool.args_schema = normalized_schema
        return tools


async def get_mcp_client(servers: list[str] | None = None) -> MCPClientManager:
    """Return an MCP client manager instance."""

    return await MCPClientManager.get_instance(servers)
