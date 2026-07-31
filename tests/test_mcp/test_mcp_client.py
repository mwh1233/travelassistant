"""测试 MCP 客户端"""
import pytest
import json

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.mcp_core.client import MCPClientManager


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前重置单例"""
    MCPClientManager.reset_instance()
    yield
    MCPClientManager.reset_instance()


@pytest.mark.asyncio
async def test_print_mcp_tools():
    """测试打印所有 MCP 工具"""

    print("\n" + "=" * 60)
    print("[Init] 正在初始化 MCP 客户端管理器...")

    manager = await MCPClientManager.get_instance(
        servers=["weather", "search", "amap", "12306-mcp","VariFlight-Aviation","aigohotel-mcp"]
    )

    try:
        # 获取所有工具
        tools = await manager.get_tools()

        print(f"[OK] 连接成功！共发现 {len(tools)} 个工具")
        print("=" * 60)

        # 打印工具详情
        for i, tool in enumerate(tools, 1):
            print(f"[Tool] [{i}]")
            print(f"Name: {tool.name}")
            print(f"Desc: {tool.description}")
            print("Args:")
            try:
                print(json.dumps(tool.args, indent=2, ensure_ascii=False))
            except Exception:
                print(f"   {tool.args}")
            print("-" * 60)

        assert len(tools) > 0, "应该至少有一个工具"

    finally:
        print("\n[Close] 正在关闭连接...")
        await manager.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_print_mcp_tools())
