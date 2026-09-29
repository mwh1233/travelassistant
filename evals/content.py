"""Content-dataset executors (Wave 1).

The 240 content rows (travel / mcp / rag) used to be *schema-checked only* —
they were never executed, so "240 行全部合规" said nothing about behaviour. This
module gives each dataset type an **explicit executor** and an **explicit
grader**, so the report can separate:

- 结构校验样本数 (``dataset`` suite) — rows are well-formed
- 行为执行样本数 (this module) — rows were actually driven through the system

Three executors, each honest about what it does *not* prove:

``travel_walk``
    Walks the real state machine turn by turn (8 steps = 8 turns, because the
    graph is one-turn-one-step), then grades the produced ``TravelState`` with
    ``evals.graders``. It proves the plan is executable end-to-end and the
    planners produce schema-valid output. It does **not** prove NLU: the
    requirement is extracted deterministically from the row text, so a row whose
    extraction is incomplete is reported as ``extraction_incomplete`` instead of
    being counted as a pass.
``mcp_probe``
    Static capability probe: does an available tool exist for every expected
    capability, and does no available tool map to a forbidden capability. Cheap,
    deterministic, and the only honest thing to assert without a live MCP server.
``rag_probe``
    BM25 retrieval over the **local** document corpus, offline. Measures entity
    recall / source coverage and checks fallback semantics.

See ``docs/design/eval-optimization-roadmap.md`` Wave 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.agents.graphs.travel_planner_graph import STEP_SEQUENCE
from app.mcp_core.registry import (
    FORBIDDEN_CAPABILITIES,
    INTERNAL_TOOL_CAPABILITIES,
    KNOWN_TOOL_VOCABULARY,
    resolve_tool_id,
)
from evals.checks import CheckResult
from evals.graders import grade_travel_state
from evals.harness import load_step_config, run_turns

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = PROJECT_ROOT / "evals" / "datasets"
DOCUMENT_DIR = PROJECT_ROOT / "data" / "documents" / "destinations"

TRAVEL_DATASET = "travel_tasks.jsonl"
MCP_DATASET = "mcp_tool_tasks.jsonl"
RAG_DATASET = "rag_qa.jsonl"

#: Fixed dates keep the walk deterministic (D09 §3.1 外部非确定依赖).
DEFAULT_DEPARTURE_DATE = "2026-10-01"
#: Used only when a row genuinely omits the origin; recorded in
#: ``Requirement.inferred`` so the report never hides the assumption.
DEFAULT_DEPARTURE_CITY = "北京"
DEFAULT_TRAVEL_DAYS = 3
DEFAULT_BUDGET_MAX = 3000.0

VALID_TRAVEL_STYLES = ("relaxation", "culture", "adventure", "food")
VALID_ACCOMMODATION = ("star_hotel", "economy_hotel", "hostel", "youth_hostel")
VALID_FOOD = ("specialty", "chain", "local")
VALID_TRANSPORT = ("flight", "train", "driving")

#: constraint -> travel_style. Only the four legal enum values may appear.
CONSTRAINT_TO_STYLE = {
    "culture": "culture",
    "museum": "culture",
    "history": "culture",
    "food": "food",
    "local_snacks": "food",
    "relaxation": "relaxation",
    "low_intensity": "relaxation",
    "slow_travel": "relaxation",
    "beach": "relaxation",
    "nature": "adventure",
    "hiking": "adventure",
    "theme_park": "adventure",
}


# ---------------------------------------------------------------------------
# text extraction (deterministic, no model)
# ---------------------------------------------------------------------------

_DAYS_RE = re.compile(r"(\d{1,2})\s*天")
_HEADCOUNT_RE = re.compile(r"(\d{1,2})\s*[人位]")
_BUDGET_RE = re.compile(r"(?:人均|每人|预算)\s*(\d{3,6})")

#: Characters that mark the token right after them as a departure city.
_ORIGIN_MARKERS = ("从", "由")
#: Markers that introduce the destination.
_DEST_MARKERS = ("去", "到", "往", "游")
#: Suffixes that make a bare city name read as a destination.
_DEST_SUFFIXES = ("天", "日", "游", "亲子", "研学", "自由行", "度", "玩")

_PARTY_PATTERNS = (
    (re.compile(r"一家三口|两大一小|两大人一小孩"), (2, 1)),
    (re.compile(r"一家四口|两大两小"), (2, 2)),
    (re.compile(r"三口之家"), (2, 1)),
    (re.compile(r"情侣|夫妻|二人世界|两人"), (2, 0)),
    (re.compile(r"带父母|带老人|带长辈"), (2, 0)),
    (re.compile(r"独自|一人"), (1, 0)),
)

#: Fallback gazetteer, used only when the regexes find nothing.
_CITY_GAZETTEER = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "重庆", "西安", "厦门",
    "苏州", "青岛", "天津", "哈尔滨", "长沙", "武汉", "昆明", "大理", "丽江", "桂林",
    "三亚", "济南", "郑州", "沈阳", "大连", "福州", "南昌", "合肥", "太原", "兰州",
    "拉萨", "西宁", "银川", "乌鲁木齐", "呼和浩特", "贵阳", "南宁", "海口", "珠海",
    "无锡", "宁波", "温州", "佛山", "东莞", "泉州", "洛阳", "开封", "敦煌", "张家界",
    "顺德", "潮汕", "阿坝", "延吉",
)


@dataclass
class Requirement:
    """The 9 fields ``record_requirement_tool`` needs, plus provenance."""

    values: dict[str, Any]
    missing: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing


def extract_requirement(row: dict[str, Any]) -> Requirement:
    """Derive the requirement contract from a row's natural-language input.

    Provenance matters more than completeness here. A city the row simply does
    not mention is *inferred*, and gets recorded as such — never silently
    invented and then reported as a clean pass.
    """

    text = str(row.get("input") or "")
    constraints = list(row.get("constraints") or [])
    missing: list[str] = []
    inferred: list[str] = []

    cities = _cities_in_order(text)
    departure = _city_after_markers(text, cities, _ORIGIN_MARKERS)
    destination = _city_after_markers(text, cities, _DEST_MARKERS)
    if not destination:
        destination = _city_before_suffix(text, cities, _DEST_SUFFIXES)

    names = [name for _, name in cities]
    if not destination:
        destination = next((name for name in names if name != departure), "")

    if not departure:
        departure = next((name for name in names if name != destination), "")
    if not departure:
        departure = DEFAULT_DEPARTURE_CITY
        inferred.append("departure_city")

    if not destination:
        missing.append("destination")
    elif destination == departure:
        # A single city with no stated origin is a local trip, not an error.
        # Recorded as inferred so it never masquerades as explicit input.
        inferred.append("same_city_trip")

    days_match = _DAYS_RE.search(text)
    travel_days = int(days_match.group(1)) if days_match else DEFAULT_TRAVEL_DAYS
    if not days_match:
        inferred.append("travel_days")

    adult_count, children_count = _party_size(text)

    budget_match = _BUDGET_RE.search(text)
    budget_max = float(budget_match.group(1)) if budget_match else DEFAULT_BUDGET_MAX
    if not budget_match:
        inferred.append("budget_max")

    styles = [CONSTRAINT_TO_STYLE[c] for c in constraints if c in CONSTRAINT_TO_STYLE]
    styles = _dedup([s for s in styles if s in VALID_TRAVEL_STYLES]) or ["culture"]

    values = {
        "departure_city": departure or "",
        "destination": destination or "",
        "departure_date": DEFAULT_DEPARTURE_DATE,
        "travel_days": max(1, min(14, travel_days)),
        "adult_count": max(1, adult_count),
        "children_count": max(0, children_count),
        "budget_min": round(budget_max * 0.6, 2),
        "budget_max": budget_max,
        "travel_styles": styles,
        # ``record_requirement_tool`` declares ``special_needs: str = ""``; an
        # explicit ``None`` fails pydantic validation and silently aborts the
        # whole walk, so the empty string is the contract-correct value.
        "special_needs": "",
    }
    return Requirement(values=values, missing=missing, inferred=inferred)


def _cities_in_order(text: str) -> list[tuple[int, str]]:
    """All gazetteer hits with their position, longest-name-wins on overlap."""

    hits: list[tuple[int, str]] = []
    for city in _CITY_GAZETTEER:
        start = 0
        while True:
            position = text.find(city, start)
            if position < 0:
                break
            hits.append((position, city))
            start = position + 1
    hits.sort()

    resolved: list[tuple[int, str]] = []
    consumed_until = -1
    for position, city in hits:
        if position < consumed_until:
            continue
        # Prefer the longer name when two entries overlap at the same spot.
        overlapping = [c for p, c in hits if p == position and len(c) > len(city)]
        chosen = sorted(overlapping, key=len)[-1] if overlapping else city
        resolved.append((position, chosen))
        consumed_until = position + len(chosen)
    return resolved


def _city_after_markers(
    text: str, cities: list[tuple[int, str]], markers: tuple[str, ...]
) -> str:
    """Return the first city immediately preceded by one of ``markers``."""

    for position, city in cities:
        prefix = text[max(0, position - 2) : position]
        if any(prefix.endswith(marker) for marker in markers):
            return city
    return ""


def _city_before_suffix(
    text: str, cities: list[tuple[int, str]], suffixes: tuple[str, ...]
) -> str:
    """Return the first city immediately followed by a destination-ish suffix."""

    for position, city in cities:
        suffix = text[position + len(city) : position + len(city) + 3]
        if any(suffix.startswith(item) for item in suffixes):
            return city
    return ""


def _party_size(text: str) -> tuple[int, int]:
    for pattern, (adults, children) in _PARTY_PATTERNS:
        if pattern.search(text):
            return adults, children
    match = _HEADCOUNT_RE.search(text)
    if match:
        total = int(match.group(1))
        return max(1, total), 0
    return 2, 0


def _dedup(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------------------
# travel walk
# ---------------------------------------------------------------------------

#: step -> (state tool, arg builder). Only these advance ``current_step``.
WALK_STATE_TOOLS: dict[str, tuple[str, Any]] = {
    "requirement_collection": ("record_requirement_tool", lambda req: dict(req)),
    "destination_recommendation": (
        "select_destination_tool",
        lambda req: {"destination": req["destination"]},
    ),
    "transport_planning": (
        "select_transport_tool",
        lambda req: {"transport_type": _transport_for(req)},
    ),
    "accommodation_planning": (
        "select_accommodation_tool",
        lambda req: {"accommodation_types": _accommodation_for(req)},
    ),
    "food_planning": ("select_food_tool", lambda req: {"food_types": _food_for(req)}),
    "itinerary_generation": ("generate_itinerary_tool", lambda req: {}),
    "budget_summarization": ("summarize_budget_tool", lambda req: {}),
}


def _transport_for(req: dict[str, Any]) -> str:
    return "train"


def _accommodation_for(req: dict[str, Any]) -> list[str]:
    return ["star_hotel"]


def _food_for(req: dict[str, Any]) -> list[str]:
    styles = req.get("travel_styles") or []
    return ["local"] if "food" in styles else ["specialty"]


async def candidate_tool_index() -> dict[str, Any]:
    """Return the step -> tool-name map plus a capability -> (step, tool) index."""

    config = await load_step_config()
    by_step: dict[str, list[str]] = {}
    by_capability: dict[str, list[tuple[str, str]]] = {}
    all_names: set[str] = set()

    for step_name, step in config.items():
        names = [getattr(tool, "name", "") for tool in step.get("tools", [])]
        by_step[step_name] = [name for name in names if name]
        for name in by_step[step_name]:
            all_names.add(name)
            capability = resolve_tool_id(name)
            by_capability.setdefault(capability, []).append((step_name, name))

    rollback: set[str] = set()
    for step in config.values():
        for tool in step.get("tools", []):
            name = getattr(tool, "name", "")
            if name.startswith("go_back_"):
                rollback.add(name)
    by_step["__rollback__"] = sorted(rollback)

    return {"by_step": by_step, "by_capability": by_capability, "all_names": sorted(all_names)}


def walk_stop_step(row: dict[str, Any]) -> str:
    """The last step a content row implies, from its own ``expected_tools``."""

    expected = set(row.get("expected_tools") or [])
    if "internal.budget.summarize" in expected:
        return "budget_summarization"
    if "internal.itinerary.generate" in expected:
        return "itinerary_generation"
    if expected & {"internal.food.select"}:
        return "food_planning"
    if expected & {"internal.transport.select"}:
        return "transport_planning"
    return "food_planning"


def build_walk_turns(row: dict[str, Any], requirement: Requirement, index: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive a turn-by-turn walk of the real state machine for one row."""

    expected = list(row.get("expected_tools") or [])
    stop = walk_stop_step(row)
    stop_index = STEP_SEQUENCE.index(stop)

    # Query tools: attach each expected external capability to the first step
    # whose candidate list actually contains a tool for it.
    query_plan: dict[str, list[dict[str, Any]]] = {}
    used_tools: set[str] = set()
    for capability in expected:
        if capability in INTERNAL_TOOL_CAPABILITIES.values():
            continue
        for step_name, tool_name in index["by_capability"].get(capability, []):
            if tool_name in used_tools:
                continue
            if STEP_SEQUENCE.index(step_name) > stop_index:
                continue
            query_plan.setdefault(step_name, []).append(
                {"tool": tool_name, "args": _query_args(tool_name, requirement.values)}
            )
            used_tools.add(tool_name)
            break

    turns: list[dict[str, Any]] = []
    for position in range(stop_index + 1):
        step_name = STEP_SEQUENCE[position]
        script: list[dict[str, Any]] = list(query_plan.get(step_name, []))

        spec = WALK_STATE_TOOLS.get(step_name)
        if spec is not None:
            tool_name, builder = spec
            script.append({"tool": tool_name, "args": builder(requirement.values)})

        if not script:
            continue
        turns.append(
            {
                "input": _turn_message(step_name, requirement.values),
                "script": script,
                "final_content": f"（{step_name} 处理完成）",
            }
        )
    return turns


