from app.planner.itinerary_planner import build_itinerary_plan


def test_build_itinerary_plan_returns_legacy_and_structured_shapes():
    state = {
        "user_requirement": {
            "travel_days": 3,
            "adult_count": 2,
            "children_count": 1,
            "travel_styles": ["culture", "food"],
        },
        "selected_destination": "西安",
        "selected_transport": "train",
        "selected_accommodation_types": ["economy_hotel"],
        "selected_food_types": ["local"],
    }

    plan = build_itinerary_plan(state)
    legacy_days = plan.to_legacy_days()

    assert plan.destination == "西安"
    assert len(plan.days) == 3
    assert len(legacy_days) == 3
    assert legacy_days[0]["activities"]
    assert legacy_days[0]["items"][0]["reason"]

