"""Promote human-reviewed online failures into permanent regression cases.

D09 §5 闭环's last step. Two audiences share this script:

**The reviewer.** ``--list`` shows what is waiting; ``--approve`` records a
decision. Approval *requires* a reviewer name and a statement of the correct
behaviour — the queue refuses anything less, because a promoted case that was
never actually reviewed is worse than no case at all.

**CI.** ``--promote`` writes approved items into
``evals/datasets/regression_feedback.jsonl`` and ``--verify`` immediately runs
the feedback suite, so the loop demonstrably closes: a promoted case that the
current code fails shows up as a regression, not as a silent addition.

Usage::

    # 1. 看看队列里有什么
    python scripts/promote_reviewed.py --list

    # 2. 人工复核并给出「正确行为」
    python scripts/promote_reviewed.py --approve rv_ab12cd34ef \\
        --reviewer alice --correct-behaviour "改目的地后必须清空 selected_destination 的下游字段"

    # 3. 晋升 + 立即跑回归证明闭环
    python scripts/promote_reviewed.py --promote --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.online.promote import (  # noqa: E402
    FEEDBACK_DATASET,
    PromotionError,
    contract_failures,
    load_feedback_cases,
    promote_queue,
)
from evals.online.queue import ReviewError, ReviewQueue  # noqa: E402

DEFAULT_QUEUE = PROJECT_ROOT / "evals" / "online" / "review_queue.jsonl"
DATASET_DIR = PROJECT_ROOT / "evals" / "datasets"


def cmd_list(queue: ReviewQueue) -> int:
    pending = queue.pending()
    approved = queue.promotable()
    print(f"=== 复核队列（{queue.path}）===")
    print(f"待复核 {len(pending)}｜已通过待晋升 {len(approved)}｜总计 {len(queue.items)}")
    for item in pending:
        print(
            f"  [待复核] {item.item_id}｜{item.reason}｜proposed={item.proposed_label or '—'}"
            f"｜trace={item.source_trace_id}"
        )
    for item in approved:
        print(f"  [待晋升] {item.item_id}｜reviewer={item.reviewer}｜{item.correct_behaviour[:60]}")
    return 0


def cmd_approve(queue: ReviewQueue, args: argparse.Namespace) -> int:
    patch = {}
    if args.patch_json:
        patch = json.loads(args.patch_json)
    try:
        item = queue.approve(
            args.approve,
            reviewer=args.reviewer,
            correct_behaviour=args.correct_behaviour,
            notes=args.notes,
        )
    except ReviewError as exc:
        print(f"❌ {exc}")
        return 2
    if patch:
        item.case_patch = patch
    if args.target_dataset:
        item.target_dataset = args.target_dataset
    queue.save()
    print(f"✅ 已通过 {item.item_id}（复核人 {item.reviewer}）")
    if item.case_patch:
        print(f"   附带结构化字段：{sorted(item.case_patch)}")
    else:
        print("   （未提供 case_patch：晋升后只能作为「契约用例」，不参与通过率）")
    return 0


def cmd_reject(queue: ReviewQueue, args: argparse.Namespace) -> int:
    try:
        item = queue.reject(args.reject, reviewer=args.reviewer, notes=args.notes)
    except ReviewError as exc:
        print(f"❌ {exc}")
        return 2
    queue.save()
    print(f"🚫 已拒绝 {item.item_id}（复核人 {item.reviewer}）")
    return 0


def cmd_promote(queue: ReviewQueue, args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset) if args.dataset else DATASET_DIR / FEEDBACK_DATASET
    try:
        result = promote_queue(queue, dataset_path, dry_run=args.dry_run)
    except PromotionError as exc:
        print(f"❌ {exc}")
        return 2

    verb = "将晋升" if result["dry_run"] else "已晋升"
    print(f"=== 晋升复核用例 → {result['dataset']} ===")
    print(f"{verb} {result['promoted_count']} 例｜已存在跳过 {len(result['skipped'])}")
    for case in result["promoted"]:
        print(f"  + {case['id']}｜reviewer={case['reviewer']}｜{case['correct_behaviour'][:60]}")
    print(f"队列剩余待复核 {result['pending']}｜待晋升 {result['remaining_promotable']}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Re-derive the promotion contract from the stored dataset file."""

    dataset_path = Path(args.dataset) if args.dataset else DATASET_DIR / FEEDBACK_DATASET
    rows = load_feedback_cases(dataset_path)
    if not rows:
        print(f"数据集 {dataset_path} 尚无晋升用例")
        return 0
    bad = {str(row.get("id")): contract_failures(row) for row in rows}
    bad = {key: value for key, value in bad.items() if value}
    print(f"=== 晋升用例审计（{len(rows)} 行）===")
    if not bad:
        print("✅ 全部保留 provenance（reviewer / correct_behaviour / source_trace_id）")
        return 0
    for case_id, failures in bad.items():
        print(f"  ❌ {case_id}: {', '.join(failures)}")
    return 1


