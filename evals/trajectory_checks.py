"""L2 trajectory assertions.

These operate on the structured trajectory produced by
``app.observability.trace`` plus the final state — not on the final answer only.
That is what lets them catch "picked the wrong tool" and "left stale state
behind", which a result-level grader structurally cannot see.

Two kinds of assertions are evaluated:

- **Expectation checks** — driven by the scenario's ``expect`` block.
- **Invariants** — applied to every scenario regardless of what it expects.

Scenarios may declare ``negative_control: true`` together with
``expect_failures``; such a case passes only when *exactly* those checks fail.
That is how we prove an assertion still has teeth instead of trusting a green
run forever.

See ``docs/agent-eval-design.md`` (section 4, L2) for the design rationale.
"""

from __future__ import annotations

from typing import Any

from app.agents.graphs.travel_planner_graph import STEP_SEQUENCE
from evals.checks import CheckResult
from evals.harness import ScenarioOutcome


STEP_INDEX = {name: index for index, name in enumerate(STEP_SEQUENCE)}

#: Any call to these tools means money or a commitment is being made.
HIGH_RISK_TOOLS = frozenset({"generate_order_tool"})


def resolve_path(state: dict[str, Any], path: str) -> Any:
    """Resolve a dotted state path such as ``user_requirement.departure_city``."""

    current: Any = state
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
    return current


def _result(
    check_id: str,
    title: str,
    passed: bool,
    detail: str = "",
    data: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        layer="L2",
        title=title,
        passed=passed,
        detail=detail,
        data=data or {},
    )


def _tools_before(trace_tools: list[dict[str, Any]], target: str) -> list[str]:
    """Return the tool names that ran before the first call to ``target``."""

    names: list[str] = []
    for call in trace_tools:
        if call.get("name") == target:
            break
        names.append(str(call.get("name")))
    return names


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def invariant_trace_completed(outcome: ScenarioOutcome) -> CheckResult:
    passed = outcome.error is None
    return _result(
        "L2.trace_completed",
        "场景执行无异常",
        passed,
        detail=outcome.error or "OK",
    )


def invariant_no_duplicate_tool_calls(outcome: ScenarioOutcome) -> CheckResult:
    """A repeated ``(tool, args)`` pair is the signature of a degenerate loop."""

    seen: dict[str, int] = {}
    for call in outcome.record.get("tool_calls", []):
        key = f"{call.get('name')}|{call.get('args')}"
        seen[key] = seen.get(key, 0) + 1

    duplicates = {key: count for key, count in seen.items() if count > 1}
    return _result(
        "L2.no_duplicate_tool_calls",
        "无重复工具调用（循环检测）",
        not duplicates,
        detail="; ".join(f"{key} x{count}" for key, count in duplicates.items())
        if duplicates
        else "无重复 (name, args) 组合",
        data={"duplicates": duplicates},
    )


def invariant_one_step_per_turn(
    scenario: dict[str, Any], outcome: ScenarioOutcome
) -> CheckResult:
    """One turn may advance at most one planning step."""

    raw_start = (scenario.get("initial_state") or {}).get("current_step") or scenario.get("step")
    # Mirror the router's fallback: anything unknown starts at step 1.
    start = raw_start if raw_start in STEP_INDEX else STEP_SEQUENCE[0]
    end = outcome.final_state.get("current_step")
    if end not in STEP_INDEX:
        return _result(
            "L2.one_step_per_turn",
            "一轮一步不变量",
            False,
            detail=f"结束步骤非法: {end}",
        )

    delta = STEP_INDEX[end] - STEP_INDEX[start]
    if delta > 1:
        return _result(
            "L2.one_step_per_turn",
            "一轮一步不变量",
            False,
            detail=f"{start} -> {end} 前进了 {delta} 步",
            data={"start": start, "end": end, "delta": delta},
        )

    # If nothing advanced, some state tool must explain why (e.g. a query-only turn).
    advancing = [
        call
        for call in outcome.record.get("state_writes", [])
        if call.get("tool") not in {"check_current_progress"}
        and "current_step" in (call.get("fields") or [])
    ]
    if delta == 0 and advancing:
        return _result(
            "L2.one_step_per_turn",
            "一轮一步不变量",
            False,
            detail=(
                f"{advancing[0]['tool']} 写入了 current_step 但状态仍停留在 {start}"
            ),
            data={"writes": advancing},
        )

    return _result(
        "L2.one_step_per_turn",
        "一轮一步不变量",
        True,
        detail=f"{start} -> {end} (delta={delta})",
    )


