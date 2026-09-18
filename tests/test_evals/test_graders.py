from evals.graders import grade_travel_state


def test_grade_travel_state_scores_structured_outputs():
    state = {
        "user_requirement": {
            "departure_city": "北京",
            "destination": "西安",
            "travel_days": 3,
            "budget_max": 3000,
            "adult_count": 2,
        },
        "structured_itinerary": {
            "days": [
                {
                    "items": [
                        {
                            "title": "西安 City museum",
                            "time_slot": "morning",
                            "duration_minutes": 120,
                            "reason": "culture",
                            "sources": [{"name": "planner"}],
                        }
                    ]
                }
            ]
        },
        "structured_budget": {
            "total": {"expected_amount": 4000},
            "items": [
                {
                    "amount": {
                        "source": {"name": "estimator"},
                    }
                }
            ],
        },
    }

    scores = grade_travel_state(
        state,
        ["departure_city", "destination", "travel_days", "budget_max"],
    )

    assert scores["requirement_completeness"] == 1.0
    assert scores["itinerary_executability"] == 1.0
    assert scores["source_coverage"] == 1.0
    assert scores["overall"] > 0.8

