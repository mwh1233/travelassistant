"""Promotion: turn an *approved* review item into a permanent regression case.

D09 §5 闭环 ends at "晋升为用例 → 跑回归 → CI 抓住". This module is that last
mile, and it is deliberately paranoid in two directions:

**Forward (contract).** A promoted case must still carry its provenance —
who reviewed it, what the correct behaviour is, which trace it came from. That
provenance is what lets a future reader tell a *human judgement* from an
*automated guess*. Losing it silently is the failure mode this file guards
against: :func:`contract_failures` is evaluated by the runner on every CI run,
so a hand-edited or truncated dataset is caught, not trusted.

**Backward (no silent overwrite).** A reviewer supplies a ``case_patch`` of
structured fields (the same keys a normal dataset row uses). Identity and
provenance keys are stripped from that patch: a patch may add
``expected_tools`` or ``initial_state``, it may never rename the case or
rewrite who approved it.

Promotion writes into a dedicated ``regression_feedback.jsonl``. Keeping the
feedback population separate is what makes "did the online loop actually
change the gate?" answerable — its rows are the only ones whose ``origin`` is a
human review rather than the original dataset author.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.online.queue import ReviewItem, ReviewQueue

PROMOTE_VERSION = "1.0"

#: The dataset that receives promoted cases. Separate from the authored
#: datasets on purpose — see the module docstring.
FEEDBACK_DATASET = "regression_feedback.jsonl"

#: A promoted case is always a regression case: it exists because something
#: broke in production, so it must never be skipped by the regression gate.
FEEDBACK_SPLIT = "regression"

#: Keys a reviewer may not set through ``case_patch``: they are either the
#: case's identity or its audit trail.
PROTECTED_KEYS = (
    "id",
    "version",
    "split",
    "type",
    "reviewer",
    "reviewed_at",
    "correct_behaviour",
    "source_trace_id",
    "source_case_id",
    "flag_reason",
    "proposed_label",
    "origin",
)

#: Provenance a promoted case must carry for CI to accept it. Missing any of
#: these means the human-review step was bypassed (or the row was mangled), so
#: a violation is a *failure*, never a skip.
REQUIRED_PROVENANCE = (
    "reviewer",
    "correct_behaviour",
    "source_trace_id",
    "version",
)


class PromotionError(RuntimeError):
    """Raised when an item cannot be promoted without breaking the contract."""


def build_feedback_case(item: ReviewItem) -> dict[str, Any]:
    """Build a dataset row from an approved, reviewed item.

    Raises :class:`PromotionError` if the item is not promotable — the check is
    duplicated here (not only in the queue) so a hand-built ``ReviewItem``
    cannot slip through.
    """

    if not item.promotable:
        raise PromotionError(
            f"{item.item_id} 不可晋升：status={item.status!r}, promoted_to={item.promoted_to!r}；"
            "只有 approved 且未晋升的复核项才能转成用例"
        )

    case: dict[str, Any] = {
        "id": f"fb_{item.item_id}",
        "type": "feedback",
        "split": FEEDBACK_SPLIT,
        "version": PROMOTE_VERSION,
        "source_trace_id": item.source_trace_id,
        "source_case_id": item.case_id,
        "reviewer": item.reviewer,
        "reviewed_at": item.decided_at,
        "proposed_label": item.proposed_label,
        "flag_reason": item.reason,
        "correct_behaviour": item.correct_behaviour,
        "notes": item.notes,
        "origin": {
            "kind": "online_review",
            "queue_version": item.queue_version,
            "item_id": item.item_id,
        },
    }

    patch = {
        key: value
        for key, value in (item.case_patch or {}).items()
        if key not in PROTECTED_KEYS
    }
    case.update(patch)
    # ``type`` may be upgraded by the patch to a runnable kind (e.g.
    # ``agent_planning``) *only* under its own key, which is protected above,
    # so we re-allow it explicitly with a whitelist rather than generically.
    runnable_kind = (item.case_patch or {}).get("runnable_kind")
    if runnable_kind:
        case["type"] = "agent_planning"
        case["runnable_kind"] = runnable_kind
    return case


def contract_failures(row: dict[str, Any]) -> list[str]:
    """Return the provenance violations in a promoted row (``[]`` = clean)."""

    failures: list[str] = []
    for key in REQUIRED_PROVENANCE:
        if not str(row.get(key) or "").strip():
            failures.append(f"L5.feedback_provenance.{key}")
    origin = row.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "online_review":
        failures.append("L5.feedback_provenance.origin")
    return failures


def load_feedback_cases(dataset_path: Path) -> list[dict[str, Any]]:
    if not dataset_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def promote_queue(
    queue: ReviewQueue,
    dataset_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Promote every promotable item, appending rows and stamping the queue.

    Rows already present (same ``id``) are treated as already promoted and are
    not duplicated — so re-running the script after a partial failure is safe.
    """

    existing = load_feedback_cases(dataset_path)
    existing_ids = {str(row.get("id")) for row in existing}

    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    pairs: list[tuple[str, dict[str, Any]]] = []  # (item_id, case)

    for item in list(queue.promotable()):
        case = build_feedback_case(item)
        if case["id"] in existing_ids:
            skipped.append({"item_id": item.item_id, "reason": "already_in_dataset"})
            if not dry_run:
                queue.mark_promoted(item.item_id, str(dataset_path))
            continue
        promoted.append(case)
        pairs.append((item.item_id, case))

    if promoted and not dry_run:
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        with dataset_path.open("a", encoding="utf-8") as handle:
            for case in promoted:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
        for item_id, _case in pairs:
            queue.mark_promoted(item_id, str(dataset_path))

    if not dry_run:
        queue.save()

    return {
        "promote_version": PROMOTE_VERSION,
        "dataset": str(dataset_path),
        "promoted": promoted,
        "promoted_count": len(promoted),
        "skipped": skipped,
        "pending": len(queue.pending()),
        "remaining_promotable": len(queue.promotable()),
        "dry_run": dry_run,
    }
