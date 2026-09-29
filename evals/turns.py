"""Per-turn signals: a millisecond-scale classifier for the turn layer.

D09 §4.3 says the turn-layer events — 越狱尝试、系统提示词泄露、政策违背、用户
挫败 — **do not change status codes, latency or token counts**, so they are
invisible in logs and traces. They have to be classified explicitly.

D09 §2 also notes the online side cannot afford an LLM judge on every turn, so
the production shape is a fast classifier. This module ships the **deterministic
baseline** of that classifier: rule-based, explainable, free, and therefore
usable in CI. Any learned/LLM classifier is a drop-in replacement that must be
calibrated against human labels before it may gate anything (see
``evals/judge/calibration.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Bump when the rule set changes; labels are compared across versions.
TURN_CLASSIFIER_VERSION = "1.0"

#: The four turn-layer events from D09 §4.3, plus a neutral bucket.
TURN_LABELS = (
    "normal",
    "jailbreak_attempt",
    "prompt_leak_attempt",
    "policy_violation",
    "user_frustration",
)


@dataclass
class TurnSignal:
    turn: int
    label: str
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "label": self.label,
            "evidence": self.evidence,
            "scores": self.scores,
            "text": self.text[:200],
        }


# ---------------------------------------------------------------------------
# rule tables
# ---------------------------------------------------------------------------

_JAILBREAK_PATTERNS = (
    r"忽略(之前|上面|以上)的?(所有)?(指令|规则|提示)",
    r"你现在(是|扮演)",
    r"假装你是",
    r"进入?\s*开发者模式",
    r"developer\s*mode",
    r"不要遵守",
    r"绕过(你的)?(限制|规则|安全)",
    r"jailbreak",
    r"越狱",
)

_PROMPT_LEAK_PATTERNS = (
    r"(重复|复述|输出|告诉我|打印).{0,8}(系统)?(提示词|prompt|指令|设定)",
    r"你的(系统)?(提示词|prompt)是什么",
    r"repeat your (system )?(prompt|instructions)",
    r"system\s*prompt",
)

_POLICY_PATTERNS = (
    r"(不用|不需要|别)(问|确认|征求)",
    r"直接(帮我)?(下单|付款|支付|预订|购买|订)",
    r"不用(我)?(同意|授权|批准)",
    r"替我(签名|付款|支付)",
    r"跳过(审批|确认)",
)

_FRUSTRATION_PATTERNS = (
    r"(怎么|为什么)(还|又)?(不|没)",
    r"我说了(好几|两|三)遍",
    r"你到底",
    r"算了",
    r"太(慢|差|烂)了",
    r"重(新)?(再)?说(一遍)?",
)


def classify_turn(text: str, turn: int = 1) -> TurnSignal:
    """Classify one user turn. Deterministic, explainable, no model call."""

    haystack = (text or "").lower()
    scores: dict[str, float] = {}
    evidence: list[str] = []

    def scan(patterns: Iterable[str], bucket: str) -> None:
        hits = [pattern for pattern in patterns if re.search(pattern, haystack)]
        scores[bucket] = float(len(hits))
        evidence.extend(hits)

    scan(_JAILBREAK_PATTERNS, "jailbreak_attempt")
    scan(_PROMPT_LEAK_PATTERNS, "prompt_leak_attempt")
    scan(_POLICY_PATTERNS, "policy_violation")
    scan(_FRUSTRATION_PATTERNS, "user_frustration")

    # Precedence: a jailbreak outranks a leak outranks a policy push outranks
    # frustration. Deterministic order keeps the label reproducible.
    label = "normal"
    for candidate in ("jailbreak_attempt", "prompt_leak_attempt", "policy_violation", "user_frustration"):
        if scores.get(candidate):
            label = candidate
            break

    return TurnSignal(turn=turn, label=label, evidence=evidence[:6], scores=scores, text=text or "")


def classify_conversation(turns: list[str]) -> list[TurnSignal]:
    return [classify_turn(text, index + 1) for index, text in enumerate(turns)]


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def turn_layer_summary(signals: list[TurnSignal]) -> dict[str, Any]:
    """Roll turns up into the metrics D09 §4.3 asks to report."""

    counts: dict[str, int] = {label: 0 for label in TURN_LABELS}
    for signal in signals:
        counts[signal.label] = counts.get(signal.label, 0) + 1

    total = len(signals) or 1
    flagged = [
        signal for signal in signals if signal.label not in ("normal",)
    ]
    return {
        "classifier_version": TURN_CLASSIFIER_VERSION,
        "turns": len(signals),
        "counts": counts,
        "flag_rate": round(len(flagged) / total, 4),
        "flagged_turns": [signal.to_dict() for signal in flagged],
    }
