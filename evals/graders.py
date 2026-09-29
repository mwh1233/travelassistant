"""Result-level graders for travel planning regression checks.

Two deliberate design decisions, both taken from docs/agent-eval-design.md:

1. **No self-certifying metrics.** ``build_itinerary_plan()`` attaches a
   ``source_type="planner"`` reference to every item, so the previous
   ``source_coverage`` check (``bool(item["sources"])``) was structurally
   pinned at 1.0 while measuring nothing. Coverage now only counts *external*
   evidence; self-produced references are excluded.
2. **Hard gates do not participate in averaging.** A safety or schema failure
   must fail the case outright instead of being diluted by the mean.
"""

from __future__ import annotations

from typing import Any


#: Source types that represent evidence produced outside this codebase.
EXTERNAL_SOURCE_TYPES = frozenset(
    {
        "web",
        "search",
        "map",
        "poi",
        "weather",
        "hotel",
        "transport",
        "flight",
        "train",
        "rag",
        "mcp",
        "tool",
        "api",
        "live",
    }
)

#: Source types produced by deterministic in-repo code. They are legitimate for
#: heuristic output but must never be counted as external evidence.
INTERNAL_SOURCE_TYPES = frozenset({"planner", "heuristic", "estimated", "assumption"})

#: Phrases that assert a real-time external fact. If such a claim carries no
#: external source, the case is a hard failure (``no_unbacked_realtime_claim``).
REALTIME_CLAIM_KEYWORDS = (
    "weather",
    "forecast",
    "opening hours",
    "ticket price",
    "availability",
    "实时",
    "天气",
    "票价",
    "开放时间",
    "余票",
    "预约",
    "预约情况",
)


def _is_external(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    source_type = str(source.get("source_type") or "").strip().lower()
    if source_type in EXTERNAL_SOURCE_TYPES:
        return True
    if source_type in INTERNAL_SOURCE_TYPES:
        return False
    # Fall back to a URL heuristic for sources that carry no explicit type.
    return bool(source.get("url"))


def _iter_itinerary_items(state: dict[str, Any]):
    itinerary = state.get("structured_itinerary") or {}
    if not isinstance(itinerary, dict):
        return
    for day in itinerary.get("days") or []:
        if not isinstance(day, dict):
            continue
        for item in day.get("items") or []:
            if isinstance(item, dict):
                yield item


def _claim_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("reason") or ""),
        str(item.get("transport_note") or ""),
    ]
    return " ".join(parts).lower()


# --------------------------------------------------------------------------
# Dimension scores
# --------------------------------------------------------------------------


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


def external_source_coverage_score(state: dict[str, Any]) -> float:
    """Fraction of structured claims backed by an *external* source.

    Replaces the old ``source_coverage_score``, which counted
    ``source_type="planner"`` references and therefore always returned 1.0.
    """

    checks = 0
    passed = 0

    for item in _iter_itinerary_items(state):
        checks += 1
        sources = item.get("sources") or []
        passed += int(any(_is_external(source) for source in sources))

    budget = state.get("structured_budget") or {}
    if isinstance(budget, dict):
        for item in budget.get("items") or []:
            if not isinstance(item, dict):
                continue
            checks += 1
            amount = item.get("amount") or {}
            passed += int(_is_external(amount.get("source")))

    return passed / checks if checks else 0.0


def unbacked_realtime_claims(state: dict[str, Any]) -> list[str]:
    """Return itinerary titles that assert a live fact without external evidence.

    This is the reverse assertion the design doc asks for: claiming real-time
    facts while only carrying a ``planner`` source is a hard failure, because it
    is exactly the shape of a hallucinated citation.
    """

    offenders: list[str] = []
    for item in _iter_itinerary_items(state):
        text = _claim_text(item)
        if not any(keyword in text for keyword in REALTIME_CLAIM_KEYWORDS):
            continue
        sources = item.get("sources") or []
        if not any(_is_external(source) for source in sources):
            offenders.append(str(item.get("title") or "<untitled>"))
    return offenders


# --------------------------------------------------------------------------
# Hard gates
# --------------------------------------------------------------------------


def _itinerary_schema_ok(state: dict[str, Any]) -> bool:
    itinerary = state.get("structured_itinerary") or {}
    if not isinstance(itinerary, dict):
        return False
    days = itinerary.get("days")
    if not days:
        return False
    for day in days:
        if not isinstance(day, dict):
            return False
        if not day.get("items"):
            return False
        for item in day["items"]:
            if not isinstance(item, dict):
                return False
            if not item.get("title") or not item.get("time_slot"):
                return False
    return True


def _budget_schema_ok(state: dict[str, Any]) -> bool:
    budget = state.get("structured_budget") or {}
    if not isinstance(budget, dict):
        return False
    total = budget.get("total") or {}
    if total.get("expected_amount") is None:
        return False
    return bool(budget.get("items"))


def evaluate_hard_gates(state: dict[str, Any]) -> dict[str, bool]:
    """Result-level gates. Any ``False`` zeroes the overall score."""

    return {
        "itinerary_schema_ok": _itinerary_schema_ok(state),
        "budget_schema_ok": _budget_schema_ok(state),
        "no_unbacked_realtime_claim": not unbacked_realtime_claims(state),
    }


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------


def grade_travel_state(
    state: dict[str, Any],
    required_fields: list[str],
    requires_external_sources: bool = False,
) -> dict[str, Any]:
    """Return a self-describing score report for one travel planning state.

    ``external_source_coverage`` is reported always but is only *averaged into*
    ``overall`` when the case declares ``requires_external_sources``. A purely
    heuristic itinerary is allowed to score 0.0 there without being punished.
    """

    scores: dict[str, Any] = {
        "requirement_completeness": requirement_completeness(state, required_fields),
        "itinerary_executability": itinerary_executability(state),
        "budget_constraint": budget_constraint_score(state),
        "external_source_coverage": external_source_coverage_score(state),
    }

    gates = evaluate_hard_gates(state)
    scores["hard_gates"] = gates
    scores["hard_gates_passed"] = all(gates.values())

    scored_dimensions = [
        "requirement_completeness",
        "itinerary_executability",
        "budget_constraint",
    ]
    if requires_external_sources:
        scored_dimensions.append("external_source_coverage")

    scores["scored_dimensions"] = scored_dimensions
    scores["unbacked_realtime_claims"] = unbacked_realtime_claims(state)

    if not scores["hard_gates_passed"]:
        scores["overall"] = 0.0
    else:
        scores["overall"] = sum(scores[name] for name in scored_dimensions) / len(
            scored_dimensions
        )
    return scores
