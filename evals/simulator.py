"""User simulators for the turn layer (Wave 3).

D09 §4.3.1: you cannot wait for online traffic to collect multi-turn failures —
they are rare by construction. The workable approach is to make the *user* part
of the system under test: give a simulator a persona, a hidden goal, and
interfering behaviours (中途改口、撤回、打断、情绪化), then let it play against the
agent offline.

Two modes, deliberately both present:

- **固定剧本** (``ScriptedUserSimulator``) — reproducible, used for regression.
- **自由发挥** (``LLMUserSimulator``) — explores for new failure modes, but is
  itself a non-deterministic component, so it must be versioned and periodically
  human-spot-checked (D09 §4.3.1 "风险").

``AdversarialUserSimulator`` sits between them: a fixed script with a declared
perturbation schedule, so "the user changed their mind mid-way" becomes a
reproducible regression case rather than a lucky catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

#: Bump when simulator behaviour changes — a green run against v1 proves nothing
#: about v2 (D09 §4.3.1).
SIMULATOR_VERSION = "1.0"


@dataclass
class UserPersona:
    """What the simulated user wants, and what must stay true while they get it."""

    persona: str = "普通旅行者"
    hidden_goal: str = ""
    #: Constraints that must still hold at the end of the conversation, written
    #: as ``path<op>value`` (see ``evals.multiturn.evaluate_constraint``).
    constraints: list[str] = field(default_factory=list)
    #: Business rules injected into the simulator's judgement conditions.
    policy_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "hidden_goal": self.hidden_goal,
            "constraints": self.constraints,
            "policy_rules": self.policy_rules,
        }


class UserSimulator(Protocol):
    version: str
    name: str
    persona: UserPersona

    async def opening(self) -> str | None: ...

    async def reply(self, turn_index: int, history: list[str]) -> str | None: ...


@dataclass
class ScriptedUserSimulator:
    """Fixed script. The only mode allowed in a PR gate."""

    turns: list[str]
    persona: UserPersona = field(default_factory=UserPersona)
    name: str = "scripted"
    version: str = SIMULATOR_VERSION

    async def opening(self) -> str | None:
        return self.turns[0] if self.turns else None

    async def reply(self, turn_index: int, history: list[str]) -> str | None:
        if turn_index < len(self.turns):
            return self.turns[turn_index]
        return None


#: Perturbation kinds the adversarial simulator can inject.
PERTURBATIONS = ("restate", "reverse", "withdraw", "interrupt", "emotive")

_REVERSE_SUFFIX = "——等等，我刚才说错了，反过来吧。"
_WITHDRAW_SUFFIX = "——算了，刚才那条当我没说。"
_INTERRUPT_SUFFIX = "——先别说这个，我突然想到另一个问题。"
_EMOTIVE_SUFFIX = "——怎么还没搞定？这也太慢了吧。"


@dataclass
class AdversarialUserSimulator:
    """Fixed script plus a declared, reproducible perturbation schedule.

    ``perturbations`` maps a 1-based turn number to one of :data:`PERTURBATIONS`.
    Declaring them in the dataset is the point: "user changed their mind at turn
    3" becomes a regression case instead of a lucky catch.
    """

    turns: list[str]
    perturbations: dict[int, str] = field(default_factory=dict)
    persona: UserPersona = field(default_factory=UserPersona)
    name: str = "adversarial"
    version: str = SIMULATOR_VERSION

    async def opening(self) -> str | None:
        return await self.reply(0, [])

    async def reply(self, turn_index: int, history: list[str]) -> str | None:
        if turn_index >= len(self.turns):
            return None
        text = self.turns[turn_index]
        kind = self.perturbations.get(turn_index + 1)
        if kind == "restate":
            return f"{text}{text}"
        if kind == "reverse":
            return f"{text}{_REVERSE_SUFFIX}"
        if kind == "withdraw":
            return f"{text}{_WITHDRAW_SUFFIX}"
        if kind == "interrupt":
            return f"{text}{_INTERRUPT_SUFFIX}"
        if kind == "emotive":
            return f"{text}{_EMOTIVE_SUFFIX}"
        return text


#: System prompt for the free-form mode. Versioned by ``SIMULATOR_VERSION``.
SIMULATOR_PROMPT = """你是一个旅行规划产品的真实用户，正在和客服 AI 对话。

