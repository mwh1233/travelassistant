"""L0 deterministic assertions.

These checks need **no model and no network**: every subject under test is a
pure function or a tool whose state transition can be replayed by hand. That
makes them the cheapest layer to run on every commit.

Run as a library::

    from evals.checks import run_l0_checks
    results = await run_l0_checks()

or as a pytest suite (``tests/test_evals/test_checks.py``).

See ``docs/agent-eval-design.md`` (section 4, L0) for the design rationale.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agents.graphs.travel_planner_graph import (
    STEP_SEQUENCE,
    _missing_requirements,
    _route_current_step,
)
from app.agents.handoffs.step_config import get_step_config
from app.core.state import TravelState
from app.mcp_core.registry import KNOWN_TOOL_VOCABULARY, resolve_tool_id
from app.planner.budget_estimator import estimate_budget
from app.planner.itinerary_planner import build_itinerary_plan
from app.schemas.planning import BudgetEstimate, ItineraryPlan
from app.tools import state_transition as st
from app.tools.planning_tools import generate_itinerary_tool, summarize_budget_tool


DATASET_DIR = Path(__file__).resolve().parent / "datasets"

#: Fields that every state-transition tool is allowed to write regardless of
#: which step it belongs to.
ALWAYS_WRITABLE = {"messages", "current_step"}


@dataclass
class CheckResult:
    """One assertion outcome."""

    check_id: str
    layer: str
    title: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: True when the failure is a *pre-existing, documented* gap rather than a
    #: regression. The runner reports these separately so a known gap does not
    #: mask a new break.
    known_gap: bool = False
    known_gap_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "layer": self.layer,
            "title": self.title,
            "passed": self.passed,
            "known_gap": self.known_gap,
            "known_gap_ref": self.known_gap_ref,
            "detail": self.detail,
            "data": self.data,
        }


class FakeRuntime:
    """Duck-typed stand-in for ``ToolRuntime``.

    The state-transition tools only touch ``runtime.state`` and
    ``runtime.tool_call_id``, which lets us replay real tool contracts without
    spinning up a graph or a model.
    """

    def __init__(self, state: dict[str, Any] | None = None, tool_call_id: str = "call-0"):
        self.state = state or {}
        self.tool_call_id = tool_call_id
        self.context = None
        self.config = {}
        self.store = None


def _update_of(command: Any) -> dict[str, Any]:
    update = getattr(command, "update", None)
    return update if isinstance(update, dict) else {}


def _message_text(command: Any) -> str:
    update = _update_of(command)
    for message in update.get("messages") or []:
        content = getattr(message, "content", None)
        if content:
            return str(content)
    return ""


# ---------------------------------------------------------------------------
# Sample invocations: the canonical way to drive each state tool by hand.
# ---------------------------------------------------------------------------

FULL_REQUIREMENT_KWARGS = dict(
    departure_city="北京",
    departure_date="2026-10-01",
    travel_days=4,
    budget_min=2500,
    budget_max=3500,
    travel_styles=["culture"],
    adult_count=2,
    children_count=1,
    destination="西安",
)


def _sample_state() -> dict[str, Any]:
    """A fully populated post-onboarding state."""

    return {
        "current_step": "order_generation",
        "user_requirement": {
            "departure_city": "北京",
            "destination": "西安",
            "departure_date": "2026-10-01",
            "travel_days": 4,
            "adult_count": 2,
            "children_count": 1,
            "budget_min": 2500.0,
            "budget_max": 3500.0,
            "budget_level": "comfort",
            "travel_styles": ["culture"],
            "special_needs": None,
        },
        "selected_destination": "西安",
        "selected_transport": "train",
        "selected_accommodation_types": ["star_hotel"],
        "selected_food_types": ["local"],
        "destination_options": [{"name": "西安"}],
        "transport_options": [{"details": "G87"}],
        "accommodation_options": [{"name": "某酒店"}],
        "food_options": [{"type": "local"}],
        "itinerary": [{"day_number": 1}],
        "structured_itinerary": {"days": [{"day_number": 1, "items": []}]},
        "budget": {"total": 1234.0},
        "structured_budget": {"total": {"expected_amount": 1234.0}, "items": [{}]},
        "order_id": "ORDER-ABCD1234",
        "source_references": [{"name": "amap"}],
        "user_id": "u-eval",
        "session_id": "s-eval",
    }


#: ``tool_name -> (callable, extra_kwargs)`` used to exercise write contracts.
STATE_TOOL_SAMPLES: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {
    "record_requirement_tool": (st.record_requirement_tool.func, dict(FULL_REQUIREMENT_KWARGS)),
    "select_destination_tool": (st.select_destination_tool.func, {"destination": "西安"}),
    "select_transport_tool": (st.select_transport_tool.func, {"transport_type": "train"}),
    "select_accommodation_tool": (
        st.select_accommodation_tool.func,
        {"accommodation_types": ["star_hotel"]},
    ),
    "select_food_tool": (st.select_food_tool.func, {"food_types": ["local"]}),
    "generate_itinerary_tool": (generate_itinerary_tool.func, {}),
    "summarize_budget_tool": (summarize_budget_tool.func, {}),
    "generate_order_tool": (st.generate_order_tool.func, {}),
}

#: Step index each state tool is expected to be called from.
TOOL_OWNER_STEP = {
    "record_requirement_tool": "requirement_collection",
    "select_destination_tool": "destination_recommendation",
    "select_transport_tool": "transport_planning",
    "select_accommodation_tool": "accommodation_planning",
    "select_food_tool": "food_planning",
    "generate_itinerary_tool": "itinerary_generation",
    "summarize_budget_tool": "budget_summarization",
    "generate_order_tool": "order_generation",
}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def check_step_requires_matrix() -> CheckResult:
    """Every subset of ``requires`` must be reported exactly."""

    step_config = await get_step_config()
    failures: list[str] = []
    combinations = 0

    for step_name in STEP_SEQUENCE:
        requires = list(step_config[step_name].get("requires", []))
        for size in range(len(requires) + 1):
            for subset in itertools.combinations(requires, size):
                combinations += 1
                state = {name: f"value::{name}" for name in subset}
                missing = _missing_requirements(step_config[step_name], state)
                expected = [name for name in requires if name not in subset]
                if sorted(missing) != sorted(expected):
                    failures.append(
                        f"{step_name}: subset={sorted(subset)} "
                        f"got={sorted(missing)} expected={sorted(expected)}"
                    )

                if subset:
                    # An explicitly-None field counts as missing too.
                    nulled = subset[0]
                    state_with_null = dict(state, **{nulled: None})
                    missing_null = _missing_requirements(step_config[step_name], state_with_null)
                    if nulled not in missing_null:
                        failures.append(
                            f"{step_name}: field {nulled}=None was not treated as missing"
                        )

    passed = not failures
    return CheckResult(
        check_id="L0.requires_matrix",
        layer="L0",
        title="步骤前置条件矩阵（requires 全子集）",
        passed=passed,
        detail=(
            f"{combinations} 个组合全部命中" if passed else "; ".join(failures[:5])
        ),
        data={"combinations": combinations, "failures": failures[:20]},
    )


async def check_route_robustness() -> CheckResult:
    """Routing must be total: known step -> itself, anything else -> step 1."""

    failures: list[str] = []
    for step in STEP_SEQUENCE:
        routed = _route_current_step({"current_step": step})  # type: ignore[arg-type]
        if routed != step:
            failures.append(f"{step} -> {routed}")

    for unknown in ("unknown_step", "", None):
        state = {} if unknown is None else {"current_step": unknown}
        routed = _route_current_step(state)  # type: ignore[arg-type]
        if routed != STEP_SEQUENCE[0]:
            failures.append(f"{unknown!r} -> {routed} (expected requirement_collection)")

    return CheckResult(
        check_id="L0.route_robustness",
        layer="L0",
        title="路由健壮性（未知 step 必须回落到第一步）",
        passed=not failures,
        detail="; ".join(failures) if failures else "全部 8 步 + 3 个非法输入均符合预期",
        data={"failures": failures},
    )


async def check_rollback_cleanup_matrix() -> CheckResult:
    """Rolling back must clear every field owned by the target step and later."""

    failures: list[str] = []
    cases = 0
    targets = [step for step in st.ALL_STEPS if step != "order_generation"]

    for target in targets:
        target_index = st.ALL_STEPS.index(target)
        state = _sample_state()
        state["current_step"] = "order_generation"

        command = st.go_back_to_step.func(
            target_step=target,
            reason="eval",
            clear_subsequent_data=True,
            runtime=FakeRuntime(state),
        )
        update = _update_of(command)
        cases += 1

        if update.get("current_step") != target:
            failures.append(f"{target}: current_step={update.get('current_step')}")

        # Fields owned by the target step itself and every later step must be cleared.
        for step in st.ALL_STEPS[target_index:]:
            for field_name in st.STEP_STATE_FIELDS.get(step, []):
                if update.get(field_name, "MISSING") is not None:
                    failures.append(
                        f"{target}: field {field_name} (owned by {step}) was not cleared"
                    )

        # Fields owned by earlier steps must survive untouched.
        for step in st.ALL_STEPS[:target_index]:
            for field_name in st.STEP_STATE_FIELDS.get(step, []):
                if field_name in update:
                    failures.append(
                        f"{target}: earlier field {field_name} (owned by {step}) was cleared"
                    )

        # clear_subsequent_data=False must be a no-op for data fields.
        no_clear = st.go_back_to_step.func(
            target_step=target,
            reason="eval",
            clear_subsequent_data=False,
            runtime=FakeRuntime(_sample_state()),
        )
        no_clear_update = _update_of(no_clear)
        data_fields = set(no_clear_update) - ALWAYS_WRITABLE
        if data_fields:
            failures.append(
                f"{target}: clear_subsequent_data=False still cleared {sorted(data_fields)}"
            )

    return CheckResult(
        check_id="L0.rollback_cleanup",
        layer="L0",
        title="回退清理矩阵（target 及其之后所有步骤字段必须清空）",
        passed=not failures,
        detail=f"{cases} 个回退目标" + (f" 存在问题: {'; '.join(failures[:5])}" if failures else " 全部符合契约"),
        data={"cases": cases, "failures": failures[:20]},
    )


async def check_state_write_contract() -> CheckResult:
    """Written keys must be declared in ``TravelState`` and covered by rollback.

    This is the assertion that catches two real classes of bug: writing a field
    the state schema does not declare (silently dropped by LangGraph), and
    writing a field that rollback never clears (stale state).
    """

    declared = set(TravelState.__annotations__.keys())
    # AgentState contributes these via inheritance; they are always legal.
    declared |= set(getattr(TravelState, "__required_keys__", set()))
    declared |= {"messages", "current_step"}

    failures: list[str] = []
    checked: list[str] = []

    for tool_name, (func, kwargs) in STATE_TOOL_SAMPLES.items():
        owner = TOOL_OWNER_STEP[tool_name]
        owner_index = st.ALL_STEPS.index(owner)
        cleaned = {
            name
            for step in st.ALL_STEPS[owner_index:]
            for name in st.STEP_STATE_FIELDS.get(step, [])
        }
        command = func(runtime=FakeRuntime(_sample_state()), **kwargs)
        written = set(_update_of(command)) - ALWAYS_WRITABLE
        checked.append(tool_name)

        undeclared = written - declared
        if undeclared:
            failures.append(f"{tool_name}: 写入未声明字段 {sorted(undeclared)}")

        uncovered = written - cleaned
        if uncovered:
            failures.append(
                f"{tool_name}: 写入 {sorted(uncovered)} 但回退清理矩阵未覆盖（将残留脏数据）"
            )

    return CheckResult(
        check_id="L0.state_write_contract",
        layer="L0",
        title="状态写入契约（已声明 + 可被回退清理）",
        passed=not failures,
        detail="; ".join(failures[:6]) if failures else f"{len(checked)} 个状态工具全部合规",
        data={"checked": checked, "failures": failures},
    )


async def check_enum_and_format_validation() -> CheckResult:
    """Illegal enums / dates / counts must be rejected without state mutation."""

    failures: list[str] = []
    cases = 0

    def expect_reject(label: str, command: Any, forbidden_key: str) -> None:
        nonlocal cases
        cases += 1
        update = _update_of(command)
        if forbidden_key in update:
            failures.append(f"{label}: 非法输入仍写入了 {forbidden_key}")
        if not _message_text(command):
            failures.append(f"{label}: 非法输入未返回任何提示消息")

    def expect_accept(label: str, command: Any, expected_key: str, next_step: str | None) -> None:
        nonlocal cases
        cases += 1
        update = _update_of(command)
        if expected_key not in update:
            failures.append(f"{label}: 合法输入未写入 {expected_key}")
        if next_step and update.get("current_step") != next_step:
            failures.append(
                f"{label}: current_step={update.get('current_step')} 期望 {next_step}"
            )

    runtime_state = _sample_state()

    # --- date format -----------------------------------------------------
    expect_reject(
        "record_requirement_tool(2026/10/01)",
        st.record_requirement_tool.func(
            **dict(FULL_REQUIREMENT_KWARGS, departure_date="2026/10/01"),
            runtime=FakeRuntime(runtime_state),
        ),
        "user_requirement",
    )
    expect_reject(
        "record_requirement_tool(2026-02-30)",
        st.record_requirement_tool.func(
            **dict(FULL_REQUIREMENT_KWARGS, departure_date="2026-02-30"),
            runtime=FakeRuntime(runtime_state),
        ),
        "user_requirement",
    )
    expect_accept(
        "record_requirement_tool(2026-10-01)",
        st.record_requirement_tool.func(
            **FULL_REQUIREMENT_KWARGS, runtime=FakeRuntime(runtime_state)
        ),
        "user_requirement",
        "destination_recommendation",
    )

    # --- party size ------------------------------------------------------
    for label, kwargs in (
        ("adult_count=0", dict(adult_count=0)),
        ("children_count=-1", dict(children_count=-1)),
    ):
        expect_reject(
            f"record_requirement_tool({label})",
            st.record_requirement_tool.func(
                **dict(FULL_REQUIREMENT_KWARGS, **kwargs), runtime=FakeRuntime(runtime_state)
            ),
            "user_requirement",
        )

    # --- transport enum --------------------------------------------------
    expect_reject(
        "select_transport_tool(rocket)",
        st.select_transport_tool.func(
            transport_type="rocket", runtime=FakeRuntime(runtime_state)
        ),
        "selected_transport",
    )
    for value, next_step in (("flight", "accommodation_planning"), ("train", "accommodation_planning"), ("driving", "accommodation_planning")):
        expect_accept(
            f"select_transport_tool({value})",
            st.select_transport_tool.func(
                transport_type=value, runtime=FakeRuntime(runtime_state)
            ),
            "selected_transport",
            next_step,
        )

    # --- accommodation / food enums --------------------------------------
    expect_reject(
        "select_accommodation_tool(castle)",
        st.select_accommodation_tool.func(
            accommodation_types=["castle"], runtime=FakeRuntime(runtime_state)
        ),
        "selected_accommodation_types",
    )
    expect_reject(
        "select_food_tool(caviar)",
        st.select_food_tool.func(
            food_types=["caviar"], runtime=FakeRuntime(runtime_state)
        ),
        "selected_food_types",
    )

    return CheckResult(
        check_id="L0.enum_validation",
        layer="L0",
        title="枚举与格式校验（非法输入必须被拒绝且不改状态）",
        passed=not failures,
        detail="; ".join(failures[:6]) if failures else f"{cases} 个输入用例全部符合预期",
        data={"cases": cases, "failures": failures},
    )


async def check_budget_level_boundaries() -> CheckResult:
    """Budget-level cutoffs: <3000 economy, 3000-7999 comfort, >=8000 luxury."""

    expectations = [
        (2999.0, "economy"),
        (3000.0, "comfort"),
        (7999.0, "comfort"),
        (8000.0, "luxury"),
    ]
    failures: list[str] = []
    observed: list[dict[str, Any]] = []

    for amount, expected_level in expectations:
        command = st.record_requirement_tool.func(
            **dict(FULL_REQUIREMENT_KWARGS, budget_min=amount, budget_max=amount),
            runtime=FakeRuntime(_sample_state()),
        )
        requirement = _update_of(command).get("user_requirement") or {}
        actual = requirement.get("budget_level")
        observed.append({"avg_budget": amount, "expected": expected_level, "actual": actual})
        if actual != expected_level:
            failures.append(f"avg={amount}: got {actual}, expected {expected_level}")

    return CheckResult(
        check_id="L0.budget_boundaries",
        layer="L0",
        title="预算等级边界（2999/3000/7999/8000）",
        passed=not failures,
        detail="; ".join(failures) if failures else "四个边界值全部命中",
        data={"observed": observed},
    )


async def check_precondition_gate_on_deterministic_tools() -> CheckResult:
    """Deterministic tools must refuse to produce output on incomplete state."""

    failures: list[str] = []

    empty = FakeRuntime({})
    itinerary_command = generate_itinerary_tool.func(runtime=empty)
    if "structured_itinerary" in _update_of(itinerary_command):
        failures.append("generate_itinerary_tool 在缺少前置状态时仍生成了行程")

    budget_command = summarize_budget_tool.func(runtime=FakeRuntime({}))
    if "structured_budget" in _update_of(budget_command):
        failures.append("summarize_budget_tool 在缺少 user_requirement 时仍生成了预算")

    return CheckResult(
        check_id="L0.tool_precondition_gate",
        layer="L0",
        title="确定性工具的前置门禁（状态不全时必须拒答）",
        passed=not failures,
        detail="; ".join(failures) if failures else "两个确定性工具均正确拒绝",
        data={"failures": failures},
    )


async def check_one_step_per_turn_invariant() -> CheckResult:
    """No single state tool may advance ``current_step`` by more than one.

    The graph wires ``add_edge(step, END)`` with a conditional START route, so a
    turn can only ever move one step forward. If a tool silently jumped two
    steps, the state machine would be bypassed.
    """

    order = {name: index for index, name in enumerate(st.ALL_STEPS)}
    failures: list[str] = []

    for tool_name, (func, kwargs) in STATE_TOOL_SAMPLES.items():
        if tool_name == "generate_order_tool":
            continue  # terminal step: does not advance
        owner = TOOL_OWNER_STEP[tool_name]
        command = func(runtime=FakeRuntime(_sample_state()), **kwargs)
        next_step = _update_of(command).get("current_step")
        if next_step is None:
            continue
        delta = order[next_step] - order[owner]
        if delta != 1:
            failures.append(f"{tool_name}: {owner} -> {next_step} (delta={delta})")

    return CheckResult(
        check_id="L0.one_step_per_turn",
        layer="L0",
        title="一轮一步不变量（状态工具最多前进 1 步）",
        passed=not failures,
        detail="; ".join(failures) if failures else "7 个状态工具均恰好前进 1 步",
        data={"failures": failures},
    )


async def check_schema_validation() -> CheckResult:
    """Planner output must satisfy the declared pydantic schemas."""

    failures: list[str] = []
    state = _sample_state()

    plan = build_itinerary_plan(state)
    if not isinstance(plan, ItineraryPlan):
        failures.append("build_itinerary_plan 未返回 ItineraryPlan")
    else:
        try:
            ItineraryPlan.model_validate(plan.model_dump())
        except Exception as exc:
            failures.append(f"ItineraryPlan 校验失败: {exc}")
        if len(plan.days) != 4:
            failures.append(f"天数与 user_requirement.travel_days 不一致: {len(plan.days)}")

    estimate = estimate_budget(state)
    if not isinstance(estimate, BudgetEstimate):
        failures.append("estimate_budget 未返回 BudgetEstimate")
    else:
        try:
            BudgetEstimate.model_validate(estimate.model_dump())
        except Exception as exc:
            failures.append(f"BudgetEstimate 校验失败: {exc}")

    # Negative case: an illegal time_slot must be rejected by the schema.
    try:
        ItineraryPlan.model_validate(
            {
                "destination": "西安",
                "summary": "bad",
                "days": [
                    {
                        "day_number": 1,
                        "theme": "x",
                        "items": [
                            {
                                "title": "x",
                                "time_slot": "midnight",
                                "location": {"name": "西安"},
                                "reason": "x",
                            }
                        ],
                    }
                ],
            }
        )
        failures.append("非法 time_slot 未被 schema 拒绝")
    except Exception:
        pass

    return CheckResult(
        check_id="L0.schema_validation",
        layer="L0",
        title="结构化输出 schema 校验（含非法枚举负例）",
        passed=not failures,
        detail="; ".join(failures) if failures else "行程与预算 schema 均通过，负例被正确拒绝",
        data={"failures": failures},
    )


async def check_travel_days_clamping() -> CheckResult:
    """``travel_days`` must be clamped into 1..14 by the planner."""

    failures: list[str] = []
    for declared, expected in ((0, 1), (-3, 1), (1, 1), (14, 14), (30, 14)):
        state = _sample_state()
        state["user_requirement"] = dict(state["user_requirement"], travel_days=declared)
        plan = build_itinerary_plan(state)
        if len(plan.days) != expected:
            failures.append(f"travel_days={declared} -> {len(plan.days)} 天，期望 {expected}")

    return CheckResult(
        check_id="L0.travel_days_clamp",
        layer="L0",
        title="行程天数截断（1..14）",
        passed=not failures,
        detail="; ".join(failures) if failures else "0/-3/1/14/30 全部落在 1..14",
        data={"failures": failures},
    )


def check_capability_vocabulary(dataset_dir: Path | None = None) -> CheckResult:
    """Every tool/capability value in a dataset must come from the vocabulary.

    Single source of truth: ``app.mcp_core.registry``. This used to be a
    documented gap (319 mismatches across the 240 rows), which made tool recall
    uncomputable because ``resolve_tool_capability()`` degraded the unknown
    values to ``unknown.read``. It is now a hard gate.
    """

    root = dataset_dir or DATASET_DIR
    failures: list[str] = []
    checked = 0

    for path in sorted(root.glob("*.jsonl")):
        for row in _iter_jsonl(path):
            candidates: list[str] = []
            for capability in row.get("expected_capabilities") or []:
                if isinstance(capability, dict):
                    candidates.append(str(capability.get("capability") or ""))
                else:
                    candidates.append(str(capability))
            candidates += [str(item) for item in row.get("forbidden_capabilities") or []]
            candidates += [str(item) for item in row.get("expected_tools") or []]
            # Concrete tool names appear in the trajectory scenarios; those must
            # resolve to a real vocabulary id rather than ``unknown.*``.
            candidates += [
                str(step["tool"]) for step in row.get("script") or [] if isinstance(step, dict) and step.get("tool")
            ]
            candidates += [
                str(name) for name in (row.get("expect") or {}).get("tools_called") or []
            ]

            for candidate in candidates:
                if not candidate:
                    continue
                checked += 1
                if candidate in KNOWN_TOOL_VOCABULARY:
                    continue
                if not resolve_tool_id(candidate).startswith("unknown."):
                    continue
                failures.append(f"{path.name}:{row.get('id')} -> {candidate}")

    passed = not failures
    return CheckResult(
        check_id="L0.capability_vocabulary",
        layer="L0",
        title="capability 词表一致性（数据集 ⊆ registry）",
        passed=passed,
        detail=(
            f"{checked} 个取值全部落在词表内（合法值 {len(KNOWN_TOOL_VOCABULARY)} 个）"
            if passed
            else f"{len(failures)} 处无法解析，例如 " + "; ".join(failures[:8])
        ),
        data={
            "checked_values": checked,
            "vocabulary": sorted(KNOWN_TOOL_VOCABULARY),
            "failures": failures[:30],
        },
    )


#: Common fields every dataset row must carry. See docs/agent-eval-design.md
#: P0-2 / Phase 2 item 2.
REQUIRED_DATASET_FIELDS = ("id", "type", "split", "version", "input", "initial_state", "metadata")
ALLOWED_SPLITS = ("smoke", "regression", "challenge", "safety", "holdout")
DATASET_VERSION = "0.2.0"


def check_dataset_schema(dataset_dir: Path | None = None) -> CheckResult:
    """Dataset rows must carry split / version / initial_state / metadata.

    ``initial_state`` is not cosmetic: the graph is "one turn = one step", so a
    case targeting the transport step has to arrive with
    ``current_step="transport_planning"`` and its prerequisites satisfied —
    without it the case cannot be located at all.
    """

    root = dataset_dir or DATASET_DIR
    failures: list[str] = []
    stats: dict[str, Any] = {}
    seen_ids: dict[str, str] = {}

    for path in sorted(root.glob("*.jsonl")):
        rows = list(_iter_jsonl(path))
        splits: dict[str, int] = {}
        versions: set[str] = set()

        for row in rows:
            row_id = str(row.get("id"))

            if row_id in seen_ids:
                failures.append(f"重复 id {row_id}（{seen_ids[row_id]} 与 {path.name}）")
            seen_ids[row_id] = path.name

            for field in REQUIRED_DATASET_FIELDS:
                if field not in row:
                    failures.append(f"{path.name}:{row_id} 缺字段 {field}")
            if not row.get("input"):
                failures.append(f"{path.name}:{row_id} input 为空")

            split = row.get("split")
            if split not in ALLOWED_SPLITS:
                failures.append(f"{path.name}:{row_id} 非法 split={split!r}")
            else:
                splits[split] = splits.get(split, 0) + 1

            version = row.get("version")
            versions.add(str(version))
            if version != DATASET_VERSION:
                failures.append(f"{path.name}:{row_id} version={version!r} ≠ {DATASET_VERSION}")

            metadata = row.get("metadata") or {}
            if not metadata.get("difficulty"):
                failures.append(f"{path.name}:{row_id} metadata 缺 difficulty")
            if "multi_turn" not in metadata:
                failures.append(f"{path.name}:{row_id} metadata 缺 multi_turn")

            # An illegal starting step is allowed only when explicitly flagged
            # (one scenario exercises the router fallback).
            step = (row.get("initial_state") or {}).get("current_step")
            flagged = bool(metadata.get("deliberately_illegal_initial_step"))
            illegal = step not in STEP_SEQUENCE
            if illegal and not flagged:
                failures.append(f"{path.name}:{row_id} 非法 initial_state.current_step={step!r}")
            if flagged and not illegal:
                failures.append(
                    f"{path.name}:{row_id} 标了 deliberately_illegal_initial_step 但 {step!r} 合法"
                )

        stats[path.name] = {"rows": len(rows), "splits": splits, "versions": sorted(versions)}

    passed = not failures
    total_rows = sum(info["rows"] for info in stats.values())
    return CheckResult(
        check_id="L0.dataset_schema",
        layer="L0",
        title="数据集 schema（split / version / initial_state / metadata 齐备）",
        passed=passed,
        detail=(
            f"{total_rows} 行全部合规"
            if passed
            else f"{len(failures)} 处问题，例如 " + "; ".join(failures[:6])
        ),
        data={"files": stats, "failures": failures[:40]},
    )


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


CHECK_FUNCTIONS: tuple[Callable[[], Any], ...] = (
    check_step_requires_matrix,
    check_route_robustness,
    check_rollback_cleanup_matrix,
    check_state_write_contract,
    check_enum_and_format_validation,
    check_budget_level_boundaries,
    check_precondition_gate_on_deterministic_tools,
    check_one_step_per_turn_invariant,
    check_schema_validation,
    check_travel_days_clamping,
)


async def run_l0_checks() -> list[CheckResult]:
    """Run every async L0 check plus the synchronous ones."""

    results: list[CheckResult] = []
    for func in CHECK_FUNCTIONS:
        try:
            results.append(await func())
        except Exception as exc:  # a crashing check is a failing check
            results.append(
                CheckResult(
                    check_id=f"L0.{getattr(func, '__name__', 'unknown')}",
                    layer="L0",
                    title=getattr(func, "__name__", "unknown"),
                    passed=False,
                    detail=f"检查执行异常: {type(exc).__name__}: {exc}",
                )
            )
    results.append(check_capability_vocabulary())
    results.append(check_dataset_schema())
    return results
