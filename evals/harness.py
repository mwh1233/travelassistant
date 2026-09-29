"""Deterministic execution harness for trajectory evaluation.

What gets replaced in Mock mode
-------------------------------
Only the *edges* of the system are faked:

- the chat model (``evals.mock_llm.ScriptedChatModel``),
- the checkpointer (a fresh ``MemorySaver`` per case),
- the long-term memory store (an in-process fake, so no Postgres is needed),
- every non-deterministic tool (MCP tools, the two nested-agent tools, and the
  memory-writing tools) is replaced by a **same-named** stub.

Everything that actually encodes product behaviour — the LangGraph topology,
the step prompts, ``_missing_requirements`` routing, and every state-transition
tool — stays real. That is what makes this layer able to run on every commit.

Execution modes
---------------
``deterministic``
    Stub responses are fixed (or taken from the scenario's ``tool_stubs``).
``replay``
    Stub responses come from a recorded **cassette**. A miss raises instead of
    falling back, because a silently-defaulted replay hides stale recordings
    (D09 §3.2, §7.3).
``live``
    Real model and real tools; the cassette may be put in ``record`` mode.

Multi-turn
----------
The graph is "one turn = one step" (``add_edge(step, END)`` + conditional
START routing), so walking all eight planning steps **requires eight turns** on
one thread. :func:`run_turns` is therefore the real driver; :func:`run_scenario`
is the single-turn special case.

See ``docs/agent-eval-design.md`` (section 6) and
``docs/design/eval-optimization-roadmap.md`` (Wave 1–2) for the rationale.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, ConfigDict

from app.agents.handoffs import step_config as step_config_module
from app.observability.trace import TraceCollector, tee_events
from app.utils.logger import app_logger
from evals.cassette import Cassette, CassetteMiss, load_cassette
from evals.faults import FaultInjector, apply_fault
from evals.mock_llm import ScriptedChatModel, build_responses


#: Tools whose behaviour is a pure function of the state. They are real in every
#: mode — the whole point is that the state machine is not being faked.
DETERMINISTIC_TOOL_NAMES = frozenset(
    {
        "record_requirement_tool",
        "update_travel_style_tool",
        "update_dietary_restriction_tool",
        "update_food_preference_tool",
        "select_destination_tool",
        "select_transport_tool",
        "select_accommodation_tool",
        "update_accommodation_preference_tool",
        "select_food_tool",
        "generate_itinerary_tool",
        "summarize_budget_tool",
        "generate_order_tool",
        "go_back_to_step",
        "go_back_to_requirement",
        "go_back_to_destination",
        "go_back_to_transport",
        "go_back_to_accommodation",
        "go_back_to_food",
        "go_back_to_itinerary",
        "go_back_to_budget",
        "check_current_progress",
        "request_action_approval",
        "record_action_approval",
    }
)

DEFAULT_STUB_RESPONSE = "[stub] 已返回模拟数据"


class StubArgs(BaseModel):
    """Permissive argument model so a stub accepts any tool call shape.

    ``reserved`` exists only because langchain treats a schema with zero fields
    as "no arguments" and would drop the real ones.
    """

    model_config = ConfigDict(extra="allow")
    reserved: Optional[str] = None


class ToolCallRecorder:
    """Collect the arguments each stub actually received."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, name: str, kwargs: dict[str, Any]) -> None:
        cleaned = {key: value for key, value in kwargs.items() if key != "reserved"}
        self.calls.append({"name": name, "args": cleaned})

    def names(self) -> list[str]:
        return [call["name"] for call in self.calls]

    def args_for(self, name: str) -> list[dict[str, Any]]:
        return [call["args"] for call in self.calls if call["name"] == name]