def _query_args(tool_name: str, req: dict[str, Any]) -> dict[str, Any]:
    """Best-effort args for a stubbed external tool — the stub accepts anything."""

    lowered = tool_name.lower()
    if "weather" in lowered:
        return {"city": req.get("destination") or req.get("departure_city") or ""}
    if "hotel" in lowered:
        return {"city": req.get("destination") or ""}
    if "direction" in lowered or "route" in lowered:
        return {"origin": req.get("departure_city") or "", "destination": req.get("destination") or ""}
    if "around" in lowered or "poi" in lowered:
        return {"keywords": req.get("destination") or "", "city": req.get("destination") or ""}
    if "search" in lowered:
        return {"query": f"{req.get('destination') or ''} 旅行攻略"}
    if "transport" in lowered:
        return {
            "origin_city": req.get("departure_city") or "",
            "destination_city": req.get("destination") or "",
            "departure_date": req.get("departure_date") or DEFAULT_DEPARTURE_DATE,
        }
    return {"query": req.get("destination") or ""}


def _turn_message(step_name: str, req: dict[str, Any]) -> str:
    messages = {
        "requirement_collection": f"{req.get('departure_city')}出发去{req.get('destination')}，{req.get('travel_days')}天",
        "destination_recommendation": f"就去{req.get('destination')}吧",
        "transport_planning": "高铁就行",
        "accommodation_planning": "住酒店，交通方便一点",
        "food_planning": "想吃当地特色",
        "itinerary_generation": "行程可以，生成吧",
        "budget_summarization": "帮我汇总一下预算",
    }
    return messages.get(step_name, "继续")