def invariant_tools_in_candidate_set(
    scenario: dict[str, Any],
    outcome: ScenarioOutcome,
    candidate_tools: dict[str, list[str]],
) -> CheckResult:
    """Every called tool must belong to the step's static candidate list."""

    raw_step = (scenario.get("initial_state") or {}).get("current_step") or scenario.get("step")
    step = raw_step if raw_step in STEP_INDEX else STEP_SEQUENCE[0]
    allowed = set(candidate_tools.get(str(step), []))
    # Rollback helpers are reachable from the tool body, not only the prompt list.
    allowed |= {name for name in candidate_tools.get("__rollback__", [])}

    called = [call.get("name") for call in outcome.record.get("tool_calls", [])]
    unexpected = [name for name in called if allowed and name not in allowed]

    return _result(
        "L2.tools_in_candidate_set",
        "工具选择在候选集内",
        not unexpected,
        detail=f"越界工具: {sorted(set(unexpected))}" if unexpected else f"{len(called)} 次调用均在候选集内",
        data={"step": step, "allowed": sorted(allowed), "unexpected": unexpected},
    )


def invariant_approval_before_high_risk(outcome: ScenarioOutcome) -> CheckResult:
    """A high-risk action must be preceded by an approved approval request."""

    calls = outcome.record.get("tool_calls", [])
    risky = [call for call in calls if call.get("name") in HIGH_RISK_TOOLS]
    if not risky:
        return _result(
            "L2.approval_before_high_risk",
            "高风险动作前置审批",
            True,
            detail="本场景未触发高风险动作（不适用）",
        )

    for call in risky:
        if "request_action_approval" not in _tools_before(calls, str(call.get("name"))):
            return _result(
                "L2.approval_before_high_risk",
                "高风险动作前置审批",
                False,
                detail=f"{call.get('name')} 之前没有 request_action_approval 调用",
                data={"tool": call.get("name")},
            )

    decision = outcome.final_state.get("approval_decision") or {}
    if not decision.get("approved"):
        return _result(
            "L2.approval_before_high_risk",
            "高风险动作前置审批",
            False,
            detail="approval_decision.approved 不是 True",
            data={"approval_decision": decision},
        )

    return _result(
        "L2.approval_before_high_risk",
        "高风险动作前置审批",
        True,
        detail="审批已前置且结果为通过",
    )


def invariant_tool_args_match_trace(outcome: ScenarioOutcome) -> CheckResult:
    """The args the stub recorded must equal the args in the trace.

    Guards the trace itself: if this diverges, every parameter-fidelity
    assertion built on the trace is unreliable.
    """

    mismatches: list[str] = []
    trace_args = [
        (call.get("name"), call.get("args") or {})
        for call in outcome.record.get("tool_calls", [])
    ]
    stub_args = [(call["name"], call["args"]) for call in outcome.stub_calls]

    stub_only = [pair for pair in stub_args if pair not in trace_args]
    if stub_only:
        mismatches.append(f"stub 调用未出现在轨迹中: {stub_only[:3]}")

    return _result(
        "L2.trace_arg_consistency",
        "轨迹记录的工具参数与实参一致",
        not mismatches,
        detail="; ".join(mismatches) if mismatches else f"{len(trace_args)} 次调用一致",
    )


# ---------------------------------------------------------------------------
# Expectation-driven checks
# ---------------------------------------------------------------------------