def make_stub(
    name: str,
    response: str,
    recorder: ToolCallRecorder,
    description: str = "",
    cassette: Cassette | None = None,
    injector: "FaultInjector | None" = None,
) -> BaseTool:
    """Build a same-named deterministic replacement for an external tool.

    In ``record`` mode the observed response is persisted; in ``replay`` mode the
    response is read back, and a missing entry raises rather than degrading.

    ``injector`` is consulted *after* the call is recorded but *before* the stub
    answers, so an injected fault replaces the tool's real behaviour while the
    call itself stays visible to every trajectory assertion. Replay is checked
    after the injector on purpose: a fault scenario must behave identically in
    ``deterministic`` and ``replay`` mode, otherwise the two are not comparable.
    """

    def _run(**kwargs: Any) -> str:
        cleaned = {key: value for key, value in kwargs.items() if key != "reserved"}
        recorder.record(name, cleaned)

        if injector is not None:
            event = injector.next_fault(name, cleaned)
            if event is not None:
                return apply_fault(event)

        if cassette is not None and cassette.mode == "replay":
            recorded = cassette.lookup(name, cleaned)
            if recorded is None:
                raise CassetteMiss(
                    f"replay 缺少录制: {name}({cleaned}) — 记录文件 {cassette.path}"
                )
            return recorded

        output = response
        if cassette is not None and cassette.mode == "record":
            cassette.record(name, cleaned, output)
        return output

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=description or f"[eval stub] {name}",
        args_schema=StubArgs,
    )


class FakeMemoryService:
    """In-process replacement for ``UserMemoryService``.

    Keeps Mock-mode runs free of any Postgres dependency while still exercising
    the prompt-injection path (and the per-``user_id`` namespace isolation).
    """

    def __init__(self) -> None:
        self._profiles: dict[str, dict[str, Any]] = {}

    async def format_memory_for_prompt(self, user_id: str | None) -> str:
        if not user_id:
            return ""
        profile = self._profiles.get(user_id) or {}
        styles = profile.get("travel_styles") or []
        if not styles:
            return ""
        return "**用户历史偏好**：\n- 旅行风格：" + "、".join(styles)

    async def update_travel_styles(self, user_id: str, styles: list[str]) -> None:
        profile = self._profiles.setdefault(user_id, {})
        current = set(profile.get("travel_styles") or [])
        current.update(styles)
        profile["travel_styles"] = sorted(current)

    async def get_user_profile(self, user_id: str) -> dict[str, Any]:
        return dict(self._profiles.get(user_id) or {})


