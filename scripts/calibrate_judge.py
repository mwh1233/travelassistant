"""Calibrate the judge against human labels and report Cohen's kappa.

Usage::

    # Deterministic proxy judge (no model, no network) — runs in CI
    python -m scripts.calibrate_judge --judge rule

    # Real LLM judge (needs a model factory / API key)
    python -m scripts.calibrate_judge --judge llm

    # Write the calibration report the runner consults
    python -m scripts.calibrate_judge --judge rule --save

Exit code is non-zero when the judge fails the kappa gate, so this can be a
pre-commit / CI check on the rubric itself (D09 §5.2).
"""

from __future__ import annotations

import argparse
import sys

from evals.judge import build_judge, calibrate, load_calibration, save_calibration
from evals.judge.calibration import load_gold
from evals.judge.rubrics import KAPPA_THRESHOLD


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="calibrate the eval judge")
    parser.add_argument("--judge", default="rule", choices=["rule", "llm"])
    parser.add_argument("--dimension", default="faithfulness")
    parser.add_argument("--threshold", type=float, default=0.5, help="binarisation cut for the judge score")
    parser.add_argument("--gold", default="", help="override the gold dataset path")
    parser.add_argument("--save", action="store_true", help="persist the calibration report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from pathlib import Path

    gold = load_gold(Path(args.gold)) if args.gold else load_gold()
    if not gold:
        print("未找到 gold 数据集，无法校准", file=sys.stderr)
        return 2

    model_factory = None
    if args.judge == "llm":
        from app.agents.handoffs.travel_agent import get_llm

        model_factory = get_llm

    judge = build_judge(args.judge, model_factory=model_factory)
    report = calibrate(judge, gold, dimension=args.dimension, threshold=args.threshold)

    print(f"Judge: {report.judge_version}（rubric {report.rubric_version}）")
    print(report.summary())
    print(f"混淆矩阵: {report.confusion}")
    if report.disagreements:
        print(f"\n分歧样本（前 {min(5, len(report.disagreements))} 例）:")
        for item in report.disagreements[:5]:
            print(f"  - {item['case_id']}: 人工={item['human']} judge={item['judge']} "
                  f"score={item['judge_score']}｜{item['evidence'][:60]}")

    if args.save:
        path = save_calibration(report)
        print(f"\n校准报告已保存：{path}")
        print(f"可用于自动门禁：{report.passes_gate}（阈值 kappa ≥ {KAPPA_THRESHOLD}）")
    else:
        existing = load_calibration(report.judge_version)
        if existing:
            print(f"\n已有校准报告：kappa={existing.get('kappa')} mode={existing.get('mode')}")

    if not report.passes_gate:
        print(
            f"\n❌ 未达 kappa ≥ {KAPPA_THRESHOLD}：该 judge 只能作粗筛，最终判定必须回流人工。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
