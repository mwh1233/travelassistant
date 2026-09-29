"""Human review queue: the gate between "online signal" and "permanent test".

D09 §3 闭环 is explicit that **人工复核是闭环里不可跳过的一环**: an online
disagreement may be a user's unsafe behaviour, a phrasing misunderstanding or a
one-off. Promoting it automatically writes that mistake into the dataset as the
expected answer.

So the queue is designed so the skip is *impossible*, not merely discouraged:

- an item cannot be approved without a reviewer identity;
- an item cannot be approved without stating ``correct_behaviour`` — the whole
  point of review is to record what *should* have happened;
- only ``approved`` items are promotable, and promotion is recorded on the item
  so it happens at most once.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REVIEW_QUEUE_VERSION = "1.0"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

DEFAULT_QUEUE_PATH = Path(__file__).resolve().parents[1] / "online" / "review_queue.jsonl"


class ReviewError(RuntimeError):
    """Raised when a queue operation would violate the review contract."""


@dataclass
class ReviewItem:
    item_id: str
    source_trace_id: str
    case_id: str
    reason: str
    """Why it was flagged: a turn-layer label, a judge disagreement, a gate fail…"""
    proposed_label: str = ""
    """What the automated layer *thinks* the failure is — a suggestion, not a fact."""
    redacted_sample: dict[str, Any] = field(default_factory=dict)
    #: Structured fields a reviewer supplies so the promoted case can actually
    #: be *executed* (``input``, ``initial_state``, ``expected_tools``…). Free
    #: text alone yields a contract-only case — see ``evals/online/promote.py``.
    case_patch: dict[str, Any] = field(default_factory=dict)
    #: Which dataset the promoted case should join. Empty = the default
    #: ``regression_feedback.jsonl``.
    target_dataset: str = ""
    status: str = STATUS_PENDING
    reviewer: str = ""
    correct_behaviour: str = ""
    notes: str = ""
    decided_at: str = ""
    promoted_to: str = ""
    promoted_at: str = ""
    queue_version: str = REVIEW_QUEUE_VERSION

    @property
    def promotable(self) -> bool:
        return self.status == STATUS_APPROVED and not self.promoted_to

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in payload.items() if key in known})


def build_review_item(
    *,
    source_trace_id: str,
    case_id: str,
    reason: str,
    redacted_sample: dict[str, Any],
    proposed_label: str = "",
    item_id: str | None = None,
) -> ReviewItem:
    return ReviewItem(
        item_id=item_id or f"rv_{uuid.uuid4().hex[:10]}",
        source_trace_id=source_trace_id,
        case_id=case_id,
        reason=reason,
        proposed_label=proposed_label,
        redacted_sample=redacted_sample,
    )


class ReviewQueue:
    """An append-only review queue persisted as JSONL.

    Rewrites on every save; the file is small and the alternative (a database)
    is not worth the operational cost at this stage. ``save`` is idempotent for a
    given ``items`` list, which keeps re-runs from duplicating entries as long as
    callers pass the same item ids.
    """

    def __init__(self, items: Iterable[ReviewItem] = (), path: Path | None = None):
        self.items: list[ReviewItem] = list(items)
        self.path = path or DEFAULT_QUEUE_PATH

    # ---------- queries ----------

    def get(self, item_id: str) -> ReviewItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)

    def by_status(self, status: str) -> list[ReviewItem]:
        return [item for item in self.items if item.status == status]

    def pending(self) -> list[ReviewItem]:
        return self.by_status(STATUS_PENDING)

    def approved(self) -> list[ReviewItem]:
        return self.by_status(STATUS_APPROVED)

    def promotable(self) -> list[ReviewItem]:
        return [item for item in self.items if item.promotable]

    def has_source(self, source_trace_id: str) -> bool:
        return any(item.source_trace_id == source_trace_id for item in self.items)

    # ---------- mutations ----------

    def enqueue(self, item: ReviewItem) -> ReviewItem:
        """Add an item, de-duplicating on the source trace id."""

        existing = next(
            (
                candidate
                for candidate in self.items
                if candidate.source_trace_id == item.source_trace_id
                and candidate.reason == item.reason
            ),
            None,
        )
        if existing is not None:
            return existing
        self.items.append(item)
        return item

    def approve(self, item_id: str, *, reviewer: str, correct_behaviour: str, notes: str = "") -> ReviewItem:
        """Approve a pending item. Both arguments are mandatory by design."""

        if not (reviewer or "").strip():
            raise ReviewError("复核人不能为空：未经人工复核的线上分歧不得入库")
        if not (correct_behaviour or "").strip():
            raise ReviewError("必须写明「正确行为应该是什么」，否则无法转成用例")

        item = self._require(item_id)
        if item.status == STATUS_REJECTED:
            raise ReviewError(f"{item_id} 已被拒绝，不能改为通过")
        item.status = STATUS_APPROVED
        item.reviewer = reviewer.strip()
        item.correct_behaviour = correct_behaviour.strip()
        item.notes = notes
        item.decided_at = _now()
        return item

    def reject(self, item_id: str, *, reviewer: str, notes: str = "") -> ReviewItem:
        if not (reviewer or "").strip():
            raise ReviewError("复核人不能为空")
        item = self._require(item_id)
        item.status = STATUS_REJECTED
        item.reviewer = reviewer.strip()
        item.notes = notes
        item.decided_at = _now()
        return item

    def mark_promoted(self, item_id: str, destination: str) -> ReviewItem:
        item = self._require(item_id)
        if not item.promotable:
            raise ReviewError(f"{item_id} 不可晋升：状态={item.status}，已晋升到={item.promoted_to!r}")
        item.promoted_to = destination
        item.promoted_at = _now()
        return item

    def _require(self, item_id: str) -> ReviewItem:
        item = self.get(item_id)
        if item is None:
            raise ReviewError(f"未找到复核项 {item_id}")
        return item

    # ---------- persistence ----------

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for item in self.items:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "ReviewQueue":
        target = path or DEFAULT_QUEUE_PATH
        if not target.exists():
            return cls(path=target)
        items: list[ReviewItem] = []
        with target.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(ReviewItem.from_dict(json.loads(line)))
                except Exception:
                    continue
        return cls(items, path=target)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