async def cmd_verify(args: argparse.Namespace) -> int:
    from evals.runner import quiet_logger, run_feedback_suite

    quiet_logger()
    suite = await run_feedback_suite(
        mode=args.mode,
        repeat=1,
        cassette_mode="off",
        dataset_path=Path(args.dataset) if args.dataset else None,
    )
    coverage = suite["coverage"]
    print("=== 反馈回归（晋升用例立即重跑）===")
    print(
        f"晋升 {coverage['promoted_rows']} 行｜行为回归 {coverage['behavioural_rows']}"
        f"｜仅契约 {coverage['contract_only_rows']}｜契约违规 {suite['contract_violations']}"
    )
    print(f"pass@1 {suite['pass_at_1']}/{suite['scored']}｜失败 {suite['regressions']}")
    for case in suite["cases"]:
        if not case["pass_at_1"] and not case["known_gap"]:
            print(f"  ❌ {case['case_id']}: {case['summary'][:140]}")
    return 1 if suite["regressions"] or suite["contract_violations"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="线上复核 → 版本化回归用例")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE))
    parser.add_argument("--dataset", default="", help=f"默认 evals/datasets/{FEEDBACK_DATASET}")
    parser.add_argument("--list", action="store_true", help="列出队列内容")
    parser.add_argument("--audit", action="store_true", help="审计已晋升用例的 provenance")
    parser.add_argument("--promote", action="store_true", help="晋升所有已通过且未晋升的用例")
    parser.add_argument("--verify", action="store_true", help="晋升后立即跑反馈回归")
    parser.add_argument("--dry-run", action="store_true", help="只示意将晋升什么，不写盘")
    parser.add_argument("--mode", default="deterministic", choices=["deterministic", "replay", "live"])
    parser.add_argument("--approve", default="", help="要通过的复核项 id")
    parser.add_argument("--reject", default="", help="要拒绝的复核项 id")
    parser.add_argument("--reviewer", default="", help="复核人（通过/拒绝时必填）")
    parser.add_argument("--correct-behaviour", default="", help="正确行为描述（通过时必填）")
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--patch-json",
        default="",
        help="结构化字段 JSON（如 initial_state / expected_tools），用于把用例变成可执行回归",
    )
    parser.add_argument("--target-dataset", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    queue = ReviewQueue.load(Path(args.queue))

    if args.list:
        cmd_list(queue)
        return 0
    if args.approve:
        return cmd_approve(queue, args)
    if args.reject:
        return cmd_reject(queue, args)
    if args.audit:
        return cmd_audit(args)
    if args.promote:
        code = cmd_promote(queue, args)
        if code or args.dry_run or not args.verify:
            return code
        return asyncio.run(cmd_verify(args))
    if args.verify:
        return asyncio.run(cmd_verify(args))

    cmd_list(queue)
    print("\n用 --approve / --reject 处理队列，--promote --verify 晋升并验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
