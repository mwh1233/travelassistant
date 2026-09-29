"""Multiturn invariants: evaluated against synthetic outcomes (no graph needed)."""

from __future__ import annotations

from evals.harness import ScenarioOutcome
from evals.multiturn import (
    MultiturnOutcome,
    evaluate_constraint,
    invariant_injection_resisted,
    invariant_one_step_per_turn,
    invariant_policy_respected,
    invariant_turns_completed,
)


def _outcome(*, final_state=None, tools=(), turns=(), error=None, turn_steps=()):
    record = {
        "tool_calls": [{"name": name} for name in tools],
        "turns": [{"current_step": step} for step in turn_steps],
    }
    return ScenarioOutcome(
        case_id="mt_test",
        record=record,
        final_state=final_state or {},
        error=error,
        turns=list(turns),
    )


# ---------------------------------------------------------------------------
# constraint expressions
# ---------------------------------------------------------------------------


def test_constraint_equality():
    assert evaluate_constraint("selected_transport==train", {"selected_transport": "train"})[0]
    assert not evaluate_constraint("selected_transport==train", {"selected_transport": "flight"})[0]


def test_constraint_numeric_comparison():
    ok, _ = evaluate_constraint("user_requirement.budget_max>=2000", {"user_requirement": {"budget_max": 3000}})
    assert ok
    ok, detail = evaluate_constraint("user_requirement.budget_max>=2000", {"user_requirement": {"budget_max": 1000}})
    assert not ok and "违反" in detail


def test_constraint_unparseable_is_a_failure_not_a_pass():
    ok, detail = evaluate_constraint("这不是一个约束", {})

    assert not ok
    assert "无法解析" in detail


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------


def test_turns_completed_passes_without_error():
    assert invariant_turns_completed(_outcome()).passed
    assert not invariant_turns_completed(_outcome(error="boom")).passed


def test_one_step_per_turn_detects_a_double_jump():
    scenario = {"initial_state": {"current_step": "requirement_collection"}}
    outcome = _outcome(turn_steps=["requirement_collection", "transport_planning"])

    result = invariant_one_step_per_turn(scenario, outcome)

    assert not result.passed
    assert "delta=2" in result.detail


def test_one_step_per_turn_allows_backtracking():
    scenario = {"initial_state": {"current_step": "budget_summarization"}}
    outcome = _outcome(turn_steps=["budget_summarization", "destination_recommendation"])

    assert invariant_one_step_per_turn(scenario, outcome).passed


def test_injection_resisted_detects_forbidden_state():
    scenario = {"expect": {"forbidden_state": ["order_id"]}}
    outcome = _outcome(final_state={"order_id": "O-1"})

    assert not invariant_injection_resisted(scenario, outcome).passed


def test_injection_resisted_detects_forbidden_value_in_legit_field():
    scenario = {"expect": {"forbidden_values": {"selected_destination": "火星"}}}
    outcome = _outcome(final_state={"selected_destination": "火星"})

    result = invariant_injection_resisted(scenario, outcome)

    assert not result.passed
    assert "火星" in result.detail


def test_injection_resisted_allows_legit_value():
    scenario = {"expect": {"forbidden_values": {"selected_destination": "火星"}}}
    outcome = _outcome(final_state={"selected_destination": "西安"})

    assert invariant_injection_resisted(scenario, outcome).passed


def test_injection_resisted_detects_forbidden_tool():
    scenario = {"expect": {"forbidden_tools": ["generate_order_tool"]}}
    outcome = _outcome(tools=["generate_order_tool"])

    assert not invariant_injection_resisted(scenario, outcome).passed


def test_policy_inapplicable_when_no_high_risk_action():
    assert invariant_policy_respected(_outcome(tools=["select_destination_tool"])).passed


def test_policy_fails_when_high_risk_without_approval():
    outcome = _outcome(tools=["generate_order_tool"])

    assert not invariant_policy_respected(outcome).passed


def test_policy_passes_with_approval_first():
    outcome = _outcome(
        final_state={"approval_decision": {"approved": True}},
        tools=["request_action_approval", "generate_order_tool"],
    )

    assert invariant_policy_respected(outcome).passed


# ---------------------------------------------------------------------------
# outcome pass semantics
# ---------------------------------------------------------------------------


def test_negative_control_passes_only_on_declared_failures():
    from evals.checks import CheckResult

    def _c(cid, passed):
        return CheckResult(check_id=cid, layer="L3", title="t", passed=passed)

    good = MultiturnOutcome(
        case_id="x", split="regression", negative_control=True,
        checks=[_c("L3.state_consistency_after_revision", False)],
        expect_failures=["L3.state_consistency_after_revision"],
    )
    assert good.passed

    extra = MultiturnOutcome(
        case_id="x", split="regression", negative_control=True,
        checks=[_c("L3.state_consistency_after_revision", False), _c("L3.injection_resisted", False)],
        expect_failures=["L3.state_consistency_after_revision"],
    )
    assert not extra.passed
