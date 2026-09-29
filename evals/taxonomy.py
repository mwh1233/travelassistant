"""Failure taxonomy — the fixed label set that makes failures comparable.

Why this exists
---------------
"成功率掉了 3 个点" cannot drive a fix. "``tool_argument_error`` 增加 12 例，
集中在日期字段" can. That only works if every failure gets a label from a
*versioned, closed* set — otherwise slice reports (§6 of D09) cut nothing but
noise.

Rules (D09 §4.5)
----------------
1. Every failed case gets **exactly one primary label** + optional secondary.
2. The label set itself is versioned; adding one needs a stated reason.
3. Labels are the precondition for slice reports.

The list is deliberately kept in the 15–25 band. ``check_id -> label`` mapping
is the machine-readable half; it lets the runner tag a failure automatically
instead of relying on a human to classify it.
"""

from __future__ import annotations

from typing import Any

#: Bump when labels are added/removed/renamed. Reports record the version so
#: cross-version comparisons are never silently made.
TAXONOMY_VERSION = "1.0"

#: category -> ordered label list. Kept explicit so the doc and code agree.
LABELS: dict[str, tuple[str, ...]] = {
    "outcome": (
        "state_mismatch",
        "required_field_missing",
        "schema_invalid",
        "constraint_violation",
        "unsupported_claim",
    ),
    "trajectory": (
        "tool_selection_error",
        "tool_argument_error",
        "unnecessary_tool_call",
        "loop_detected",
        "silent_tool_failure",
        "recovery_failure",
        "unauthorized_action",
    ),
    "turn": (
        "intent_drift",
        "constraint_forgotten",
        "prompt_injection_success",
        "policy_violation",
        "missed_clarification",
    ),
    "runtime": (
        "timeout",
        "cost_limit_exceeded",
        "latency_limit_exceeded",
        "nondeterministic_failure",
        "scenario_error",
    ),
}

ALL_LABELS: frozenset[str] = frozenset(
    label for labels in LABELS.values() for label in labels
)

#: ``check_id -> primary label``. A check that fails tags the case with its
#: label; the first failing check (in evaluation order) wins the primary slot.
CHECK_LABELS: dict[str, str] = {
    # --- L0 ---
    "L0.requires_matrix": "state_mismatch",
    "L0.route_robustness": "state_mismatch",
    "L0.rollback_cleanup": "state_mismatch",
    "L0.state_write_contract": "state_mismatch",
    "L0.enum_validation": "constraint_violation",
    "L0.budget_boundaries": "constraint_violation",
    "L0.tool_precondition_gate": "silent_tool_failure",
    "L0.one_step_per_turn": "state_mismatch",
    "L0.schema_validation": "schema_invalid",
    "L0.travel_days_clamp": "constraint_violation",
    "L0.capability_vocabulary": "tool_selection_error",
    "L0.dataset_schema": "schema_invalid",
    "L0.judge_gold_schema": "schema_invalid",
    "L0.feedback_schema": "schema_invalid",
    # --- L2 trajectory ---
    "L2.trace_completed": "scenario_error",
    "L2.no_duplicate_tool_calls": "loop_detected",
    "L2.one_step_per_turn": "state_mismatch",
    "L2.tools_in_candidate_set": "tool_selection_error",
    "L2.approval_before_high_risk": "unauthorized_action",
    "L2.trace_arg_consistency": "tool_argument_error",
    "L2.routing": "state_mismatch",
    "L2.tools_called": "tool_selection_error",
    "L2.tools_not_called": "unnecessary_tool_call",
    "L2.arg_fidelity": "tool_argument_error",
    "L2.final_step": "state_mismatch",
    "L2.state_null": "state_mismatch",
    "L2.state_not_null": "required_field_missing",
    "L2.state_equals": "state_mismatch",
    "L2.llm_calls": "cost_limit_exceeded",
    "L2.step_budget": "cost_limit_exceeded",
    "L2.latency_budget": "latency_limit_exceeded",
    "L2.replay_miss": "nondeterministic_failure",
    # --- L3 result ---
    "L1.result_hard_gate": "schema_invalid",
    "L1.unbacked_realtime_claim": "unsupported_claim",
    "L1.budget_constraint": "constraint_violation",
    "L1.requirement_completeness": "required_field_missing",
    "L1.itinerary_executability": "state_mismatch",
    "L1.external_source_coverage": "unsupported_claim",
    # --- L1 capability probes ---
    "L1.capability_available": "tool_selection_error",
    "L1.forbidden_capability_available": "unauthorized_action",
    "L1.required_args_present": "tool_argument_error",
    "L1.rag_entity_recall": "unsupported_claim",
    "L1.rag_source_coverage": "unsupported_claim",
    "L1.rag_fallback": "recovery_failure",
    # --- L3 per-turn ---
    "L3.turn_completed": "scenario_error",
    "L3.intent_preserved": "intent_drift",
    "L3.constraint_preserved": "constraint_forgotten",
    "L3.state_consistency_after_revision": "state_mismatch",
    "L3.injection_resisted": "prompt_injection_success",
    "L3.policy_respected": "policy_violation",
    "L3.clarification_asked": "missed_clarification",
    # --- fault injection (W6) ---
    # A fault scenario that is "not injected" measured nothing; an unrecovered
    # fault that still advanced the state machine is silent data corruption.
    "L6.fault_injected": "scenario_error",
    "L6.fault_surfaced": "silent_tool_failure",
    "L6.no_fabrication": "unsupported_claim",
    "L6.recovery_attempted": "recovery_failure",
    "L6.bounded_retry": "loop_detected",
    "L6.graceful_degradation": "state_mismatch",
    # --- judge / online ---
    "L4.judge_kappa": "nondeterministic_failure",
    "L4.judge_rubric_missing": "schema_invalid",
    # --- online feedback loop (W5) ---
    # A promoted case that lost its reviewer/correct-behaviour provenance is a
    # *data* defect: the row is structurally invalid, not behaviourally wrong.
    "L5.feedback_provenance": "schema_invalid",
}


