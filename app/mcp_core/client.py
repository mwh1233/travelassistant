"""
MCP 客户端管理器
统一管理所有 MCP 服务连接
"""
import asyncio
import os
import sys
from typing import Optional, List
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from app.utils.logger import app_logger

load_dotenv()


class MCPClientManager:
    """
    MCP 客户端管理器（单例模式）
    """

    _instance: Optional['MCPClientManager'] = None
    _client: Optional[MultiServerMCPClient] = None
    _tools: Optional[List] = None
    _lock = asyncio.Lock()

    # 项目根目录（用于 stdio 服务）
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    # 环境变量（追加 PYTHONPATH）
    ENV_VARS = os.environ.copy()
    ENV_VARS["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + ENV_VARS.get("PYTHONPATH", "")

    # 服务器配置
    SERVER_CONFIGS = {
        # ========== 自建服务（stdio） ==========
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

        # ========== 外部服务（HTTP） ==========
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
                "Content-Type": "application/json"
            },
            "requires_env": ["AIGOHOTEL_MCP_API"],
        },
    }

    @classmethod
    async def get_instance(cls, servers: List[str] = None) -> 'MCPClientManager':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance.initialize(servers=servers)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        cls._instance = None

    async def initialize(self, servers: List[str] = None):
        """
        初始化 MCP 客户端

        Args:
            servers: 要启用的服务列表，默认启用所有
        """
        if self._client is not None:
            app_logger.warning("MCP 客户端已初始化，跳过")
            return

        # 默认启用所有服务
        servers = servers or list(self.SERVER_CONFIGS.keys())
        unknown_servers = [name for name in servers if name not in self.SERVER_CONFIGS]
        if unknown_servers:
            app_logger.warning(f"忽略未知 MCP 服务: {unknown_servers}")

        configs = {}
        for name in servers:
            if name not in self.SERVER_CONFIGS:
                continue
            cfg = self.SERVER_CONFIGS[name]
            required_envs = cfg.get("requires_env", [])
            missing = [key for key in required_envs if not os.getenv(key)]
            if missing:
                app_logger.warning(
                    f"跳过服务 {name}，缺少环境变量: {missing}"
                )
                continue
            configs[name] = cfg

        if not configs:
            raise ValueError("未找到可用的 MCP 服务配置，请检查 servers 参数")

        app_logger.info(f"初始化 MCP: {list(configs.keys())}")

        # 创建客户端
        self._client = MultiServerMCPClient(configs)

        # 预加载工具
        try:
            self._tools = await self._load_tools_best_effort()
            app_logger.info(f"已加载 {len(self._tools)} 个 MCP 工具")
        except Exception as e:
            app_logger.warning(f"预加载工具失败: {e}")
            self._tools = []

    async def close(self):
        """关闭客户端"""
        if self._client:
            self._client = None
            self._tools = None
            app_logger.info("MCP 客户端已关闭")

    #充当对外接口(懒加载策略)
    async def get_tools(self) -> List:
        """
        获取所有 MCP 工具

        Returns:
            LangChain 工具列表
        """
        if self._client is None:
            raise RuntimeError("MCP 客户端未初始化，请先调用 initialize()")

        # 如果已缓存，直接返回（包括空列表）
        if self._tools is not None:
            return self._tools

        # 否则重新获取（按服务容错，避免单点失败影响整体）
        self._tools = await self._load_tools_best_effort()
        return self._tools

    async def _load_tools_best_effort(self) -> List:
        """
        逐个服务加载工具，单个服务失败不影响其他服务。
        """
        if self._client is None:
            raise RuntimeError("MCP 客户端未初始化")

        all_tools = []
        failed_servers = []
        timeout_s = float(os.getenv("MCP_TOOL_LOAD_TIMEOUT", "20"))

        async def _load_one(server_name: str):
            try:
                tools = await asyncio.wait_for(
                    self._client.get_tools(server_name=server_name),
                    timeout=timeout_s
                )
                return server_name, tools, None
            except Exception as e:
                return server_name, [], e

        tasks = [
            asyncio.create_task(_load_one(server_name))
            for server_name in self._client.connections.keys()
        ]
        results = await asyncio.gather(*tasks)

        for server_name, tools, error in results:
            if error is None:
                all_tools.extend(tools)
                continue
            failed_servers.append(server_name)
            app_logger.warning(f"服务 {server_name} 加载工具失败: {error}")

        if failed_servers and not all_tools:
            raise RuntimeError(f"所有 MCP 服务均加载失败: {failed_servers}")

        return all_tools


async def get_mcp_client(servers: List[str] = None) -> MCPClientManager:
    """获取 MCP 客户端管理器实例"""
    return await MCPClientManager.get_instance(servers)
