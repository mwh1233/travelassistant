"""Structured trajectory capture for the travel planner.

Why this module exists
----------------------
``app/observability/tracing.py`` only writes log strings, so nothing is
retrievable or replayable. This module builds a **structured trajectory** by
tapping the event stream that ``chat.py`` already consumes, and can write it as
JSONL without changing any SSE behaviour.

Design notes
------------
- The collector is *passive*: it never raises into the caller's stream.
- ``state_writes`` is derived from the tools' ``Command.update`` payloads. It is
  the single most valuable field for regression assertions such as "did a
  rollback leave stale state behind?".
- Node-level spans are only recorded for the focused planning steps, so the
  trace stays small even though ``astream_events`` emits hundreds of events.

See ``docs/agent-eval-design.md`` (P0-1, section 6) for the rationale.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from app.utils.logger import app_logger


TRACE_SCHEMA_VERSION = "1.0"

#: Only these chain names produce a node-level span.
DEFAULT_FOCUS_CHAINS = {
    "requirement_collection",
    "destination_recommendation",
    "transport_planning",
    "accommodation_planning",
    "food_planning",
    "itinerary_generation",
    "budget_summarization",
    "order_generation",
}

#: Keys copied into ``final_state``. Keep this list explicit: dumping the whole
#: state drags in message objects that are not JSON serialisable.
STATE_SNAPSHOT_KEYS = (
    "current_step",
    "user_requirement",
    "selected_destination",
    "selected_transport",
    "selected_accommodation_types",
    "selected_food_types",
    "destination_options",
    "transport_options",
    "accommodation_options",
    "food_options",
    "itinerary",
    "budget",
    "structured_itinerary",
    "structured_budget",
    "source_references",
    "order_id",
    "approval_pending",
    "approval_reason",
    "pending_approval",
    "approval_decision",
    "user_id",
    "session_id",
)


def digest(value: Any, limit: int = 4000) -> str:
    """Return a short, stable digest for arbitrarily shaped tool output."""

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return "sha256:" + hashlib.sha256(text[:limit].encode("utf-8")).hexdigest()[:16]


def _safe(value: Any) -> Any:
    """Best-effort JSON-safe conversion."""

    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception:
        return str(value)


def _extract_state_write(output: Any) -> dict[str, Any] | None:
    """Return ``{tool, fields, after}`` when a tool returned a ``Command``.

    LangGraph's ``Command.update`` is exactly the contract that state-transition
    tools commit against, so it doubles as a machine-checkable state-write
    declaration.
    """

    update = getattr(output, "update", None)
    if not isinstance(update, dict):
        return None
    return {
        "fields": sorted(update.keys()),
        "after": {key: _safe(value) for key, value in update.items() if key != "messages"},
        "goto": getattr(output, "goto", None),
    }


@dataclass
class _ToolCall:
    name: str
    args: dict[str, Any]
    run_id: str
    parent_ids: list[str]
    started_at: float
    ended_at: float | None = None
    ok: bool = True
    error: str | None = None
    result_digest: str | None = None
    state_write: dict[str, Any] | None = None
    node: str | None = None
    tags: list[str] = field(default_factory=list)
    output: Any = None

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "args": _safe(self.args),
            "ok": self.ok,
            "parent_span": self.node,
            "tags": self.tags,
        }
        if self.ended_at is not None:
            payload["elapsed_ms"] = round((self.ended_at - self.started_at) * 1000, 2)
        if self.error:
            payload["error"] = self.error
        if self.result_digest:
            payload["result_digest"] = self.result_digest
        if self.state_write:
            payload["state_write"] = self.state_write
        if include_output and self.output is not None:
            payload["output"] = _safe(self.output)
        return payload


class TraceCollector:
    """Accumulate one evaluation case worth of trajectory."""

    def __init__(
        self,
        case_id: str | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        dataset_version: str | None = None,
        dataset_split: str | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
        model_params: dict[str, Any] | None = None,
        mode: str = "unknown",
        checkpointer_kind: str | None = None,
        focus_chains: Iterable[str] | None = None,
        code_sha: str | None = None,
        capture_tool_outputs: bool = False,
    ) -> None:
        self.run_id = str(uuid.uuid4())
        self.case_id = case_id or self.run_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.dataset_version = dataset_version
        self.dataset_split = dataset_split
        self.prompt_version = prompt_version
        self.model = model
        self.model_params = model_params or {}
        self.mode = mode
        self.checkpointer_kind = checkpointer_kind
        self.focus_chains = set(focus_chains or DEFAULT_FOCUS_CHAINS)
        self.code_sha = code_sha
        #: Replay needs the *full* tool return, not just a digest. Off by default
        #: because tool output may contain user data (D09 §7.6).
        self.capture_tool_outputs = capture_tool_outputs

        self.started_at = time.time()
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.errors: list[dict[str, Any]] = []

        self._nodes: dict[str, dict[str, Any]] = {}
        self._tool_calls: dict[str, _ToolCall] = {}
        self._tool_order: list[str] = []
        self._active_node: str | None = None
        #: node name -> per-node cost/latency. This is what turns "the agent is
        #: slow" into "the transport node costs 4 model calls and 1.8s".
        self._node_metrics: dict[str, dict[str, Any]] = {}
        self._node_started: dict[str, float] = {}
        self._node_durations: dict[str, float] = {}

    # ---------- event ingestion ----------

    def consume(self, event: dict[str, Any]) -> None:
        """Feed one ``astream_events`` payload. Never raises."""

        try:
            self._consume(event)
        except Exception as exc:  # pragma: no cover - defensive
            app_logger.debug(f"trace collector ignored malformed event: {exc}")

    def _consume(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        name = event.get("name") or ""
        run_id = str(event.get("run_id") or "")
        parent_ids = [str(item) for item in (event.get("parent_ids") or [])]

        if kind == "on_chain_start" and name in self.focus_chains:
            self._active_node = name
            self._node_started[name] = event.get("ts") or time.time()
            self._nodes[run_id] = {
                "node": name,
                "started_at": event.get("ts") or time.time(),
                "ended_at": None,
                "exit_step": None,
            }

        elif kind == "on_chain_end" and name in self.focus_chains:
            span = self._nodes.get(run_id)
            output = event.get("data", {}).get("output")
            ended = event.get("ts") or time.time()
            if span is not None:
                span["ended_at"] = ended
                if isinstance(output, dict):
                    span["exit_step"] = output.get("current_step")
            started = self._node_started.pop(name, None)
            if started is not None:
                self._node_durations[name] = self._node_durations.get(name, 0.0) + (
                    ended - started
                ) * 1000
            self._active_node = None

        elif kind == "on_tool_start":
            self._tool_calls[run_id] = _ToolCall(
                name=name,
                args=event.get("data", {}).get("input") or {},
                run_id=run_id,
                parent_ids=parent_ids,
                started_at=event.get("ts") or time.time(),
                node=self._active_node,
                tags=list(event.get("tags") or []),
            )
            self._tool_order.append(run_id)
            self._node_counter(self._active_node, "tool_calls")

        elif kind == "on_tool_end":
            call = self._tool_calls.get(run_id)
            output = event.get("data", {}).get("output")
            if call is None:
                # A tool span that never produced an on_tool_start (rare).
                call = _ToolCall(
                    name=name,
                    args={},
                    run_id=run_id,
                    parent_ids=parent_ids,
                    started_at=event.get("ts") or time.time(),
                    node=self._active_node,
                )
                self._tool_calls[run_id] = call
                self._tool_order.append(run_id)
            call.ended_at = event.get("ts") or time.time()
            call.result_digest = digest(output)
            call.state_write = _extract_state_write(output)
            if self.capture_tool_outputs:
                call.output = output

        elif kind == "on_tool_error":
            call = self._tool_calls.get(run_id)
            error = event.get("data", {}).get("error")
            if call is not None:
                call.ok = False
                call.ended_at = event.get("ts") or time.time()
                call.error = str(error)
            self.errors.append({"type": "tool_error", "tool": name, "error": str(error)})

        elif kind == "on_chat_model_start":
            self.llm_calls += 1
            self._node_counter(self._active_node, "llm_calls")

        elif kind == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            usage = getattr(output, "usage_metadata", None) or {}
            if isinstance(usage, dict):
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                self.input_tokens += input_tokens
                self.output_tokens += output_tokens
                self._node_counter(self._active_node, "input_tokens", input_tokens)
                self._node_counter(self._active_node, "output_tokens", output_tokens)

    def _node_counter(self, node: str | None, field_name: str, amount: int = 1) -> None:
        """Accumulate a per-node counter. Unknown nodes are ignored on purpose."""

        if not node:
            return
        bucket = self._node_metrics.setdefault(
            node,
            {
                "node": node,
                "llm_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "elapsed_ms": 0.0,
            },
        )
        bucket[field_name] += amount

    # ---------- output ----------

    @property
    def tool_names(self) -> list[str]:
        return [self._tool_calls[rid].name for rid in self._tool_order]

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [
            self._tool_calls[rid].to_dict(include_output=self.capture_tool_outputs)
            for rid in self._tool_order
        ]

    @property
    def steps(self) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for span in self._nodes.values():
            enriched = dict(span)
            node = span.get("node")
            started = span.get("started_at")
            ended = span.get("ended_at")
            if started and ended:
                enriched["elapsed_ms"] = round((ended - started) * 1000, 2)
            metrics = self._node_metrics.get(node or "")
            if metrics:
                enriched.update(
                    {
                        "llm_calls": metrics["llm_calls"],
                        "tool_calls": metrics["tool_calls"],
                        "input_tokens": metrics["input_tokens"],
                        "output_tokens": metrics["output_tokens"],
                    }
                )
            spans.append(enriched)
        return spans

    @property
    def node_metrics(self) -> list[dict[str, Any]]:
        """Per-node cost and latency, including chains that produced no span."""

        merged: dict[str, dict[str, Any]] = {}
        for node, metrics in self._node_metrics.items():
            merged[node] = {**metrics, "elapsed_ms": round(self._node_durations.get(node, 0.0), 2)}
        for node, duration in self._node_durations.items():
            merged.setdefault(
                node,
                {
                    "node": node,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            merged[node]["elapsed_ms"] = round(duration, 2)
        return [merged[node] for node in sorted(merged)]

    def state_writes(self) -> list[dict[str, Any]]:
        writes: list[dict[str, Any]] = []
        for rid in self._tool_order:
            call = self._tool_calls[rid]
            if call.state_write:
                writes.append(
                    {
                        "tool": call.name,
                        "fields": call.state_write["fields"],
                        "after": call.state_write["after"],
                    }
                )
        return writes

    def to_record(self, final_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build the JSONL record defined in docs/agent-eval-design.md (P0-1)."""

        snapshot: dict[str, Any] = {}
        if isinstance(final_state, dict):
            for key in STATE_SNAPSHOT_KEYS:
                if key in final_state:
                    snapshot[key] = _safe(final_state[key])

        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "versions": {
                "code_sha": self.code_sha,
                "dataset_version": self.dataset_version,
                "dataset_split": self.dataset_split,
                "prompt_version": self.prompt_version,
                "model": self.model,
                "model_params": self.model_params,
            },
            "mode": self.mode,
            "checkpointer": self.checkpointer_kind,
            "steps": self.steps,
            "node_metrics": self.node_metrics,
            "tool_calls": self.tools,
            "state_writes": self.state_writes(),
            "errors": self.errors,
            "final_state": snapshot,
            "cost": {
                "llm_calls": self.llm_calls,
                "tool_calls": len(self._tool_order),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_ms": round((time.time() - self.started_at) * 1000, 2),
            },
        }


async def tee_events(
    events: AsyncIterator[dict[str, Any]],
    collector: TraceCollector,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every event unchanged while feeding a copy to the collector."""

    async for event in events:
        collector.consume(event)
        yield event


def write_jsonl(record: dict[str, Any], path: str | Path) -> Path:
    """Append one trajectory record to a JSONL file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return target