# ---------------------------------------------------------------------------
# outcome container
# ---------------------------------------------------------------------------


@dataclass
class ContentOutcome:
    case_id: str
    dataset: str
    split: str
    executed: bool
    checks: list[CheckResult] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return self.executed and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[str]:
        return sorted({check.check_id for check in self.checks if not check.passed})


def _result(check_id: str, title: str, passed: bool, detail: str = "", data: dict | None = None, layer: str = "L1") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        layer=layer,
        title=title,
        passed=passed,
        detail=detail,
        data=data or {},
    )


# ---------------------------------------------------------------------------
# executor 1: travel walk
# ---------------------------------------------------------------------------


async def run_travel_probe(
    row: dict[str, Any],
    *,
    index: dict[str, Any],
    mode: str = "deterministic",
    dataset_version: str | None = None,
    code_sha: str = "",
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
) -> ContentOutcome:
    """Drive the state machine end to end for one travel row, then grade it."""

    case_id = str(row.get("id"))
    requirement = extract_requirement(row)
    checks: list[CheckResult] = []

    if requirement.missing:
        checks.append(
            _result(
                "L1.requirement_completeness",
                "需求字段可从用例文本还原",
                False,
                f"缺失 {requirement.missing}（需要人工补 expect，或改用带 initial_state 的轨迹用例）",
                {"missing": requirement.missing, "input": row.get("input")},
            )
        )
        return ContentOutcome(
            case_id=case_id,
            dataset=TRAVEL_DATASET,
            split=str(row.get("split")),
            executed=False,
            checks=checks,
            metadata={"difficulty": (row.get("metadata") or {}).get("difficulty")},
        )

    turns = build_walk_turns(row, requirement, index)
    outcome = await run_turns(
        row,
        turns,
        mode=mode,
        dataset_version=dataset_version,
        dataset_split=str(row.get("split")),
        code_sha=code_sha,
        cassette_mode=cassette_mode,
        cassette_dir=cassette_dir,
    )

    if outcome.error:
        checks.append(
            _result("L2.trace_completed", "走查执行无异常", False, outcome.error)
        )
        return ContentOutcome(
            case_id=case_id,
            dataset=TRAVEL_DATASET,
            split=str(row.get("split")),
            executed=True,
            checks=checks,
            trace=outcome.record,
            metadata={"difficulty": (row.get("metadata") or {}).get("difficulty")},
            error=outcome.error,
            latency_ms=float(outcome.latency_ms or 0.0),
        )

    expected = set(row.get("expected_tools") or [])
    requires_external = "requires_sources" in (row.get("constraints") or [])
    stop_step = walk_stop_step(row)

    scores = grade_travel_state(
        outcome.final_state,
        required_fields=list(row.get("must_collect") or []),
        requires_external_sources=requires_external,
    )

    # --- the walk must actually reach the step the row implies -----------
    reached = outcome.final_state.get("current_step")
    expected_final = STEP_SEQUENCE[min(STEP_SEQUENCE.index(stop_step) + 1, len(STEP_SEQUENCE) - 1)]
    checks.append(
        _result(
            "L2.final_step",
            "走查到达该用例隐含的终点",
            reached in (stop_step, expected_final),
            f"实际 {reached}，期望 {stop_step} 或 {expected_final}",
        )
    )

    # --- result-level hard gates (only for what the row actually asked for)
    gates = scores.get("hard_gates") or {}
    if "internal.itinerary.generate" in expected:
        checks.append(
            _result(
                "L1.result_hard_gate",
                "行程 schema 硬门禁",
                bool(gates.get("itinerary_schema_ok")),
                f"itinerary_schema_ok={gates.get('itinerary_schema_ok')}",
                {"scores": scores},
            )
        )
    if "internal.budget.summarize" in expected:
        checks.append(
            _result(
                "L1.result_hard_gate",
                "预算 schema 硬门禁",
                bool(gates.get("budget_schema_ok")),
                f"budget_schema_ok={gates.get('budget_schema_ok')}",
            )
        )
    if "avoid_hallucination" in (row.get("constraints") or []):
        checks.append(
            _result(
                "L1.unbacked_realtime_claim",
                "无无据实时声称",
                bool(gates.get("no_unbacked_realtime_claim")),
                f"违例项 {scores.get('unbacked_realtime_claims')}",
            )
        )

    # --- budget constraint -------------------------------------------------
    if "budget_limited" in (row.get("constraints") or []) or "low_budget" in (row.get("constraints") or []):
        score = float(scores.get("budget_constraint") or 0.0)
        checks.append(
            _result(
                "L1.budget_constraint",
                "预算约束被满足",
                score >= 0.99,
                f"budget_constraint={score:.2f}",
            )
        )

    # --- external evidence -------------------------------------------------
    if requires_external:
        coverage = float(scores.get("external_source_coverage") or 0.0)
        checks.append(
            _result(
                "L1.external_source_coverage",
                "需要外部来源的用例具备外部证据",
                coverage > 0.0,
                f"external_source_coverage={coverage:.2f}"
                "（0 表示行程全部来自 planner，未消费任何工具返回）",
            )
        )

    return ContentOutcome(
        case_id=case_id,
        dataset=TRAVEL_DATASET,
        split=str(row.get("split")),
        executed=True,
        checks=checks,
        scores=scores,
        trace=outcome.record,
        metadata={
            "difficulty": (row.get("metadata") or {}).get("difficulty"),
            "risk_level": (row.get("metadata") or {}).get("risk_level"),
            "turn_count": len(turns),
            "inferred_fields": requirement.inferred,
        },
        latency_ms=float(outcome.latency_ms or 0.0),
    )


