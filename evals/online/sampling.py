"""Deterministic sampling for the online triage path.

Sampling has to be **reproducible**: if a reviewer asks "why did (or didn't) this
trace get sampled?", the answer must be a pure function of the trace, not a coin
flip re-rolled on every import. A salted hash of the case id gives exactly that,
and lets the rate be tuned without re-labelling what was already reviewed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

SAMPLING_VERSION = "1.0"
DEFAULT_SALT = "travelassistant-online-1.0"


def sample_decision(key: str, rate: float, *, salt: str = DEFAULT_SALT) -> bool:
    """Deterministic Bernoulli draw. Same ``key`` + ``salt`` always agrees."""

    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{salt}:{key}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate


@dataclass
class SamplingPolicy:
    rate: float = 0.1
    salt: str = DEFAULT_SALT
    version: str = SAMPLING_VERSION
    #: Traces matching one of these reasons are always kept, whatever the rate
    #: (D09 §4.3: the dangerous events are invisible in aggregate metrics).
    always_keep: tuple[str, ...] = ("policy_violation", "jailbreak_attempt", "prompt_leak_attempt")

    def decides(self, key: str) -> bool:
        return sample_decision(key, self.rate, salt=self.salt)

    def should_keep(self, key: str, reasons: Iterable[str] = ()) -> bool:
        if _matches_always_keep(reasons, self.always_keep):
            return True
        return self.decides(key)


def _normalise(reason: str) -> str:
    """``turn:jailbreak_attempt`` and ``jailbreak_attempt`` are the same event.

    Reasons arrive namespaced from different producers (``turn:`` from the turn
    classifier, ``gate:`` from a release gate). Normalising here means
    ``always_keep`` never silently stops matching because a producer changed its
    prefix — which would quietly drop the most dangerous traces.
    """

    text = str(reason or "").strip()
    return text.rsplit(":", 1)[-1] if ":" in text else text


def _matches_always_keep(reasons: Iterable[str], always_keep: Iterable[str]) -> bool:
    wanted = {_normalise(item) for item in always_keep}
    return any(_normalise(reason) in wanted for reason in reasons)


def sample_records(
    records: Iterable[dict[str, Any]],
    policy: SamplingPolicy,
    *,
    key_field: str = "case_id",
    reason_field: str = "reasons",
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        key = str(record.get(key_field) or record.get("run_id") or index)
        reasons = record.get(reason_field) or []
        if policy.should_keep(key, reasons):
            kept.append(record)
    return kept
