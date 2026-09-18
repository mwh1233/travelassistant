"""Human approval tools for high-risk travel actions."""

import uuid
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.core.state import TravelState
from app.schemas.approval import ApprovalRequest


@tool
def request_action_approval(
    action_type: str,
    title: str,
    summary: str,
    tool_name: str,
    risk_level: Literal["write", "purchase", "payment"] = "purchase",
    tool_input: dict[str, Any] | None = None,
    estimated_amount: float | None = None,
    runtime: ToolRuntime[None, TravelState] = None,
) -> Command:
    """Create an approval request before write, purchase, or payment actions."""

    approval = ApprovalRequest(
        action_id=f"approval-{uuid.uuid4().hex[:12]}",
        action_type=action_type,
        risk_level=risk_level,
        title=title,
        summary=summary,
        tool_name=tool_name,
        tool_input=tool_input or {},
        estimated_amount=estimated_amount,
    )

    return Command(update={
        "messages": [
            ToolMessage(
                content=f"需要用户确认：{approval.title}\n{approval.summary}",
                tool_call_id=(runtime.tool_call_id if runtime else ""),
            )
        ],
        "approval_pending": True,
        "approval_reason": approval.summary,
        "pending_approval": approval.model_dump(),
    })


@tool
def record_action_approval(
    approved: bool,
    reason: str = "",
    runtime: ToolRuntime[None, TravelState] = None,
) -> Command:
    """Record the user's approval decision."""

    status = "已确认" if approved else "已拒绝"
    return Command(update={
        "messages": [
            ToolMessage(
                content=f"{status}：{reason}".strip("："),
                tool_call_id=(runtime.tool_call_id if runtime else ""),
            )
        ],
        "approval_pending": False,
        "approval_reason": reason,
        "approval_decision": {
            "approved": approved,
            "reason": reason,
        },
    })


APPROVAL_TOOLS = [request_action_approval, record_action_approval]