# ---------------------------------------------------------------------------
# executor 2: MCP capability probe
# ---------------------------------------------------------------------------


async def run_mcp_probe(
    row: dict[str, Any],
    *,
    index: dict[str, Any],
) -> ContentOutcome:
    """Static probe: capability availability + forbidden-capability exposure.

    No model, no network. It answers exactly two questions:

    1. Is there an available tool for every capability the row expects to be
       *required*? (If not, the row is unachievable on this build.)
    2. Does any available tool resolve to a forbidden capability? (If yes, the
       deny-list is decorative.)
    """

    case_id = str(row.get("id"))
    checks: list[CheckResult] = []
    available = index["by_capability"]

    expected = [
        entry.get("capability") if isinstance(entry, dict) else entry
        for entry in row.get("expected_capabilities") or []
    ]
    required = [
        (entry.get("capability") if isinstance(entry, dict) else entry)
        for entry in row.get("expected_capabilities") or []
        if not isinstance(entry, dict) or entry.get("required", True)
    ]

    missing_required = [
        capability for capability in required if capability not in available
    ]
    # A capability with no server is acceptable only when the row says the
    # correct behaviour is to fall back.
    fallback_ok = bool(row.get("fallback_expected"))
    checks.append(
        _result(
            "L1.capability_available",
            "必需能力有可用工具（或声明了兜底）",
            not missing_required or fallback_ok,
            (
                "全部必需能力均可用"
                if not missing_required
                else f"缺失 {missing_required}"
                + ("（该行声明 fallback_expected=true，视为可接受）" if fallback_ok else "")
            ),
            {"expected": expected, "missing": missing_required, "fallback_expected": fallback_ok},
        )
    )

    # --- forbidden capabilities must not be reachable --------------------
    forbidden = [str(item) for item in row.get("forbidden_capabilities") or []]
    exposed = [capability for capability in forbidden if capability in available]
    declarable = [capability for capability in forbidden if capability in FORBIDDEN_CAPABILITIES]
    checks.append(
        _result(
            "L1.forbidden_capability_available",
            "禁列能力未被任何可用工具暴露",
            not exposed,
            (
                "禁列能力均无实现"
                if not exposed
                else f"以下禁列能力存在可用工具: {exposed}"
            ),
            {
                "forbidden": forbidden,
                "exposed": exposed,
                "declared_in_denylist": declarable,
                "unknown_forbidden": [
                    c for c in forbidden if c not in KNOWN_TOOL_VOCABULARY
                ],
            },
        )
    )

    # --- required_args is data-quality metadata, not an agent assertion ---
    required_args = row.get("required_args") or {}
    arg_names = sorted(required_args.keys())
    text = str(row.get("input") or "")
    unevidenced = [
        name
        for name, value in required_args.items()
        if isinstance(value, str) and value and value not in text
    ]
    checks.append(
        _result(
            "L1.required_args_present",
            "required_args 可在用例文本中找到依据",
            not unevidenced,
            (
                f"{len(arg_names)} 个必需参数均有文本依据"
                if not unevidenced
                else f"文本中找不到: {unevidenced}（数据自洽性问题）"
            ),
            {"required_args": required_args, "unevidenced": unevidenced},
        )
    )

    return ContentOutcome(
        case_id=case_id,
        dataset=MCP_DATASET,
        split=str(row.get("split")),
        executed=True,
        checks=checks,
        metadata={
            "difficulty": (row.get("metadata") or {}).get("difficulty"),
            "risk_level": row.get("risk_level"),
            "probe": "static_capability",
            "forbidden": forbidden,
        },
    )


