"""A deterministic, scripted chat model for evaluation runs.

Mock mode exists so the whole graph — routing, tool selection, tool execution,
state writes, rollback — can be exercised on every commit with **zero** model
cost and zero flakiness. The model is the only thing that gets faked; the graph
and the tools are the real ones.

The model replays a fixed list of responses, one per generation call. A
response that carries ``tool_calls`` makes the agent execute real tools; a
response with plain content ends the turn. When the script is exhausted the
model answers with a terminal message so the agent loop always terminates.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class ScriptedChatModel(BaseChatModel):
    """Replay a scripted sequence of ``AIMessage`` objects."""

    responses: list[AIMessage] = Field(default_factory=list)
    default_content: str = "（脚本已结束）"

    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":  # noqa: D102
        # The script already knows which tool to call; tool schemas are irrelevant.
        return self

    def _next_message(self) -> AIMessage:
        if self._cursor < len(self.responses):
            message = self.responses[self._cursor]
            self._cursor += 1
            return message
        return AIMessage(content=self.default_content)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message())])


def tool_call_response(
    tool_name: str,
    args: dict[str, Any] | None = None,
    call_id: str = "call-1",
) -> AIMessage:
    """Build an assistant message that requests one tool call."""

    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool_name,
                "args": args or {},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def build_responses(steps: Sequence[dict[str, Any]], final_content: str = "") -> list[AIMessage]:
    """Turn a scenario ``script`` into a list of scripted model responses.

    Each script entry is ``{"tool": ..., "args": {...}}``; the trailing entry is
    a plain assistant message so the agent loop terminates.
    """

    responses: list[AIMessage] = []
    for index, step in enumerate(steps):
        if "content" in step and "tool" not in step:
            responses.append(AIMessage(content=step["content"]))
            continue
        responses.append(
            tool_call_response(
                tool_name=step["tool"],
                args=step.get("args") or {},
                call_id=step.get("call_id") or f"call-{index + 1}",
            )
        )
    responses.append(AIMessage(content=final_content or "好的，已按你的要求处理。"))
    return responses
