"""Redaction: strip user data and credentials *before* a trace leaves the box.

D09 §7.6 is blunt about this: online traces carry user text, identifiers and
occasionally credentials, and anything sent to a judge, an observability backend
or a labelling queue has to be sanitised first, with a stated retention window.

This module is deliberately conservative and dependency-free: regex redaction of
the obvious identifiers, plus hashing of opaque ids so samples stay joinable
without being invertible.

It is **not** a compliance guarantee. It is the mechanical first pass that makes
"can this sample leave the process?" answerable at all.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

#: Bump when a pattern is added or changed; redacted samples record the version
#: so a downstream consumer knows which rules were applied.
REDACTION_VERSION = "1.0"

#: Ordered most-specific-first so a card number is not partially eaten by the
#: generic digit rule, and — critically — a URL credential is not half-eaten by
#: the email rule (``user:pass@host`` looks like an address until you notice the
#: scheme in front of it).
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("url_creds", re.compile(r"://[^/\s:@]+:[^/\s:@]+@"), "://[REDACTED]@"),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    ("id_card", re.compile(r"\b\d{17}[\dXx]\b"), "[ID]"),
    ("bank_card", re.compile(r"\b\d{16,19}\b"), "[CARD]"),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    ("api_key", re.compile(r"\b(?:sk|ak|pk|ghp|xoxb|Bearer)[-_ ][A-Za-z0-9_\-]{12,}\b", re.I), "[SECRET]"),
    ("token", re.compile(r"\b(?:token|secret|password|passwd|pwd)\s*[:=]\s*\S+", re.I), "[SECRET]"),
)

#: Keys whose *value* is an opaque identifier: hashed rather than masked, so a
#: reviewer can still tell "same user" without learning who they are.
_HASH_KEYS = ("user_id", "session_id", "conversation_id", "thread_id", "openid")

#: Keys that are provably *not* user text (enums, versions, ids of internal
#: artefacts). Everything else is scanned. This is a deny-list on purpose: an
#: allow-list of "text" keys silently lets a new field name leak user data, and
#: the cost of scanning a harmless field is zero.
_NON_TEXT_KEYS = (
    "model",
    "mode",
    "checkpointer",
    "split",
    "version",
    "status",
    "kind",
    "type",
    "role",
    "layer",
    "check_id",
    "dataset",
    "dataset_split",
    "dataset_version",
    "prompt_version",
    "code_sha",
    "schema_version",
)


@dataclass
class RedactionReport:
    version: str = REDACTION_VERSION
    counts: dict[str, int] = field(default_factory=dict)
    hashed_ids: int = 0

    def record(self, kind: str, n: int = 1) -> None:
        if n:
            self.counts[kind] = self.counts.get(kind, 0) + n

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "counts": self.counts,
            "hashed_ids": self.hashed_ids,
            "total": self.total,
            "retention_days": 30,
        }


def _hash_id(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"id:{digest[:12]}"


def redact_text(text: str, report: RedactionReport | None = None) -> str:
    """Apply every identifier pattern to one string."""

    result = text or ""
    for kind, pattern, replacement in _PATTERNS:
        result, n = pattern.subn(replacement, result)
        if report is not None:
            report.record(kind, n)
    return result


def redact_record(record: dict[str, Any], report: RedactionReport | None = None) -> dict[str, Any]:
    """Return a deep-copied, redacted trace record.

    Failures are scanned too — an exception message routinely embeds the very
    input we are trying to strip.
    """

    report = report or RedactionReport()
    return _walk(record, report)


def _walk(value: Any, report: RedactionReport, key: str | None = None) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for child_key, child_value in value.items():
            lowered = str(child_key).lower()
            if lowered in _HASH_KEYS:
                if child_value in (None, ""):
                    out[child_key] = child_value
                else:
                    out[child_key] = _hash_id(child_value)
                    report.hashed_ids += 1
                continue
            out[child_key] = _walk(child_value, report, lowered)
        return out
    if isinstance(value, list):
        return [_walk(item, report, key) for item in value]
    if isinstance(value, str):
        # Scan unless the key is provably non-text. Text keys are the common
        # case; scanning the rest is the safe default.
        if key is None or key not in _NON_TEXT_KEYS:
            return redact_text(value, report)
        return value
    return value


def redact(record: dict[str, Any]) -> tuple[dict[str, Any], RedactionReport]:
    """Convenience wrapper returning ``(redacted_record, report)``."""

    report = RedactionReport()
    return redact_record(record, report), report