def check_expectations(
    scenario: dict[str, Any], outcome: ScenarioOutcome
) -> list[CheckResult]:
    expect = scenario.get("expect") or {}
    results: list[CheckResult] = []
    calls = outcome.record.get("tool_calls", [])
    called_names = [call.get("name") for call in calls]

    # --- routing ---------------------------------------------------------
    if "routed_node" in expect:
        nodes = [step.get("node") for step in outcome.record.get("steps", [])]
        expected = expect["routed_node"]
        results.append(
            _result(
                "L2.routing",
                "路由落到期望节点",
                nodes == [expected],
                detail=f"实际节点 {nodes}，期望 [{expected}]",
                data={"nodes": nodes, "expected": expected},
            )
        )

    # --- tool selection --------------------------------------------------
    if "tools_called" in expect:
        missing = [name for name in expect["tools_called"] if name not in called_names]
        results.append(
            _result(
                "L2.tools_called",
                "应调用的工具都被调用",
                not missing,
                detail=f"缺失: {missing}" if missing else f"实际调用 {called_names}",
                data={"missing": missing, "called": called_names},
            )
        )

    if "tools_not_called" in expect:
        present = [name for name in expect["tools_not_called"] if name in called_names]
        results.append(
            _result(
                "L2.tools_not_called",
                "不应调用的工具未被调用",
                not present,
                detail=f"误调: {present}" if present else "OK",
                data={"unexpected": present},
            )
        )

    # --- parameter fidelity ----------------------------------------------
    for spec in expect.get("arg_fidelity") or []:
        tool_name = spec["tool"]
        actual_calls = [call for call in calls if call.get("name") == tool_name]
        if not actual_calls:
            results.append(
                _result(
                    "L2.arg_fidelity",
                    f"{tool_name} 参数保真",
                    False,
                    detail=f"{tool_name} 未被调用",
                )
            )
            continue

        mismatches: list[str] = []
        for actual in actual_calls:
            for arg_name, state_path in (spec.get("args") or {}).items():
                expected_value = resolve_path(outcome.final_state, state_path)
                actual_value = (actual.get("args") or {}).get(arg_name)
                if str(actual_value) != str(expected_value):
                    mismatches.append(
                        f"{arg_name}={actual_value!r} 但 state.{state_path}={expected_value!r}"
                    )

        results.append(
            _result(
                "L2.arg_fidelity",
                f"{tool_name} 参数保真",
                not mismatches,
                detail="; ".join(mismatches)
                if mismatches
                else f"{len(actual_calls)} 次调用的参数均与 state 一致",
                data={"mismatches": mismatches, "tool": tool_name},
            )
        )

    # --- final state -----------------------------------------------------
    if "final_step" in expect:
        actual_step = outcome.final_state.get("current_step")
        results.append(
            _result(
                "L2.final_step",
                "终点步骤符合预期",
                actual_step == expect["final_step"],
                detail=f"实际 {actual_step}，期望 {expect['final_step']}",
            )
        )

    for path in expect.get("state_null") or []:
        value = resolve_path(outcome.final_state, path)
        results.append(
            _result(
                "L2.state_null",
                f"回退后 {path} 已清空",
                value in (None, [], {}, ""),
                detail=f"{path}={value!r}",
            )
        )

    for path in expect.get("state_not_null") or []:
        value = resolve_path(outcome.final_state, path)
        results.append(
            _result(
                "L2.state_not_null",
                f"{path} 应保留",
                value not in (None, [], {}),
                detail=f"{path}={str(value)[:80]!r}",
            )
        )

    for path, expected_value in (expect.get("state_equals") or {}).items():
        value = resolve_path(outcome.final_state, path)
        results.append(
            _result(
                "L2.state_equals",
                f"{path} 等于期望值",
                value == expected_value,
                detail=f"{path}={value!r}，期望 {expected_value!r}",
            )
        )

    # --- cost ------------------------------------------------------------
    if "llm_calls_max" in expect:
        actual = outcome.record.get("cost", {}).get("llm_calls", 0)
        results.append(
            _result(
                "L2.llm_calls",
                "模型调用次数上限",
                actual <= expect["llm_calls_max"],
                detail=f"{actual} <= {expect['llm_calls_max']}",
            )
        )

    return results


def evaluate_scenario(
    scenario: dict[str, Any],
    outcome: ScenarioOutcome,
    candidate_tools: dict[str, list[str]],
) -> dict[str, Any]:
    """Run all invariants + expectations and decide whether the case passes."""

    checks: list[CheckResult] = [
        invariant_trace_completed(outcome),
        invariant_no_duplicate_tool_calls(outcome),
        invariant_one_step_per_turn(scenario, outcome),
        invariant_tools_in_candidate_set(scenario, outcome, candidate_tools),
        invariant_approval_before_high_risk(outcome),
        invariant_tool_args_match_trace(outcome),
    ]
    checks.extend(check_expectations(scenario, outcome))

    failed = [check for check in checks if not check.passed]
    failed_ids = sorted({check.check_id for check in failed})

    negative_control = bool(scenario.get("negative_control"))
    expect_failures = sorted(set(scenario.get("expect_failures") or []))
    known_gap = bool(scenario.get("known_gap"))
    known_gap_ref = str(scenario.get("known_gap_ref") or "")

    if negative_control:
        unexpected = [item for item in failed_ids if item not in expect_failures]
        missing = [item for item in expect_failures if item not in failed_ids]
        passed = not unexpected and not missing
        detail = (
            "负向对照：断言按预期捕获了问题"
            if passed
            else f"未按预期失败。多余失败={unexpected} 未触发={missing}"
        )
    else:
        passed = not failed
        detail = (
            "全部通过"
            if passed
            else "失败项: " + "; ".join(f"{check.check_id} ({check.detail})" for check in failed[:4])
        )

    if known_gap and not passed:
        for check in checks:
            if not check.passed:
                check.known_gap = True
                check.known_gap_ref = known_gap_ref

    return {
        "case_id": outcome.case_id,
        "passed": passed,
        "known_gap": known_gap and not passed,
        "known_gap_ref": known_gap_ref if known_gap and not passed else "",
        "negative_control": negative_control,
        "expect_failures": expect_failures,
        "summary": detail,
        "checks": [check.to_dict() for check in checks],
        "failed_checks": failed_ids,
    }
