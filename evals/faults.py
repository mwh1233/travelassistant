"""Fault injection: what the agent does when a tool it depends on breaks.

D09 §6 is explicit that a capability boundary can only be found by *pushing to
it*: 能力刻画 requires dimensions like 任务长度 / 前置依赖 / 故障注入. Happy-path
scenarios measure the middle of the distribution, never the edge.

Three rules make injected faults useful instead of decorative:

1. **The fault is planned, not random.** It lives in the dataset row, so a
   failure is reproducible and reviewable. Random fault injection produces
   unreproducible results, which cannot gate anything.
2. **The fault must be *observable* in the trace.** A fault that the harness
   swallows proves nothing. :func:`evaluate_fault_scenario` fails a scenario
   whose declared fault never fired — otherwise the suite silently graduates
   from "tests degradation" to "tests nothing".
3. **The interesting failure is a *silent* one.** A tool that raises is easy;
   a tool that returns ``""`` or a truncated payload and gets treated as data
   is how a planner produces a confident, wrong itinerary. That is why payload
   faults (``empty`` / ``malformed`` / ``partial``) grade the *absence of
   fabrication*, not merely the absence of a crash.

The kinds map onto the ways a real dependency fails, and each one has a
characteristic correct response:

===============  ========================  ==============================
kind             shape                     correct response
===============  ========================  ==============================
``timeout``      raises TimeoutError       retry, then degrade honestly
``error``        raises RuntimeError       surface it; do not retry blindly
``empty``        returns ``""``            treat as "no data"; never invent
``malformed``    returns broken payload    do not parse it as fact
``partial``      returns truncated payload  flag incompleteness
``slow``         returns valid + delay     still succeeds, latency recorded
===============  ========================  ==============================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

FAULTS_VERSION = "1.0"

KIND_TIMEOUT = "timeout"
KIND_ERROR = "error"
KIND_SOFT_ERROR = "soft_error"
KIND_EMPTY = "empty"
KIND_MALFORMED = "malformed"
KIND_PARTIAL = "partial"
KIND_SLOW = "slow"

#: Kinds that fail by raising. Everything else fails by returning misleading
#: *content*, which is strictly harder to detect.
RAISING_KINDS = (KIND_TIMEOUT, KIND_ERROR)

KINDS = (
    KIND_TIMEOUT,
    KIND_ERROR,
    KIND_SOFT_ERROR,
    KIND_EMPTY,
    KIND_MALFORMED,
    KIND_PARTIAL,
    KIND_SLOW,
)

_DEFAULT_MESSAGES = {
    KIND_TIMEOUT: "TimeoutError: 外部服务 30s 无响应",
    KIND_ERROR: "RuntimeError: 外部服务返回 500",
    # A retryable error returned *as content* — the shape a real HTTP client
    # gives you when it does not raise. The agent has a chance to recover.
    KIND_SOFT_ERROR: '{"error": "rate_limit_exceeded", "retryable": true}',
    KIND_EMPTY: "",
    KIND_MALFORMED: "{not-json",
    KIND_PARTIAL: '{"options": [{"mode": "train"',  # truncated mid-object
    KIND_SLOW: "OK",
}

_EXCEPTION_TYPES: dict[str, type[BaseException]] = {
    KIND_TIMEOUT: TimeoutError,
    KIND_ERROR: RuntimeError,
}


@dataclass
class FaultSpec:
    """One planned fault against one tool."""

    kind: str
    target: str
    """Tool name, or ``"*"`` for any stubbed tool."""
    times: int = 1
    """How many consecutive matching calls are faulted (``0`` = unlimited)."""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "times": self.times, "message": self.message}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FaultSpec":
        return cls(
            kind=str(payload.get("kind") or KIND_ERROR),
            target=str(payload.get("target") or "*"),
            times=int(payload.get("times", 1)),
            message=str(payload.get("message") or ""),
        )

    @property
    def payload(self) -> str:
        return self.message or _DEFAULT_MESSAGES.get(self.kind, "fault")


def parse_faults(raw: Iterable[Any]) -> list[FaultSpec]:
    """Accept dicts or ``FaultSpec``s from a dataset row."""

    specs: list[FaultSpec] = []
    for item in raw or []:
        if isinstance(item, FaultSpec):
            specs.append(item)
        elif isinstance(item, dict):
            specs.append(FaultSpec.from_dict(item))
    return specs


@dataclass
class FaultEvent:
    """A fault that actually fired. Recorded so the grader can prove it."""

    tool: str
    call_index: int
    kind: str
    raised: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "call_index": self.call_index,
            "kind": self.kind,
            "raised": self.raised,
            "message": self.message[:160],
        }


class FaultInjector:
    """Deterministic, per-tool call counter that fires each spec in order.

    Determinism is the whole point: the same scenario must fault the same call
    every run, or the resulting failure rate is not a property of the agent.
    """

    def __init__(self, specs: Iterable[FaultSpec] = (), sink: list[FaultEvent] | None = None):
        self.specs = list(specs)
        self.events: list[FaultEvent] = sink if sink is not None else []
        self._matched: dict[int, int] = {index: 0 for index in range(len(self.specs))}
        self._calls: dict[str, int] = {}

    @property
    def active(self) -> bool:
        return bool(self.specs)

    def next_fault(self, tool: str, args: dict[str, Any] | None = None) -> FaultEvent | None:
        """Return the fault for this call, or ``None`` to let the stub answer."""

        call_index = self._calls.get(tool, 0) + 1
        self._calls[tool] = call_index

        for index, spec in enumerate(self.specs):
            if spec.target not in ("*", tool):
                continue
            used = self._matched[index]
            if spec.times and used >= spec.times:
                continue
            self._matched[index] = used + 1
            event = FaultEvent(
                tool=tool,
                call_index=call_index,
                kind=spec.kind,
                raised=spec.kind in RAISING_KINDS,
                message=spec.payload,
            )
            self.events.append(event)
            return event
        return None

    # ---------- convenience ----------

    def calls(self, tool: str) -> int:
        return self._calls.get(tool, 0)

    def events_for(self, tool: str) -> list[FaultEvent]:
        return [event for event in self.events if event.tool == tool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FAULTS_VERSION,
            "specs": [spec.to_dict() for spec in self.specs],
            "events": [event.to_dict() for event in self.events],
            "fired": len(self.events),
        }


def apply_fault(event: FaultEvent) -> str:
    """Turn a fault event into the stub's behaviour (raise or return)."""

    if event.raised:
        exception = _EXCEPTION_TYPES.get(event.kind, RuntimeError)
        raise exception(event.message)
    return event.message