def label_for(check_id: str) -> str:
    """Return the taxonomy label for a check, defaulting to ``state_mismatch``."""

    if check_id in CHECK_LABELS:
        return CHECK_LABELS[check_id]
    # Prefix-based fallbacks so a new check never lands unlabelled.
    if check_id.startswith("L0."):
        return "state_mismatch"
    if check_id.startswith(("L2.", "L3.")):
        return "state_mismatch"
    if check_id.startswith("L1."):
        return "state_mismatch"
    if check_id.startswith("L5."):
        return "schema_invalid"
    if check_id.startswith("L6."):
        return "recovery_failure"
    return "scenario_error"


def categorise(failed_check_ids: list[str]) -> dict[str, Any]:
    """Turn a case's failed checks into ``{primary, secondary, categories}``.

    The primary label is the one attached to the *first* failure in evaluation
    order — that is the closest thing to "root cause" a static mapping can give.
    Secondary labels are deduplicated and used for clustering, never for
    attribution.
    """

    labels: list[str] = []
    for check_id in failed_check_ids:
        label = label_for(check_id)
        if label not in labels:
            labels.append(label)

    primary = labels[0] if labels else ""
    secondary = labels[1:]
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "primary": primary,
        "secondary": secondary,
        "category": category_of(primary),
    }


def category_of(label: str) -> str:
    for category, labels in LABELS.items():
        if label in labels:
            return category
    return ""


def validate_taxonomy() -> list[str]:
    """Return problems with the taxonomy itself (used by a unit test)."""

    problems: list[str] = []
    total = len(ALL_LABELS)
    if not 15 <= total <= 25:
        problems.append(f"标签总数 {total} 不在 15–25 区间")
    for check_id, label in CHECK_LABELS.items():
        if label not in ALL_LABELS:
            problems.append(f"{check_id} 映射到未定义标签 {label!r}")
    seen: dict[str, str] = {}
    for category, labels in LABELS.items():
        for label in labels:
            if label in seen:
                problems.append(f"标签 {label!r} 同时属于 {seen[label]} 与 {category}")
            seen[label] = category
    return problems
