"""MCP tool capability registry."""

from dataclasses import dataclass
from typing import Literal


RiskLevel = Literal["read", "write", "purchase", "payment"]


@dataclass(frozen=True)
class ToolCapability:
    """Metadata used to expose MCP tools safely."""

    capability: str
    risk_level: RiskLevel = "read"
    timeout_seconds: float = 20.0
    enabled: bool = True


DEFAULT_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "get_weather_forecast": ToolCapability("weather.realtime"),
    "get_current_date": ToolCapability("date.current"),
    "current_date": ToolCapability("date.current"),
    "today": ToolCapability("date.current"),
    "search_travel_info": ToolCapability("search.web"),
    "maps_around_search": ToolCapability("map.poi"),
    "find-hotels": ToolCapability("hotel.search"),
    "12306": ToolCapability("transport.train"),
    "train": ToolCapability("transport.train"),
    "flight": ToolCapability("transport.flight"),
    "aviation": ToolCapability("transport.flight"),
}


def resolve_tool_capability(tool_name: str) -> ToolCapability:
    """Resolve a best-effort capability for a tool name."""

    normalized_name = tool_name.lower()
    for keyword, capability in DEFAULT_TOOL_CAPABILITIES.items():
        if keyword.lower() in normalized_name:
            return capability
    return ToolCapability("unknown.read")
