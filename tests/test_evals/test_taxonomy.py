"""Taxonomy: the failure label set must stay closed, versioned and complete."""

from __future__ import annotations

from evals import taxonomy


def test_taxonomy_is_self_consistent():
    assert taxonomy.validate_taxonomy() == []


def test_label_count_is_in_the_documented_band():
    assert 15 <= len(taxonomy.ALL_LABELS) <= 25


def test_label_for_known_and_unknown():
    assert taxonomy.label_for("L2.tools_called") == "tool_selection_error"
    # Prefix fallback: a brand-new L3 check must never land unlabelled.
    assert taxonomy.label_for("L3.some_new_check") == "state_mismatch"
    assert taxonomy.label_for("totally.unknown") == "scenario_error"


def test_categorise_picks_first_failure_as_primary():
    result = taxonomy.categorise(["L2.tools_called", "L2.arg_fidelity", "L2.tools_called"])

    assert result["primary"] == "tool_selection_error"
    assert result["secondary"] == ["tool_argument_error"]
    assert result["category"] == "trajectory"
    assert result["taxonomy_version"] == taxonomy.TAXONOMY_VERSION


def test_categorise_on_empty_is_safe():
    result = taxonomy.categorise([])

    assert result["primary"] == ""
    assert result["category"] == ""


def test_category_of_round_trips_every_label():
    for category, labels in taxonomy.LABELS.items():
        for label in labels:
            assert taxonomy.category_of(label) == category
