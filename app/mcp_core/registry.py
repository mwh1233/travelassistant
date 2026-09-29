"""Tool vocabulary registry — the single source of truth.

Three vocabularies live here, and datasets may only reference values from them:

``MCP_CAPABILITIES``
    External capabilities the agent may call (weather, maps, hotels, ...).

``INTERNAL_TOOL_CAPABILITIES``
    The state-machine tools, namespaced ``internal.<domain>.<action>`` so they
    can never be confused with an MCP capability. Mapping them into the same
    vocabulary is what lets trajectory checks compute tool recall / precision
    over one closed set.

``FORBIDDEN_CAPABILITIES``
    The deny-list: known-dangerous actions the agent must never perform. These
    are deliberately *not* implemented tool entries — keeping them as an
    explicit vocabulary means ``forbidden_capabilities: [payment.execute]`` can
    be validated against a closed set, while a hallucinated name such as
    ``pay.now`` is reported as an unknown value instead of silently accepted.

``evals/checks.py`` (``L0.capability_vocabulary``) enforces "dataset ⊆
vocabulary". See ``docs/agent-eval-design.md`` P0-2 / Phase 2 item 1.
"""

from dataclasses import dataclass
from typing import Literal


#: ``sensitive`` is included because datasets tag "persist personal data"
#: cases with it (``memory.write_sensitive``).
RiskLevel = Literal["read", "write", "purchase", "payment", "sensitive"]


@dataclass(frozen=True)
class ToolCapability:
    """Metadata used to expose MCP tools safely."""

    capability: str
    risk_level: RiskLevel = "read"
    timeout_seconds: float = 20.0
    enabled: bool = True


#: tool-name keyword -> capability metadata.
#: ``resolve_tool_capability`` walks this table in order and returns the first
#: keyword found inside the lowercased tool name, so **specific keywords must
#: come before broad ones**.
#:
#: Only the two ``map.route`` / ``food.search`` entries are new. Keywords are
#: deliberately kept narrow: widening them (e.g. a bare ``"hotel"``) would
#: change which tools ``MCPToolGateway.list_tools`` exposes at runtime, which
#: is a product change, not an evaluation change.
DEFAULT_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    # --- weather ---------------------------------------------------------
    "get_weather_forecast": ToolCapability("weather.realtime"),
    # --- date ------------------------------------------------------------
    "get_current_date": ToolCapability("date.current"),
    # Real MCP tool names are hyphenated (``get-current-date``); without this
    # alias they resolve to ``unknown.read``. ``get_date_tools()`` sidesteps
    # the gateway with its own keyword list, so this only makes the vocabulary
    # honest.
    "get-current-date": ToolCapability("date.current"),
    "current_date": ToolCapability("date.current"),
    "today": ToolCapability("date.current"),
    # --- web search ------------------------------------------------------
    "search_travel_info": ToolCapability("search.web"),
    # --- maps: POI -------------------------------------------------------
    "maps_around_search": ToolCapability("map.poi"),
    # --- maps: routing ---------------------------------------------------
    # amap's direction tools (maps_direction_driving / _walking / ...).
    "maps_direction": ToolCapability("map.route"),
    # --- hotels ----------------------------------------------------------
    "find-hotels": ToolCapability("hotel.search"),
    # --- food ------------------------------------------------------------
    # NOTE: no MCP server currently exposes a food tool. The capability is
    # declared so `food.search` in datasets has a home; rows expecting it
    # should stay `fallback_expected` until a server is wired.
    "find-restaurants": ToolCapability("food.search"),
    # --- transport -------------------------------------------------------
    "12306": ToolCapability("transport.train"),
    "train": ToolCapability("transport.train"),
    "flight": ToolCapability("transport.flight"),
    "aviation": ToolCapability("transport.flight"),
}


#: External capabilities the agent is allowed to call, derived from the
#: resolution table above so there is exactly one source of truth.
MCP_CAPABILITIES: frozenset[str] = frozenset(
    metadata.capability for metadata in DEFAULT_TOOL_CAPABILITIES.values()
)


#: Every state-machine tool the agent can call, mapped to its vocabulary id.
#: Covers all tools enumerated in ``app/agents/handoffs/step_config.py``.
INTERNAL_TOOL_CAPABILITIES: dict[str, str] = {
    # requirement collection
    "record_requirement_tool": "internal.requirement.record",
    "update_travel_style_tool": "internal.requirement.update_style",
    "update_dietary_restriction_tool": "internal.requirement.update_dietary",
    "update_food_preference_tool": "internal.requirement.update_food",
    "add_travel_record_tool": "internal.memory.record",
    # destination
    "select_destination_tool": "internal.destination.select",
    "query_destination_info": "internal.knowledge.destination_info",
    # transport
    "select_transport_tool": "internal.transport.select",
    "query_transport_options": "internal.transport.query",
    # accommodation
    "select_accommodation_tool": "internal.accommodation.select",
    "update_accommodation_preference_tool": "internal.accommodation.update_preference",
    # food
    "select_food_tool": "internal.food.select",
    # itinerary / budget / order
    "generate_itinerary_tool": "internal.itinerary.generate",
    "summarize_budget_tool": "internal.budget.summarize",
    "generate_order_tool": "internal.order.generate",
    # approval
    "request_action_approval": "internal.approval.request",
    "record_action_approval": "internal.approval.record",
    # rollback (all go_back_* share one id; the target is a tool arg, not a
    # separate capability)
    "go_back_to_requirement": "internal.step.rollback",
    "go_back_to_destination": "internal.step.rollback",
    "go_back_to_transport": "internal.step.rollback",
    "go_back_to_accommodation": "internal.step.rollback",
    "go_back_to_food": "internal.step.rollback",
    "go_back_to_itinerary": "internal.step.rollback",
    "go_back_to_budget": "internal.step.rollback",
    "go_back_to_step": "internal.step.rollback",
}


#: The deny-list of dangerous actions. Not callable tools — the closed set a
#: dataset may forbid. ``transport.train`` / ``transport.flight`` are also
#: legitimately forbidden by user preference ("我不想坐飞机"), so a forbidden
#: value is validated against the whole vocabulary, not just this set.
FORBIDDEN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "payment.execute",
        "hotel.purchase",
        "ticket.purchase",
        "activity.purchase",
        "message.send",
        "location.share",
        "memory.write_sensitive",
    }
)


#: Every value a dataset row is allowed to reference in a tool/capability
#: field. ``L0.capability_vocabulary`` asserts the datasets stay inside this.
KNOWN_TOOL_VOCABULARY: frozenset[str] = (
    MCP_CAPABILITIES
    | frozenset(INTERNAL_TOOL_CAPABILITIES.values())
    | FORBIDDEN_CAPABILITIES
)


def resolve_tool_capability(tool_name: str) -> ToolCapability:
    """Resolve a best-effort capability for a tool name."""

    normalized_name = tool_name.lower()
    for keyword, capability in DEFAULT_TOOL_CAPABILITIES.items():
        if keyword.lower() in normalized_name:
            return capability
    return ToolCapability("unknown.read")


def resolve_tool_id(tool_name: str) -> str:
    """Resolve any concrete tool name (internal or MCP) to a vocabulary id.

    Trajectory checks use this to compare "tools actually called" against
    "tools expected" over a single vocabulary.
    """

    internal = INTERNAL_TOOL_CAPABILITIES.get(tool_name)
    if internal:
        return internal
    return resolve_tool_capability(tool_name).capability


def is_known_tool_id(value: str) -> bool:
    """Whether ``value`` is a legal dataset-side tool vocabulary entry."""

    return value in KNOWN_TOOL_VOCABULARY
