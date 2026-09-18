"""Budget estimation for travel plans."""

from __future__ import annotations

import math
from typing import Any

from app.schemas.planning import BudgetEstimate, BudgetItem, MoneyRange, SourceReference


TRANSPORT_BASE_PER_PERSON = {
    "flight": 900.0,
    "train": 450.0,
    "driving": 280.0,
}

ACCOMMODATION_BASE_PER_ROOM = {
    "star_hotel": 680.0,
    "economy_hotel": 320.0,
    "hostel": 260.0,
    "youth_hostel": 160.0,
    "comfort_hotel": 420.0,
}

FOOD_BASE_PER_PERSON_DAY = {
    "specialty": 220.0,
    "local": 150.0,
    "chain": 100.0,
}

BUDGET_LEVEL_MULTIPLIER = {
    "economy": 0.85,
    "comfort": 1.0,
    "luxury": 1.45,
}


def _requirement_value(requirement: dict[str, Any], key: str, default: Any) -> Any:
    value = requirement.get(key, default) if isinstance(requirement, dict) else default
    return default if value is None else value


def _money(expected: float, source: SourceReference, spread: float = 0.2) -> MoneyRange:
    return MoneyRange(
        min_amount=round(max(0.0, expected * (1 - spread)), 2),
        expected_amount=round(max(0.0, expected), 2),
        max_amount=round(max(0.0, expected * (1 + spread)), 2),
        estimated=True,
        source=source,
    )


def estimate_budget(state: dict[str, Any]) -> BudgetEstimate:
    """Estimate a travel budget from structured state and fallback heuristics."""

    requirement = state.get("user_requirement") or {}
    adult_count = int(_requirement_value(requirement, "adult_count", 1))
    children_count = int(_requirement_value(requirement, "children_count", 0))
    people = max(1, adult_count + children_count)
    child_weighted_people = adult_count + children_count * 0.6
    travel_days = max(1, int(_requirement_value(requirement, "travel_days", 1)))
    nights = max(1, travel_days - 1)
    budget_level = _requirement_value(requirement, "budget_level", "comfort")
    multiplier = BUDGET_LEVEL_MULTIPLIER.get(str(budget_level), 1.0)

    source = SourceReference(
        name="Internal budget estimator",
        source_type="heuristic",
        confidence=0.55,
    )

    transport_type = state.get("selected_transport") or "train"
    transport_expected = TRANSPORT_BASE_PER_PERSON.get(str(transport_type), 450.0) * people * 2

    accommodation_types = state.get("selected_accommodation_types") or ["comfort_hotel"]
    room_price = sum(
        ACCOMMODATION_BASE_PER_ROOM.get(str(item), 420.0)
        for item in accommodation_types
    ) / max(1, len(accommodation_types))
    rooms = max(1, math.ceil(people / 2))
    accommodation_expected = room_price * rooms * nights * multiplier

    food_types = state.get("selected_food_types") or ["local"]
    food_price = max(FOOD_BASE_PER_PERSON_DAY.get(str(item), 150.0) for item in food_types)
    food_expected = food_price * child_weighted_people * travel_days * multiplier

    attraction_expected = 180.0 * child_weighted_people * travel_days * multiplier
    misc_expected = 80.0 * people * travel_days

    items = [
        BudgetItem(
            category="transport",
            label="Round-trip major transport",
            amount=_money(transport_expected, source, spread=0.25),
            note="Uses selected transport type with city-level fallback price.",
        ),
        BudgetItem(
            category="accommodation",
            label="Accommodation",
            amount=_money(accommodation_expected, source, spread=0.3),
            note=f"Estimated with {rooms} room(s) for {nights} night(s).",
        ),
        BudgetItem(
            category="food",
            label="Food",
            amount=_money(food_expected, source, spread=0.25),
            note="Adjusted by selected food style and party size.",
        ),
        BudgetItem(
            category="attractions",
            label="Tickets and paid experiences",
            amount=_money(attraction_expected, source, spread=0.35),
            note="Fallback estimate until live ticket data is available.",
        ),
        BudgetItem(
            category="misc",
            label="Local transport and misc",
            amount=_money(misc_expected, source, spread=0.2),
            note="Covers local rides, storage, small purchases, and buffers.",
        ),
    ]

    total_min = sum(item.amount.min_amount for item in items)
    total_expected = sum(item.amount.expected_amount for item in items)
    total_max = sum(item.amount.max_amount for item in items)
    per_person_expected = total_expected / people

    warnings: list[str] = []
    budget_max = _requirement_value(requirement, "budget_max", None)
    if budget_max is not None:
        try:
            user_total_budget = float(budget_max) * people
            if total_expected > user_total_budget:
                warnings.append(
                    "Estimated total exceeds the user's upper budget; consider cheaper lodging, train travel, or fewer paid attractions."
                )
        except (TypeError, ValueError):
            pass

    assumptions = [
        "Prices are interval estimates and should be refreshed with live transport/hotel/ticket tools before booking.",
        "Children are counted as 60% of adult food and attraction cost in fallback estimation.",
        "Accommodation uses room-level pricing with two people per room as the default assumption.",
    ]

    return BudgetEstimate(
        items=items,
        total=MoneyRange(
            min_amount=round(total_min, 2),
            expected_amount=round(total_expected, 2),
            max_amount=round(total_max, 2),
            estimated=True,
            source=source,
        ),
        per_person=MoneyRange(
            min_amount=round(total_min / people, 2),
            expected_amount=round(per_person_expected, 2),
            max_amount=round(total_max / people, 2),
            estimated=True,
            source=source,
        ),
        assumptions=assumptions,
        warnings=warnings,
    )


def format_budget_summary(estimate: BudgetEstimate) -> str:
    """Format a concise budget summary for tool output."""

    lines = [
        "预算估算完成：",
        (
            f"总预算区间：{estimate.total.min_amount:.0f}-"
            f"{estimate.total.max_amount:.0f} {estimate.total.currency}"
            f"（参考值 {estimate.total.expected_amount:.0f}）"
        ),
        (
            f"人均区间：{estimate.per_person.min_amount:.0f}-"
            f"{estimate.per_person.max_amount:.0f} {estimate.per_person.currency}"
            f"（参考值 {estimate.per_person.expected_amount:.0f}）"
        ),
    ]
    for item in estimate.items:
        lines.append(
            f"- {item.label}: {item.amount.expected_amount:.0f} "
            f"{item.amount.currency} ({item.amount.min_amount:.0f}-{item.amount.max_amount:.0f})"
        )
    if estimate.warnings:
        lines.append("风险提示：" + "；".join(estimate.warnings))
    lines.append("估算说明：" + "；".join(estimate.assumptions))
    return "\n".join(lines)