def apply_tool_overrides(
    step_config: dict[str, dict[str, Any]],
    stub_responses: dict[str, str],
    recorder: ToolCallRecorder,
    cassette: Cassette | None = None,
    injector: FaultInjector | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Replace every non-deterministic tool with a same-named stub.

    Returns the patched config and the list of stubbed tool names. Stubs keep
    the original name on purpose: trajectory assertions must still be able to
    check *which* tool the agent chose.
    """

    patched: dict[str, dict[str, Any]] = {}
    stubbed: set[str] = set()

    for step_name, config in step_config.items():
        tools: list[BaseTool] = []
        for tool in config.get("tools", []):
            name = getattr(tool, "name", None)
            if name is None or name in DETERMINISTIC_TOOL_NAMES:
                tools.append(tool)
                continue
            tools.append(
                make_stub(
                    name=name,
                    response=stub_responses.get(name, DEFAULT_STUB_RESPONSE),
                    recorder=recorder,
                    description=getattr(tool, "description", "") or "",
                    cassette=cassette,
                    injector=injector,
                )
            )
            stubbed.add(name)
        patched[step_name] = {**config, "tools": tools}

    return patched, sorted(stubbed)


@dataclass
class ScenarioOutcome:
    """Everything a trajectory assertion might need about one scenario run."""

    case_id: str
    record: dict[str, Any]
    final_state: dict[str, Any]
    stub_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    cassette: dict[str, Any] = field(default_factory=dict)
    faults: dict[str, Any] = field(default_factory=dict)
    """Fault-injection record (``{}`` when no fault was planned)."""

    @property
    def tool_names(self) -> list[str]:
        return [call["name"] for call in self.record.get("tool_calls", [])]

    @property
    def trace_args(self) -> list[dict[str, Any]]:
        return self.record.get("tool_calls", [])

    @property
    def node_metrics(self) -> list[dict[str, Any]]:
        return self.record.get("node_metrics", [])


def base_step_config_snapshot() -> dict[str, dict[str, Any]]:
    """Return the un-patched step config (loaded once, then reused)."""

    return _STEP_CONFIG_CACHE


_STEP_CONFIG_CACHE: dict[str, dict[str, Any]] = {}


async def load_step_config() -> dict[str, dict[str, Any]]:
    """Load and cache the real step config once per process."""

    global _STEP_CONFIG_CACHE
    if not _STEP_CONFIG_CACHE:
        _STEP_CONFIG_CACHE = await step_config_module.get_step_config()
    return _STEP_CONFIG_CACHE


def build_input_state(scenario: dict[str, Any], turn_input: str | None = None) -> dict[str, Any]:
    """Turn a scenario's ``initial_state`` into graph input.

    Because the graph advances one step per turn, a scenario must seed the exact
    step it wants to exercise; that is the whole reason the dataset needs
    ``initial_state`` at all (docs/agent-eval-design.md P0-2).

    Later turns of a multi-turn run pass ``turn_input`` and inherit state through
    the checkpointer, so only the message is sent.
    """

    from langchain_core.messages import HumanMessage

    content = turn_input if turn_input is not None else scenario.get("input") or ""
    if turn_input is not None:
        return {"messages": [HumanMessage(content=content)]}

    state = dict(scenario.get("initial_state") or {})
    state.setdefault("current_step", scenario.get("step", "requirement_collection"))
    state["messages"] = [HumanMessage(content=content)]
    return state


async def run_turns(
    scenario: dict[str, Any],
    turns: list[dict[str, Any]],
    *,
    mode: str = "deterministic",
    model_factory: Any = None,
    dataset_version: str | None = None,
    dataset_split: str | None = None,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
    injector: FaultInjector | None = None,
) -> ScenarioOutcome:
    """Run N turns on one thread and return the merged trajectory + final state.

    Each turn dict accepts ``input`` / ``script`` / ``final_content``. The first
    turn also seeds ``initial_state`` from the scenario.

    ``injector`` (W6) faults stubbed tools on a fixed schedule so degradation —
    not just the happy path — can be graded reproducibly.
    """

    import app.agents.graphs.travel_planner_graph as graph_module

    case_id = str(scenario.get("id") or uuid.uuid4().hex[:8])
    recorder = ToolCallRecorder()
    memory = FakeMemoryService()
    run_id = uuid.uuid4().hex[:8]
    thread_id = f"eval-{case_id}-{run_id}"

    cassette = load_cassette(case_id, mode=cassette_mode, directory=cassette_dir)

    base_config = await load_step_config()
    stub_responses = scenario.get("tool_stubs") or {}
    if mode in ("deterministic", "replay"):
        step_config, stubbed_tools = apply_tool_overrides(
            base_config, stub_responses, recorder, cassette, injector
        )
    else:
        step_config, stubbed_tools = base_config, []

    original_get_step_config = graph_module.get_step_config
    original_get_memory = getattr(graph_module, "get_user_memory_service", None)

    async def _patched_step_config():
        return step_config

    graph_module.get_step_config = _patched_step_config
    if original_get_memory is not None and mode in ("deterministic", "replay"):
        async def _patched_memory():
            return memory

        graph_module.get_user_memory_service = _patched_memory

    started = time.time()
    turn_records: list[dict[str, Any]] = []
    error: str | None = None

    try:
        collector = TraceCollector(
            case_id=case_id,
            conversation_id=thread_id,
            user_id=scenario.get("user_id"),
            dataset_version=dataset_version,
            dataset_split=dataset_split or scenario.get("split"),
            prompt_version=scenario.get("prompt_version") or "step_config@working-tree",
            model="scripted" if mode in ("deterministic", "replay") else None,
            mode=mode,
            checkpointer_kind="memory",
            code_sha=code_sha or None,
            capture_tool_outputs=(cassette_mode == "record"),
        )

        # ---- build the model once, so the script cursor spans all turns ----
        if mode in ("deterministic", "replay"):
            flattened: list[Any] = []
            for turn in turns:
                flattened.extend(
                    build_responses(
                        turn.get("script") or [],
                        final_content=turn.get("final_content") or "",
                    )
                )
            model = ScriptedChatModel(responses=flattened)
            factory = model_factory or (lambda: model)
        else:
            factory = model_factory
            if factory is None:
                from app.agents.handoffs.travel_agent import get_llm

                factory = get_llm

        graph = await graph_module.create_travel_planner_graph(
            model_factory=factory,
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": thread_id}}

        for index, turn in enumerate(turns):
            if index == 0:
                input_state = build_input_state(scenario)
            else:
                input_state = build_input_state(scenario, turn.get("input") or "")
            before = _cost_snapshot(collector)
            turn_error: str | None = None
            try:
                stream = graph.astream_events(input_state, config=config, version="v2")
                async for _ in tee_events(stream, collector):
                    pass
            except CassetteMiss:
                raise
            except Exception as exc:
                app_logger.warning(
                    f"scenario {case_id} turn {index + 1} raised: {type(exc).__name__}: {exc}"
                )
                turn_error = f"{type(exc).__name__}: {exc}"
                error = turn_error

            turn_records.append(
                {
                    "turn": index + 1,
                    "input": turn.get("input") or scenario.get("input") or "",
                    "cost_delta": _cost_delta(before, _cost_snapshot(collector)),
                    "error": turn_error,
                }
            )

            # Snapshot the step at each turn boundary. The turn-layer assertions
            # need "how far did this turn move the state machine", which is not
            # recoverable from the final state alone.
            try:
                turn_snapshot = await graph.aget_state(config)
                values = dict(getattr(turn_snapshot, "values", {}) or {})
                turn_records[-1]["current_step"] = values.get("current_step")
            except Exception:
                pass

        final_state: dict[str, Any] = {}
        try:
            snapshot = await graph.aget_state(config)
            final_state = dict(getattr(snapshot, "values", {}) or {})
        except Exception as exc:
            app_logger.debug(f"final state unavailable for {case_id}: {exc}")

        # Attribute the terminal step to the record for per-turn assertions.
        record = collector.to_record(final_state)
        record["stubbed_tools"] = stubbed_tools
        record["turns"] = turn_records

        saved = cassette.save()
        return ScenarioOutcome(
            case_id=case_id,
            record=record,
            final_state=final_state,
            stub_calls=recorder.calls,
            error=error,
            turns=turn_records,
            latency_ms=round((time.time() - started) * 1000, 2),
            cassette={**cassette.summary(), "saved": str(saved) if saved else None},
            faults=injector.to_dict() if injector is not None else {},
        )
    finally:
        graph_module.get_step_config = original_get_step_config
        if original_get_memory is not None:
            graph_module.get_user_memory_service = original_get_memory


def _cost_snapshot(collector: TraceCollector) -> dict[str, int]:
    return {
        "llm_calls": collector.llm_calls,
        "input_tokens": collector.input_tokens,
        "output_tokens": collector.output_tokens,
        "tool_calls": len(collector._tool_order),  # noqa: SLF001 - internal by design
    }


def _cost_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


async def run_scenario(
    scenario: dict[str, Any],
    *,
    mode: str = "deterministic",
    model_factory: Any = None,
    dataset_version: str | None = None,
    dataset_split: str | None = None,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
    injector: FaultInjector | None = None,
) -> ScenarioOutcome:
    """Run one scenario end to end (single-turn special case of ``run_turns``)."""

    return await run_turns(
        scenario,
        [
            {
                "input": scenario.get("input") or "",
                "script": scenario.get("script") or [],
                "final_content": scenario.get("final_content"),
            }
        ],
        mode=mode,
        model_factory=model_factory,
        dataset_version=dataset_version,
        dataset_split=dataset_split,
        cassette_mode=cassette_mode,
        cassette_dir=cassette_dir,
        code_sha=code_sha,
        injector=injector,
    )
