"""Approval models for high-risk agent actions."""

from typing import Any, Literal

from pydantic import BaseModel, Field


ApprovalRiskLevel = Literal["read", "write", "purchase", "payment"]


class ApprovalRequest(BaseModel):
    """Payload emitted when a tool action needs user approval."""

    action_id: str
    action_type: str
    risk_level: ApprovalRiskLevel
    title: str
    summary: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    estimated_amount: float | None = None
    currency: str = "CNY"

