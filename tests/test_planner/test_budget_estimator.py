from app.planner.budget_estimator import estimate_budget


def test_estimate_budget_returns_ranges_and_warnings():
    state = {
        "user_requirement": {
            "travel_days": 4,
            "adult_count": 2,
            "children_count": 1,
            "budget_max": 1000,
            "budget_level": "comfort",
        },
        "selected_transport": "flight",
        "selected_accommodation_types": ["star_hotel"],
        "selected_food_types": ["specialty"],
    }

    estimate = estimate_budget(state)
    legacy = estimate.to_legacy_breakdown()

    assert estimate.total.expected_amount > 0
    assert estimate.total.min_amount < estimate.total.expected_amount < estimate.total.max_amount
    assert legacy["transport"] > 0
    assert legacy["accommodation"] > 0
    assert legacy["warnings"]

