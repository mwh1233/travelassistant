"""Lightweight graders for travel planning regression checks."""

from __future__ import annotations

from typing import Any


def requirement_completeness(state: dict[str, Any], required_fields: list[str]) -> float:
    """Score how many required requirement/state fields are present."""

    if not required_fields:
        return 1.0

    requirement = state.get("user_requirement") or {}
    present = 0
    for field in required_fields:
        value = state.get(field)
        if value is None:
            value = requirement.get(field) if isinstance(requirement, dict) else None
        if value not in (None, "", []):
            present += 1
    return present / len(required_fields)


def itinerary_executability(state: dict[str, Any]) -> float:
    """Score whether itinerary items contain executable structure."""

    itinerary = state.get("structured_itinerary") or {}
    days = itinerary.get("days") if isinstance(itinerary, dict) else None
    if not days:
        return 0.0

    checks = 0
    passed = 0
    for day in days:
        items = day.get("items") or []
        checks += 1
        if items:
            passed += 1
        for item in items:
            checks += 4
            passed += int(bool(item.get("title")))
            passed += int(bool(item.get("time_slot")))
            passed += int(bool(item.get("duration_minutes")))
            passed += int(bool(item.get("reason")))
    return passed / checks if checks else 0.0


def budget_constraint_score(state: dict[str, Any]) -> float:
    """Score whether budget exists and respects the user budget when known."""

    requirement = state.get("user_requirement") or {}
    budget = state.get("structured_budget") or {}
    total = budget.get("total") if isinstance(budget, dict) else None
    if not total:
        return 0.0

    expected_amount = total.get("expected_amount")
    if expected_amount is None:
        return 0.0

    adult_count = int(requirement.get("adult_count") or 1)
    children_count = int(requirement.get("children_count") or 0)
    people = max(1, adult_count + children_count)
    budget_max = requirement.get("budget_max")
    if budget_max is None:
        return 1.0

    try:
        max_total = float(budget_max) * people
    except (TypeError, ValueError):
        return 0.8

    if expected_amount <= max_total:
        return 1.0
    over_ratio = (expected_amount - max_total) / max_total
    return max(0.0, 1.0 - over_ratio)


def source_coverage_score(state: dict[str, Any]) -> float:
    """Score whether structured outputs carry source references."""

    itinerary = state.get("structured_itinerary") or {}
    budget = state.get("structured_budget") or {}
    checks = 0
    passed = 0

    for day in itinerary.get("days", []):
        for item in day.get("items", []):
            checks += 1
            passed += int(bool(item.get("sources")))

    for item in budget.get("items", []):
        checks += 1
        amount = item.get("amount") or {}
        passed += int(bool(amount.get("source")))

    return passed / checks if checks else 0.0


def grade_travel_state(state: dict[str, Any], required_fields: list[str]) -> dict[str, float]:
    """Return a compact score report for one travel planning state."""

    scores = {
        "requirement_completeness": requirement_completeness(state, required_fields),
        "itinerary_executability": itinerary_executability(state),
        "budget_constraint": budget_constraint_score(state),
        "source_coverage": source_coverage_score(state),
    }
    scores["overall"] = sum(scores.values()) / len(scores)
    return scores