# ---------------------------------------------------------------------------
# grading a fault scenario
# ---------------------------------------------------------------------------


@dataclass
class FaultExpectation:
    """What a correct response looks like, declared per scenario.

    Everything is optional. An unspecified expectation is *not* asserted — a
    check that cannot fail is worse than no check, because it inflates the
    appearance of coverage.
    """

    retry: bool = False
    """The faulted tool must be called again after the fault."""
    max_calls: dict[str, int] = field(default_factory=dict)
    """Upper bound per tool, so "retry" cannot become "loop forever"."""
    no_fabrication: tuple[str, ...] = ()
    """State keys that must stay empty when their data source failed."""
    allowed_terminal_steps: tuple[str, ...] = ()
    """Where the run may legitimately stop after an unrecovered fault."""
    require_error_signal: bool = False
    """The trace must carry a visible error / failed tool call."""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "FaultExpectation":
        payload = payload or {}
        return cls(
            retry=bool(payload.get("retry", False)),
            max_calls={str(k): int(v) for k, v in (payload.get("max_calls") or {}).items()},
            no_fabrication=tuple(payload.get("no_fabrication") or ()),
            allowed_terminal_steps=tuple(payload.get("allowed_terminal_steps") or ()),
            require_error_signal=bool(payload.get("require_error_signal", False)),
        )


