"""Constraint-aware itinerary generation.

The first implementation is intentionally deterministic and dependency-free:
it replaces placeholder activities with a structured plan that can later be
fed by real POI, map, weather, and hotel tools.
"""

from __future__ import annotations

from typing import Any

from app.schemas.planning import (
    ItineraryDayPlan,
    ItineraryItem,
    ItineraryPlan,
    LocationPoint,
    MoneyRange,
    SourceReference,
)


STYLE_ACTIVITY_LIBRARY: dict[str, list[tuple[str, str, int, float]]] = {
    "culture": [
        ("City museum / heritage area", "museum and heritage context", 150, 80),
        ("Old town walk", "compact cultural walking route", 120, 30),
        ("Local craft or performance", "hands-on culture experience", 120, 120),
    ],
    "food": [
        ("Local breakfast street", "signature breakfast and snacks", 90, 50),
        ("Food market route", "high-density local food sampling", 150, 120),
        ("Representative dinner", "one memorable local meal", 120, 180),
    ],
    "relaxation": [
        ("Slow city walk", "low-pressure neighborhood exploration", 120, 20),
        ("Cafe / park break", "rest buffer and light activity", 90, 60),
        ("Scenic sunset spot", "relaxed evening experience", 90, 30),
    ],
    "adventure": [
        ("Outdoor viewpoint", "active scenic experience", 180, 100),
        ("Light hiking route", "moderate outdoor activity", 180, 80),
        ("Cycling or waterside walk", "flexible active option", 120, 70),
    ],
}

DEFAULT_ACTIVITIES = [
    ("City orientation walk", "first-day orientation with low risk", 90, 20),
    ("Landmark visit", "representative local landmark", 150, 100),
    ("Neighborhood exploration", "flexible local experience", 120, 40),
]


def _requirement_value(requirement: dict[str, Any], key: str, default: Any) -> Any:
    value = requirement.get(key, default) if isinstance(requirement, dict) else default
    return default if value is None else value


def _style_pool(styles: list[str]) -> list[tuple[str, str, int, float]]:
    pool: list[tuple[str, str, int, float]] = []
    for style in styles:
        pool.extend(STYLE_ACTIVITY_LIBRARY.get(style, []))
    return pool or DEFAULT_ACTIVITIES


def _money(amount: float, source: SourceReference) -> MoneyRange:
    return MoneyRange(
        min_amount=round(amount * 0.8, 2),
        expected_amount=round(amount, 2),
        max_amount=round(amount * 1.25, 2),
        estimated=True,
        source=source,
    )


def build_itinerary_plan(state: dict[str, Any]) -> ItineraryPlan:
    """Build a structured itinerary from the current graph state."""

    requirement = state.get("user_requirement") or {}
    destination = state.get("selected_destination") or requirement.get("destination") or "selected destination"
    travel_days = int(_requirement_value(requirement, "travel_days", 1))
    travel_days = max(1, min(travel_days, 14))
    styles = list(_requirement_value(requirement, "travel_styles", []) or [])
    children_count = int(_requirement_value(requirement, "children_count", 0))
    selected_food_types = state.get("selected_food_types") or ["local"]
    selected_accommodation_types = state.get("selected_accommodation_types") or ["comfort_hotel"]

    source = SourceReference(
        name="Internal heuristic planner",
        source_type="planner",
        confidence=0.55,
    )
    pool = _style_pool(styles)
    intensity = "low" if children_count > 0 else "medium"
    days: list[ItineraryDayPlan] = []

    for day_number in range(1, travel_days + 1):
        items: list[ItineraryItem] = []
        day_offset = (day_number - 1) * 2
        morning = pool[day_offset % len(pool)]
        afternoon = pool[(day_offset + 1) % len(pool)]

        if day_number == 1:
            morning = ("Arrival and check-in buffer", "avoid a rushed arrival day", 90, 0)
        if day_number == travel_days and travel_days > 1:
            afternoon = ("Return buffer", "leave enough time for transport and packing", 90, 0)

        for time_slot, activity in [("morning", morning), ("afternoon", afternoon)]:
            title, reason, duration, cost = activity
            items.append(
                ItineraryItem(
                    title=f"{destination} {title}",
                    time_slot=time_slot,
                    location=LocationPoint(name=destination),
                    duration_minutes=duration,
                    estimated_cost=_money(cost, source),
                    transport_note="Prefer nearby areas on the same day; verify route time before departure.",
                    reason=reason,
                    sources=[source],
                )
            )

        evening_label = "Local snacks" if "food" in selected_food_types else "Light evening walk"
        items.append(
            ItineraryItem(
                title=f"{destination} {evening_label}",
                time_slot="evening",
                location=LocationPoint(name=destination),
                duration_minutes=90,
                estimated_cost=_money(80 if "food" in selected_food_types else 30, source),
                transport_note="Keep this close to the hotel to reduce fatigue.",
                reason="keeps the evening flexible and leaves recovery time",
                sources=[source],
            )
        )

        days.append(
            ItineraryDayPlan(
                day_number=day_number,
                theme=_build_day_theme(styles, day_number),
                items=items,
                meals=_build_meals(selected_food_types),
                accommodation=", ".join(selected_accommodation_types),
                plan_b="Use an indoor museum, mall, or cafe-heavy route if weather or fatigue becomes an issue.",
                intensity=intensity,
            )
        )

    assumptions = [
        "POI candidates are generated from current user preferences until live POI data is available.",
        "Route order favors same-area activities and daily rest buffers.",
        "Opening hours and ticket prices should be verified by live tools before booking.",
    ]

    return ItineraryPlan(
        destination=destination,
        days=days,
        summary=f"{travel_days}-day {destination} plan focused on {', '.join(styles) or 'balanced travel'}.",
        assumptions=assumptions,
    )


def _build_day_theme(styles: list[str], day_number: int) -> str:
    if not styles:
        return f"Balanced day {day_number}"
    style = styles[(day_number - 1) % len(styles)]
    labels = {
        "culture": "Culture and city context",
        "food": "Local food discovery",
        "relaxation": "Relaxed slow travel",
        "adventure": "Light outdoor exploration",
    }
    return labels.get(style, f"Personalized day {day_number}")


def _build_meals(food_types: list[str]) -> list[str]:
    if "local" in food_types or "food" in food_types:
        return ["local breakfast", "nearby casual lunch", "local specialty dinner"]
    if "chain" in food_types:
        return ["hotel breakfast", "reliable chain lunch", "mall dinner"]
    return ["breakfast", "lunch", "dinner"]


def format_itinerary_summary(plan: ItineraryPlan) -> str:
    """Format a concise tool message for the agent."""

    lines = [f"已生成 {len(plan.days)} 天结构化行程：{plan.summary}"]
    for day in plan.days:
        titles = "；".join(item.title for item in day.items)
        lines.append(f"Day {day.day_number}｜{day.theme}：{titles}")
    if plan.assumptions:
        lines.append("注意：" + "；".join(plan.assumptions))
    return "\n".join(lines)
