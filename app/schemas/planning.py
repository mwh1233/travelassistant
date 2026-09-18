"""Structured travel planning schemas."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceReference(BaseModel):
    """Source metadata for planner and tool results."""

    name: str
    url: str | None = None
    source_type: str = "estimated"
    updated_at: str | None = None
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class MoneyRange(BaseModel):
    """A conservative money interval."""

    min_amount: float = Field(ge=0)
    expected_amount: float = Field(ge=0)
    max_amount: float = Field(ge=0)
    currency: str = "CNY"
    estimated: bool = True
    source: SourceReference | None = None


class LocationPoint(BaseModel):
    """Location information used by map/timeline UI."""

    name: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class PoiOption(BaseModel):
    """Candidate point of interest."""

    name: str
    category: str = "attraction"
    location: LocationPoint
    estimated_cost: MoneyRange | None = None
    duration_minutes: int = Field(default=120, ge=15)
    opening_hours: str | None = None
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)


class ItineraryItem(BaseModel):
    """A concrete activity in a day plan."""

    title: str
    time_slot: Literal["morning", "afternoon", "evening", "flex"]
    location: LocationPoint
    duration_minutes: int = Field(default=120, ge=15)
    estimated_cost: MoneyRange | None = None
    transport_note: str | None = None
    reason: str
    sources: list[SourceReference] = Field(default_factory=list)


class ItineraryDayPlan(BaseModel):
    """A single day plan."""

    day_number: int = Field(ge=1)
    theme: str
    items: list[ItineraryItem]
    meals: list[str] = Field(default_factory=list)
    accommodation: str | None = None
    plan_b: str | None = None
    intensity: Literal["low", "medium", "high"] = "medium"


class ItineraryPlan(BaseModel):
    """Full itinerary plan."""

    destination: str
    days: list[ItineraryDayPlan]
    summary: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    assumptions: list[str] = Field(default_factory=list)

    def to_legacy_days(self) -> list[dict[str, Any]]:
        """Return the legacy shape used by the current TravelState."""

        legacy_days: list[dict[str, Any]] = []
        for day in self.days:
            legacy_days.append(
                {
                    "day_number": day.day_number,
                    "activities": [item.title for item in day.items],
                    "meals": day.meals,
                    "accommodation": day.accommodation or "selected accommodation",
                    "theme": day.theme,
                    "plan_b": day.plan_b,
                    "intensity": day.intensity,
                    "items": [item.model_dump() for item in day.items],
                }
            )
        return legacy_days


class BudgetItem(BaseModel):
    """Budget line item."""

    category: str
    label: str
    amount: MoneyRange
    note: str | None = None


class BudgetEstimate(BaseModel):
    """Structured budget estimate."""

    items: list[BudgetItem]
    total: MoneyRange
    per_person: MoneyRange
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_legacy_breakdown(self) -> dict[str, Any]:
        """Return the legacy budget dict with extra structured fields."""

        by_category = {
            item.category: item.amount.expected_amount
            for item in self.items
        }
        return {
            "transport": by_category.get("transport", 0.0),
            "accommodation": by_category.get("accommodation", 0.0),
            "food": by_category.get("food", 0.0),
            "attractions": by_category.get("attractions", 0.0),
            "misc": by_category.get("misc", 0.0),
            "total": self.total.expected_amount,
            "range": {
                "min": self.total.min_amount,
                "expected": self.total.expected_amount,
                "max": self.total.max_amount,
                "currency": self.total.currency,
            },
            "per_person": self.per_person.model_dump(),
            "items": [item.model_dump() for item in self.items],
            "assumptions": self.assumptions,
            "warnings": self.warnings,
        }