# ---------------------------------------------------------------------------
# executor 3: RAG probe (offline BM25)
# ---------------------------------------------------------------------------


class LocalBm25Index:
    """Offline lexical index over the local destination corpus.

    Deliberately not the production dense retriever: that needs embeddings and
    therefore a network call, which would make the suite non-deterministic. BM25
    over the same documents is a real retrieval signal that runs on every commit.
    """

    def __init__(self, documents: list[tuple[str, str]]):
        self.documents = documents
        self._tokens: list[list[str]] = []
        self._bm25 = None
        self._build()

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    def _build(self) -> None:
        try:
            import jieba
            from rank_bm25 import BM25Okapi
        except Exception:  # pragma: no cover - dependency guard
            return
        self._tokenize = lambda text: [token for token in jieba.lcut(text) if token.strip()]
        self._tokens = [self._tokenize(text) for _, text in self.documents]
        if self._tokens:
            self._bm25 = BM25Okapi(self._tokens)

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if self._bm25 is None or not self._tokens:
            return []
        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[dict[str, Any]] = []
        for rank, position in enumerate(ranked[:top_k], 1):
            name, text = self.documents[position]
            results.append(
                {
                    "rank": rank,
                    "source": name,
                    "score": float(scores[position]),
                    "content": text,
                }
            )
        return results