def evaluate_fault_scenario(
    *,
    case_id: str,
    specs: list[FaultSpec],
    injector: FaultInjector,
    record: dict[str, Any],
    final_state: dict[str, Any],
    tool_names: list[str],
    expectation: FaultExpectation,
    negative_control: bool = False,
    expect_failures: list[str] | None = None,
) -> dict[str, Any]:
    """Grade one fault scenario. Returns check dicts + a summary."""

    checks: list[dict[str, Any]] = []

    def add(check_id: str, title: str, passed: bool, detail: str) -> None:
        checks.append(
            {"check_id": check_id, "title": title, "passed": passed, "detail": detail}
        )

    # 1. the scenario must actually inject something
    add(
        "L6.fault_injected",
        "场景确实注入了故障",
        bool(injector.events) or not specs,
        (
            f"已注入 {len(injector.events)} 次"
            if injector.events
            else "声明的故障一次都没有触发 — 该用例什么都没测到"
        ),
    )

    # 2. the fault must be visible at the tool layer, and a raising fault must
    #    additionally show up as an error. A payload fault has no error to raise
    #    — its whole danger is that it looks like a normal answer — so for those
    #    the assertion is "the call is in the trace", which is what makes the
    #    no_fabrication check below meaningful.
    raised = [event for event in injector.events if event.raised]
    errors = record.get("errors") or []
    tool_calls = record.get("tool_calls") or []
    failed_tools = [call for call in tool_calls if not call.get("ok", True)]
    faulted_tool_names = {event.tool for event in injector.events}
    traced = [call for call in tool_calls if call.get("name") in faulted_tool_names]

    if injector.events:
        if raised or expectation.require_error_signal:
            surfaced = bool(errors) or bool(failed_tools) or bool(traced)
            detail = (
                f"errors={len(errors)}｜失败工具调用={len(failed_tools)}｜命中工具={len(traced)}"
                if surfaced
                else "注入了会抛错的故障，但 trace 里既没有 error 也没有该工具的调用记录"
            )
        else:
            surfaced = bool(traced)
            detail = (
                f"命中工具调用 {len(traced)} 次（载荷型故障不抛错，可见性即其调用记录）"
                if surfaced
                else "载荷型故障没有出现在 trace 里，说明它根本没到达模型"
            )
        add("L6.fault_surfaced", "故障在 trace 中可见（未被静默吞掉）", surfaced, detail)

    # 3. payload faults must not become data
    if expectation.no_fabrication:
        fabricated = [
            key
            for key in expectation.no_fabrication
            if _present(final_state.get(key))
        ]
        add(
            "L6.no_fabrication",
            "数据源故障时不得凭空填充下游字段",
            not fabricated,
            (
                "故障字段保持为空"
                if not fabricated
                else f"这些字段在数据源故障的情况下仍有值: {', '.join(fabricated)}"
            ),
        )

    # 4. recovery vs bounded retry
    if expectation.retry:
        recovered = any(injector.calls(name) > 0 for name in _faulted_tools(injector))
        retried = any(
            injector.calls(name) > len(injector.events_for(name))
            for name in _faulted_tools(injector)
        )
        add(
            "L6.recovery_attempted",
            "故障后发生了重试",
            recovered and retried,
            (
                "故障后重新调用了失败的工具"
                if recovered and retried
                else f"未重试；调用次数 {injector._calls}"  # noqa: SLF001 - diagnostic only
            ),
        )

    for tool, bound in expectation.max_calls.items():
        actual = injector.calls(tool)
        add(
            f"L6.bounded_retry[{tool}]",
            f"{tool} 调用次数不超过 {bound}（重试不得退化为死循环）",
            actual <= bound,
            f"实际调用 {actual} 次",
        )

    # 5. graceful degradation target
    if expectation.allowed_terminal_steps:
        step = str(final_state.get("current_step") or "")
        add(
            "L6.graceful_degradation",
            "故障未恢复时停在允许的步骤",
            step in expectation.allowed_terminal_steps,
            (
                f"停留在 {step}"
                if step in expectation.allowed_terminal_steps
                else f"停留在 {step!r}，允许集合 {list(expectation.allowed_terminal_steps)}"
            ),
        )

    failed = [check["check_id"] for check in checks if not check["passed"]]

    # A negative control asserts that the grader itself bites: exactly the
    # declared checks must fail, and nothing else. Same rule as the trajectory
    # suite, so "the check works" is proven in the same vocabulary everywhere.
    expect_failures = sorted(set(expect_failures or []))
    if negative_control:
        unexpected = [item for item in failed if item not in expect_failures]
        missing = [item for item in expect_failures if item not in failed]
        passed = not unexpected and not missing
        if passed:
            summary = "负向对照成立：" + ", ".join(expect_failures) + " 如期失败"
        else:
            parts = []
            if unexpected:
                parts.append("不该失败却失败: " + ", ".join(unexpected))
            if missing:
                parts.append("应当失败却没失败: " + ", ".join(missing))
            summary = "负向对照失效｜" + "；".join(parts)
    else:
        passed = not failed
        summary = "全部通过" if not failed else "失败项: " + ", ".join(failed)

    return {
        "case_id": case_id,
        "checks": checks,
        "failed_checks": failed,
        "passed": passed,
        "summary": summary,
        "negative_control": bool(negative_control),
        "expect_failures": expect_failures,
        "faults": injector.to_dict(),
        "tool_names": list(tool_names),
    }


def _faulted_tools(injector: FaultInjector) -> list[str]:
    tools: list[str] = []
    for event in injector.events:
        if event.tool not in tools:
            tools.append(event.tool)
    return tools


def _present(value: Any) -> bool:
    """A state value counts as "populated" unless it is empty/None/zero-ish."""

    if value is None:
        return False
    if isinstance(value, (str, list, tuple, set, dict)):
        return len(value) > 0
    return True
