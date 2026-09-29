from evals.graders import (
    evaluate_hard_gates,
    external_source_coverage_score,
    grade_travel_state,
    unbacked_realtime_claims,
)


def _planner_only_state() -> dict:
    """The shape ``build_itinerary_plan()`` actually produces today."""

    return {
        "user_requirement": {
            "departure_city": "北京",
            "destination": "西安",
            "travel_days": 3,
            "budget_max": 3000,
            "adult_count": 2,
            "children_count": 0,
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
                            "sources": [{"name": "planner", "source_type": "planner"}],
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
                        "source": {"name": "estimator", "source_type": "heuristic"},
                    }
                }
            ],
        },
    }


def test_grade_travel_state_scores_structured_outputs():
    scores = grade_travel_state(
        _planner_only_state(),
        ["departure_city", "destination", "travel_days", "budget_max"],
    )

    assert scores["requirement_completeness"] == 1.0
    assert scores["itinerary_executability"] == 1.0
    assert scores["budget_constraint"] == 1.0
    assert scores["hard_gates_passed"] is True
    assert scores["overall"] == 1.0


def test_external_source_coverage_excludes_self_produced_sources():
    """Regression: the old metric returned 1.0 for planner-only output."""

    state = _planner_only_state()

    assert external_source_coverage_score(state) == 0.0

    state["structured_itinerary"]["days"][0]["items"][0]["sources"].append(
        {"name": "amap", "source_type": "map", "url": "https://example.com/poi"}
    )
    assert external_source_coverage_score(state) == 0.5

    state["structured_budget"]["items"][0]["amount"]["source"] = {
        "name": "amap",
        "source_type": "map",
    }
    assert external_source_coverage_score(state) == 1.0


def test_external_source_coverage_is_opt_in_for_overall():
    state = _planner_only_state()

    without = grade_travel_state(state, [], requires_external_sources=False)
    with_required = grade_travel_state(state, [], requires_external_sources=True)

    assert without["overall"] == 1.0
    assert with_required["overall"] == 0.75
    assert "external_source_coverage" not in without["scored_dimensions"]
    assert "external_source_coverage" in with_required["scored_dimensions"]


def test_unbacked_realtime_claim_trips_the_hard_gate():
    state = _planner_only_state()
    state["structured_itinerary"]["days"][0]["items"][0]["title"] = (
        "西安 weather forecast for tomorrow"
    )

    assert unbacked_realtime_claims(state) == ["西安 weather forecast for tomorrow"]
    assert evaluate_hard_gates(state)["no_unbacked_realtime_claim"] is False

    scores = grade_travel_state(state, [])
    assert scores["hard_gates_passed"] is False
    assert scores["overall"] == 0.0


def test_hard_gate_zeroes_overall_instead_of_averaging():
    state = _planner_only_state()
    state["structured_itinerary"] = {"days": []}

    scores = grade_travel_state(state, [])

    assert scores["hard_gates"]["itinerary_schema_ok"] is False
    assert scores["overall"] == 0.0
