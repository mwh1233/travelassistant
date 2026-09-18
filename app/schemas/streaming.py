"""Streaming response event schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


StreamEventType = Literal[
    "token",
    "step_started",
    "step_completed",
    "tool_call",
    "tool_result",
    "itinerary_delta",
    "budget_update",
    "map_marker",
    "approval_required",
    "error",
    "done",
]


class StreamEvent(BaseModel):
    """Canonical SSE event payload."""

    type: StreamEventType
    conversation_id: str
    step: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