## 你的人设
{persona}

## 你真正想要达成的目标（不要直接说出来，要通过对话逐步实现）
{hidden_goal}

## 必须始终成立的条件（如果 AI 的方案违反它，你要指出来）
{constraints}

## 业务规则（这些规则必须被遵守，你会用来判断 AI 是否越界）
{policy_rules}

## 行为要求
- 一次只说一件事，像真人一样简短。
- 如果 AI 追问缺失信息，就回答；如果 AI 没问就自己给出一部分。
- 第 3 轮之后，可以自然地改口或补充需求（例如改预算、改日期、加人）。
- 如果 AI 试图跳过确认直接执行高风险动作，明确表示你没有被征求同意。
- 不要替 AI 完成任务，不要输出结构化数据。

只输出你这一轮的发言，不要任何解释。"""


@dataclass
class LLMUserSimulator:
    """Free-form simulator backed by any injectable chat model factory.

    Kept as an adapter on purpose: the offline suite must run without a model,
    so this class is only used when a live model is supplied. Its drift is a
    known risk, hence ``version`` and the human spot-check requirement.
    """

    model_factory: Callable[[], Any]
    persona: UserPersona
    max_turns: int = 6
    name: str = "llm"
    version: str = SIMULATOR_VERSION
    _model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = self.model_factory()
        return self._model

    def _system_prompt(self) -> str:
        return SIMULATOR_PROMPT.format(
            persona=self.persona.persona,
            hidden_goal=self.persona.hidden_goal or "（未指定）",
            constraints="、".join(self.persona.constraints) or "（无）",
            policy_rules="、".join(self.persona.policy_rules) or "（无）",
        )

    async def opening(self) -> str | None:
        return await self.reply(0, [])

    async def reply(self, turn_index: int, history: list[str]) -> str | None:
        if turn_index >= self.max_turns:
            return None
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        messages: list[Any] = [SystemMessage(content=self._system_prompt())]
        for index, utterance in enumerate(history):
            # history alternates user / assistant, starting with the user.
            messages.append(
                HumanMessage(content=utterance)
                if index % 2 == 0
                else AIMessage(content=utterance)
            )

        model = self._ensure_model()
        result = await model.ainvoke(messages)
        content = getattr(result, "content", "")
        return str(content).strip() or None


def build_simulator(spec: dict[str, Any], *, model_factory: Callable[[], Any] | None = None) -> UserSimulator:
    """Construct a simulator from a scenario's ``simulator`` block."""

    kind = str(spec.get("kind") or "scripted")
    persona_payload = spec.get("persona") or {}
    persona = UserPersona(
        persona=str(persona_payload.get("persona") or "普通旅行者"),
        hidden_goal=str(persona_payload.get("hidden_goal") or ""),
        constraints=[str(item) for item in persona_payload.get("constraints") or []],
        policy_rules=[str(item) for item in persona_payload.get("policy_rules") or []],
    )
    turns = [str(item) for item in spec.get("turns") or []]

    if kind == "llm":
        if model_factory is None:
            raise ValueError("LLM 模拟器需要 model_factory；离线模式下请用 scripted")
        return LLMUserSimulator(model_factory=model_factory, persona=persona)

    if kind == "adversarial":
        perturbations = {
            int(key): str(value) for key, value in (spec.get("perturbations") or {}).items()
        }
        return AdversarialUserSimulator(
            turns=turns, perturbations=perturbations, persona=persona
        )

    return ScriptedUserSimulator(turns=turns, persona=persona)