def load_local_corpus(document_dir: Path | None = None) -> list[tuple[str, str]]:
    root = document_dir or DOCUMENT_DIR
    if not root.exists():
        return []
    corpus: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.md")):
        try:
            corpus.append((path.name, path.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            continue
    return corpus


def run_rag_probe(
    row: dict[str, Any],
    *,
    index: LocalBm25Index,
    top_k: int = 3,
) -> ContentOutcome:
    """Retrieve offline and check entity recall / source coverage / fallback."""

    case_id = str(row.get("id"))
    query = str(row.get("input") or "")
    results = index.search(query, top_k=top_k)
    haystack = "\n".join(item["content"] for item in results)

    entities = [str(item) for item in row.get("expected_entities") or []]
    hit_entities = [entity for entity in entities if entity and entity in haystack]
    recall = len(hit_entities) / len(entities) if entities else 1.0

    requires_sources = bool(row.get("requires_sources"))
    fallback_expected = bool(row.get("fallback_expected"))

    checks: list[CheckResult] = []

    # --- entity recall ---------------------------------------------------
    if recall > 0.0:
        checks.append(
            _result(
                "L1.rag_entity_recall",
                "期望实体可在检索结果中找到",
                recall >= 0.5,
                f"recall={recall:.2f}（命中 {hit_entities}）",
                {"hit": hit_entities, "expected": entities, "retrieved": [r["source"] for r in results]},
            )
        )
    else:
        # A miss is only a failure when the row does not expect a fallback.
        checks.append(
            _result(
                "L1.rag_entity_recall",
                "期望实体可在检索结果中找到",
                fallback_expected,
                (
                    "语料未覆盖该实体，但该行声明 fallback_expected=true，判定为应兜底"
                    if fallback_expected
                    else "语料未覆盖该实体，且该行未声明兜底 → 无法满足"
                ),
                {"expected": entities, "retrieved": [r["source"] for r in results]},
            )
        )

    # --- source coverage --------------------------------------------------
    if requires_sources:
        has_source = any(item.get("source") for item in results)
        checks.append(
            _result(
                "L1.rag_source_coverage",
                "需要来源的用例返回了来源",
                has_source and bool(results),
                f"检索到 {len(results)} 条，来源 {[r['source'] for r in results]}",
            )
        )

    # --- fallback semantics ----------------------------------------------
    if fallback_expected:
        checks.append(
            _result(
                "L1.rag_fallback",
                "兜底路径可判定",
                bool(results) or recall == 0.0,
                (
                    "有检索结果，或在无结果时应走兜底"
                    if results
                    else "无检索结果，需上游走兜底话术"
                ),
            )
        )

    return ContentOutcome(
        case_id=case_id,
        dataset=RAG_DATASET,
        split=str(row.get("split")),
        executed=True,
        checks=checks,
        scores={"entity_recall": recall, "retrieved": len(results)},
        metadata={
            "difficulty": (row.get("metadata") or {}).get("difficulty"),
            "corpus_docs": index.doc_count,
            "probe": "offline_bm25",
        },
    )


# ---------------------------------------------------------------------------
# corpus coverage helper
# ---------------------------------------------------------------------------


def corpus_coverage(rows: list[dict[str, Any]], corpus: list[tuple[str, str]]) -> dict[str, Any]:
    """How many rag rows have at least one expected entity inside the corpus.

    This is the single most useful number about the RAG setup: it shows how much
    of the question set the *corpus* can even answer, before any retriever is
    involved.
    """

    haystack = "\n".join(text for _, text in corpus)
    covered: list[str] = []
    uncovered: list[str] = []
    for row in rows:
        entities = [str(item) for item in row.get("expected_entities") or []]
        if any(entity and entity in haystack for entity in entities):
            covered.append(str(row.get("id")))
        else:
            uncovered.append(str(row.get("id")))
    return {
        "corpus_docs": len(corpus),
        "rows": len(rows),
        "covered": len(covered),
        "uncovered": len(uncovered),
        "coverage_rate": round(len(covered) / len(rows), 4) if rows else 0.0,
        "uncovered_ids": uncovered,
    }
