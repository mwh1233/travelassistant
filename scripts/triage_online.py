"""Online triage: 线上 trace → 脱敏 → 抽样 → 逐轮分类 → 复核队列.

This is the *entry* of the D09 §5 闭环. It is deliberately a batch script rather
than a service: the queue it produces is reviewed by a human, so the throughput
of the front half is bounded by human review anyway, and a script is far easier
to audit than a background worker.

Usage::

    python scripts/triage_online.py --records evals/online/incoming.jsonl \\
        --queue evals/online/review_queue.jsonl \\
        --kept-out evals/online/kept.jsonl \\
        --rate 0.2

Input rows are any JSON objects carrying at least an id. Which fields are read:

- ``case_id`` / ``run_id`` / ``trace_id`` — identity, used as the sampling key
- ``turns`` / ``messages`` / ``input``   — user text, classified for turn events
- ``errors``                             — non-empty ⇒ flagged as a trace error
- ``reasons``                            — upstream signals merged in verbatim

Nothing is invented: a record with no turn text simply gets no turn label, and
``--rate 0`` + no danger reasons yields an empty queue, which is the honest
answer rather than a fabricated one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observability.redaction import REDACTION_VERSION, RedactionReport, redact_record  # noqa: E402
from evals.online.drift import distribution_drift  # noqa: E402
from evals.online.queue import ReviewQueue, build_review_item  # noqa: E402
from evals.online.sampling import SamplingPolicy  # noqa: E402
from evals.turns import TURN_CLASSIFIER_VERSION, classify_conversation, turn_layer_summary  # noqa: E402

TRIAGE_VERSION = "1.0"


# ---------------------------------------------------------------------------
# input handling
# ---------------------------------------------------------------------------


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def record_key(record: dict[str, Any], index: int) -> str:
    for field in ("case_id", "run_id", "trace_id", "conversation_id"):
        value = record.get(field)
        if value:
            return str(value)
    return f"row-{index}"


def extract_user_turns(record: dict[str, Any]) -> list[str]:
    """Best-effort extraction of the user's turns from a trace record.

    The eval traces do not carry raw user text (they carry tool I/O), so this
    accepts several online shapes. Returning ``[]`` is fine — it means "no turn
    layer signal available", which is reported, not hidden.
    """

    turns = record.get("turns")
    if isinstance(turns, list):
        out: list[str] = []
        for turn in turns:
            if isinstance(turn, str):
                out.append(turn)
            elif isinstance(turn, dict):
                for key in ("input", "text", "content", "user"):
                    if turn.get(key):
                        out.append(str(turn[key]))
                        break
        if out:
            return out

    messages = record.get("messages")
    if isinstance(messages, list):
        out = [
            str(message.get("content") or "")
            for message in messages
            if isinstance(message, dict) and str(message.get("role")) in ("user", "human")
        ]
        out = [text for text in out if text]
        if out:
            return out

    if record.get("input"):
        return [str(record["input"])]
    return []


def collect_reasons(record: dict[str, Any], signals: list[Any]) -> list[str]:
    """Merge upstream signals, trace errors and turn-layer labels."""

    reasons: list[str] = []
    for label in (record.get("reasons") or []):
        reasons.append(str(label))
    if record.get("errors"):
        reasons.append("trace_error")
    if str(record.get("outcome") or "").lower() in ("fail", "failed", "error"):
        reasons.append("task_failed")
    for signal in signals:
        if signal.label != "normal":
            reasons.append(f"turn:{signal.label}")
    # de-dup, preserve order
    seen: dict[str, None] = {}
    for reason in reasons:
        seen.setdefault(reason, None)
    return list(seen)


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


def triage(
    *,
    records: list[dict[str, Any]],
    policy: SamplingPolicy,
) -> dict[str, Any]:
    redaction_report = RedactionReport()
    kept: list[dict[str, Any]] = []
    all_labels: list[str] = []
    all_signals: list[Any] = []
    flagged_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        key = record_key(record, index)
        turns = extract_user_turns(record)
        signals = classify_conversation(turns)
        all_signals.extend(signals)
        all_labels.extend(signal.label for signal in signals if signal.label != "normal")

        reasons = collect_reasons(record, signals)
        for reason in reasons:
            flagged_counts[reason] = flagged_counts.get(reason, 0) + 1

        # Redact first, decide second: the sampling key is the *case id*, which
        # redaction preserves, so ordering does not change who is sampled.
        redacted = redact_record(record, redaction_report)

        if not policy.should_keep(key, reasons):
            continue

        kept.append({"key": key, "reasons": reasons, "record": redacted})
        if reasons:
            candidates.append(
                {
                    "key": key,
                    "reasons": reasons,
                    "redacted": redacted,
                    "signals": signals,
                    "case_id": str(record.get("case_id") or key),
                    "trace_id": str(record.get("trace_id") or record.get("run_id") or key),
                }
            )

    turn_summary = turn_layer_summary(all_signals)
    return {
        "triage_version": TRIAGE_VERSION,
        "redaction_version": REDACTION_VERSION,
        "turn_classifier_version": TURN_CLASSIFIER_VERSION,
        "seen": len(records),
        "kept": len(kept),
        "kept_records": kept,
        "candidates": candidates,
        "redaction": redaction_report.to_dict(),
        "turn_layer": turn_summary,
        "flagged_reasons": dict(sorted(flagged_counts.items(), key=lambda item: -item[1])),
        "label_distribution": _distribution(all_labels),
    }


def _distribution(labels: list[str]) -> dict[str, float]:
    if not labels:
        return {}
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    total = len(labels)
    return {label: round(count / total, 6) for label, count in sorted(counts.items())}


def enqueue_candidates(queue: ReviewQueue, result: dict[str, Any]) -> int:
    enqueued = 0
    for candidate in result["candidates"]:
        signals = candidate["signals"]
        proposed = next((signal.label for signal in signals if signal.label != "normal"), "")
        primary_reason = candidate["reasons"][0] if candidate["reasons"] else "sampled"
        item = build_review_item(
            source_trace_id=candidate["trace_id"],
            case_id=candidate["case_id"],
            reason=primary_reason,
            proposed_label=proposed,
            redacted_sample={
                "record": candidate["redacted"],
                "reasons": candidate["reasons"],
                "turns": [signal.to_dict() for signal in signals if signal.label != "normal"],
            },
        )
        before = len(queue.items)
        queue.enqueue(item)
        if len(queue.items) > before:
            enqueued += 1
    return enqueued


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="线上 trace 分诊：脱敏 → 抽样 → 分类 → 复核队列")
    parser.add_argument("--records", required=True, help="输入 JSONL（线上 trace / 会话记录）")
    parser.add_argument(
        "--queue",
        default=str(PROJECT_ROOT / "evals" / "online" / "review_queue.jsonl"),
        help="人工复核队列（JSONL）",
    )
    parser.add_argument("--kept-out", default="", help="保留下来的脱敏样本（JSONL）")
    parser.add_argument("--summary-out", default="", help="分诊报告（JSON）")
    parser.add_argument("--rate", type=float, default=0.1, help="抽样率（0–1）")
    parser.add_argument("--salt", default=None, help="抽样盐；同盐 + 同 key 结果可复现")
    parser.add_argument(
        "--baseline",
        default="",
        help="上一次分诊报告的路径；给出后额外比对标签分布漂移",
    )
    parser.add_argument("--no-enqueue", action="store_true", help="只分诊，不写复核队列")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    records = list(iter_jsonl(Path(args.records)))
    policy = (
        SamplingPolicy(rate=args.rate, salt=args.salt)
        if args.salt
        else SamplingPolicy(rate=args.rate)
    )

    result = triage(records=records, policy=policy)

    queue_path = Path(args.queue)
    queue = ReviewQueue.load(queue_path)
    enqueued = 0 if args.no_enqueue else enqueue_candidates(queue, result)
    if not args.no_enqueue:
        queue.save()

    if args.kept_out:
        out = Path(args.kept_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            for entry in result["kept_records"]:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    summary = {
        "triage_version": result["triage_version"],
        "redaction_version": result["redaction_version"],
        "turn_classifier_version": result["turn_classifier_version"],
        "seen": result["seen"],
        "kept": result["kept"],
        "sampling": {"rate": policy.rate, "salt": policy.salt},
        "redaction": result["redaction"],
        "turn_layer": result["turn_layer"],
        "flagged_reasons": result["flagged_reasons"],
        "label_distribution": result["label_distribution"],
        "queue": {
            "path": str(queue_path),
            "enqueued": enqueued,
            "total": len(queue.items),
            "pending": len(queue.pending()),
        },
    }

    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                drift = distribution_drift(
                    baseline.get("label_distribution") or {},
                    result["label_distribution"],
                )
                summary["drift"] = drift.to_dict()
            except Exception as exc:  # a bad baseline must not abort triage
                summary["drift_error"] = f"{type(exc).__name__}: {exc}"
        else:
            summary["drift_error"] = f"baseline 不存在: {baseline_path}"

    if args.summary_out:
        out = Path(args.summary_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 线上分诊（{summary['triage_version']}）===")
    print(
        f"读入 {summary['seen']} 条｜抽样 {summary['kept']}（rate={policy.rate}）"
        f"｜脱敏命中 {summary['redaction']['total']} 处"
    )
    print(
        f"逐轮分类：{summary['turn_layer']['turns']} 轮，"
        f"标记率 {summary['turn_layer']['flag_rate']:.1%}"
    )
    print(f"复核队列：新增 {enqueued}｜待复核 {summary['queue']['pending']}")
    if summary["flagged_reasons"]:
        top = list(summary["flagged_reasons"].items())[:6]
        print("主要标记原因：" + "、".join(f"{name}×{count}" for name, count in top))
    drift = summary.get("drift")
    if drift:
        flag = "⚠️ 漂移" if drift["drifted"] else "稳定"
        print(
            f"标签分布漂移（{drift['metric']}）={drift['value']}"
            f"（阈值 {drift['threshold']}）→ {flag}"
        )
    elif summary.get("drift_error"):
        print(f"漂移比对跳过：{summary['drift_error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
