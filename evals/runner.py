"""Evaluation entry point.

This is the executable that was missing from ``evals/``: it loads a suite, runs
it, applies the graders and hard gates, and writes a JSON + Markdown report.

Usage::

    # L0 deterministic assertions (no model, no network)
    python -m evals.runner --suite l0

    # Trajectory suite with the scripted model (no model, no network)
    python -m evals.runner --suite trajectory

    # Both, with pass^3 reported as well
    python -m evals.runner --suite all --repeat 3

    # Structural validation of the 240-row dataset
    python -m evals.runner --suite dataset

Exit code is non-zero when a *regression* fails — known gaps and negative
controls are reported separately and do not flip the exit code.

See ``docs/agent-eval-design.md`` for the layer definitions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evals import checks as checks_module
from evals.checks import CheckResult
from evals.harness import load_step_config, run_scenario
from evals.trajectory_checks import evaluate_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = PROJECT_ROOT / "evals" / "datasets"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "evals" / "reports"
TRACE_PATH = PROJECT_ROOT / "evals" / "traces" / "trajectory.jsonl"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def quiet_logger() -> None:
    """Keep the report readable by dropping INFO logs from the console."""

    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# L0 suite
# ---------------------------------------------------------------------------


async def run_l0_suite() -> dict[str, Any]:
    results = await checks_module.run_l0_checks()
    return {
        "suite": "l0",
        "checks": [result.to_dict() for result in results],
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if not result.passed and not result.known_gap),
        "known_gaps": sum(1 for result in results if not result.passed and result.known_gap),
    }


# ---------------------------------------------------------------------------
# trajectory suite
# ---------------------------------------------------------------------------


def load_trajectory_scenarios(only: list[str] | None = None) -> list[dict[str, Any]]:
    path = DATASET_DIR / "trajectory_scenarios.jsonl"
    scenarios = list(iter_jsonl(path))
    if only:
        wanted = set(only)
        scenarios = [item for item in scenarios if item.get("id") in wanted]
    return scenarios


async def candidate_tool_map() -> dict[str, list[str]]:
    config = await load_step_config()
    mapping: dict[str, list[str]] = {}
    for step_name, step in config.items():
        mapping[step_name] = [getattr(tool, "name", "") for tool in step.get("tools", [])]

    rollback: set[str] = set()
    for step in config.values():
        for tool in step.get("tools", []):
            name = getattr(tool, "name", "")
            if name.startswith("go_back_"):
                rollback.add(name)
    mapping["__rollback__"] = sorted(rollback)
    return mapping


async def run_trajectory_suite(
    scenarios: list[dict[str, Any]],
    *,
    mode: str,
    repeat: int,
    trace_path: Path | None = None,
    dataset_version: str | None = None,
) -> dict[str, Any]:
    from app.agents.handoffs.travel_agent import get_llm
    from app.observability.trace import write_jsonl

    candidates = await candidate_tool_map()
    model_factory = None if mode == "deterministic" else get_llm

    cases: list[dict[str, Any]] = []

    for scenario in scenarios:
        attempts: list[dict[str, Any]] = []
        for attempt in range(max(1, repeat)):
            outcome = await run_scenario(
                scenario,
                mode=mode,
                model_factory=model_factory,
                dataset_version=dataset_version,
            )
            verdict = evaluate_scenario(scenario, outcome, candidates)
            verdict["attempt"] = attempt + 1
            verdict["mode"] = mode
            verdict["description"] = scenario.get("description", "")
            verdict["split"] = scenario.get("split")
            verdict["tools_called"] = outcome.tool_names
            verdict["final_step"] = outcome.final_state.get("current_step")
            verdict["llm_calls"] = outcome.record.get("cost", {}).get("llm_calls", 0)
            verdict["error"] = outcome.error
            attempts.append(verdict)

            if trace_path is not None:
                write_jsonl(outcome.record, trace_path)

        passed_all = all(item["passed"] for item in attempts)
        cases.append(
            {
                "case_id": scenario["id"],
                "split": scenario.get("split"),
                "description": scenario.get("description", ""),
                "known_gap": bool(scenario.get("known_gap")),
                "known_gap_ref": scenario.get("known_gap_ref", ""),
                "negative_control": bool(scenario.get("negative_control")),
                "pass_at_1": attempts[0]["passed"],
                "pass_pow_k": passed_all,
                "attempts": attempts,
            }
        )

    regressions = [
        case
        for case in cases
        if not case["pass_at_1"] and not case["known_gap"] and not case["negative_control"]
    ]
    return {
        "suite": "trajectory",
        "mode": mode,
        "repeat": repeat,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": sum(1 for case in cases if case["negative_control"]),
        "regressions": len(regressions),
    }


# ---------------------------------------------------------------------------
# dataset suite
# ---------------------------------------------------------------------------


async def run_dataset_suite() -> dict[str, Any]:
    """Structural validation of every dataset file.

    Deliberately reuses ``check_dataset_schema`` so the ``dataset`` suite and
    the ``L0.dataset_schema`` gate can never disagree about whether the data is
    valid.
    """

    from evals.checks import check_dataset_schema

    result = check_dataset_schema()
    files = [{"file": name, **info} for name, info in (result.data.get("files") or {}).items()]
    return {
        "suite": "dataset",
        "passed": result.passed,
        "detail": result.detail,
        "failures": result.data.get("failures", []),
        "files": files,
        "total_rows": sum(info["rows"] for info in files),
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def build_report(results: list[dict[str, Any]], *, mode: str, repeat: int, sha: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
        "mode": mode,
        "repeat": repeat,
        "results": results,
    }


def _failed_checks(case: dict[str, Any]) -> str:
    attempt = case["attempts"][0]
    if attempt["passed"]:
        return ""
    details = [
        f"{check['check_id']}: {check['detail']}"
        for check in attempt["checks"]
        if not check["passed"]
    ]
    return "<br>".join(details[:3]) or attempt["summary"]


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Agent 评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 代码版本：`{report['git_sha']}`",
        f"- 执行模式：`{report['mode']}`（repeat={report['repeat']}）",
        "",
    ]

    for result in report["results"]:
        suite = result["suite"]

        if suite == "l0":
            lines += [
                "## L0 · 确定性断言",
                "",
                f"通过 {result['passed']}/{result['total']}，硬失败 {result['failed']}，已知缺口 {result['known_gaps']}",
                "",
                "| 断言 | 结果 | 说明 |",
                "|---|---|---|",
            ]
            for check in result["checks"]:
                flag = "✅" if check["passed"] else ("⚠️ 已知缺口" if check["known_gap"] else "❌")
                lines.append(
                    f"| `{check['check_id']}` | {flag} | {check['title']}｜{check['detail'][:160]} |"
                )
            lines.append("")

        elif suite == "trajectory":
            lines += [
                "## L2 · 轨迹断言",
                "",
                (
                    f"pass@1 {result['pass_at_1']}/{result['total']}，"
                    f"pass^{result['repeat']} {result['pass_pow_k']}/{result['total']}，"
                    f"回归失败 {result['regressions']}，已知缺口 {result['known_gaps']}，"
                    f"负向对照 {result['negative_controls']}"
                ),
                "",
                "| 用例 | split | pass@1 | 实际工具 | 终点 | 失败项 |",
                "|---|---|---|---|---|---|",
            ]
            for case in result["cases"]:
                if case["pass_at_1"]:
                    flag = "✅"
                elif case["known_gap"]:
                    flag = "⚠️ 已知缺口"
                elif case["negative_control"]:
                    flag = "❌ 对照失效"
                else:
                    flag = "❌"
                lines.append(
                    f"| `{case['case_id']}` | {case['split']} | {flag} | "
                    f"{', '.join(case['attempts'][0]['tools_called']) or '—'} | "
                    f"{case['attempts'][0]['final_step']} | {_failed_checks(case)} |"
                )
            lines.append("")

            gaps = [case for case in result["cases"] if case["known_gap"] and not case["pass_at_1"]]
            if gaps:
                lines += ["### 已知缺口（不计入回归）", ""]
                for case in gaps:
                    lines.append(
                        f"- `{case['case_id']}`（{case['known_gap_ref'] or '未标注来源'}）："
                        f"{case['description']}｜失败项 "
                        f"{', '.join(case['attempts'][0]['failed_checks'])}"
                    )
                lines.append("")

        elif suite == "dataset":
            headline = (
                "✅ 全部合规"
                if result["passed"]
                else f"❌ {len(result['failures'])} 处问题"
            )
            lines += [
                "## 数据集结构校验",
                "",
                f"{result['total_rows']} 行｜{headline}",
                "",
                "| 文件 | 行数 | split 分布 | 版本 |",
                "|---|---|---|---|",
            ]
            for info in result["files"]:
                splits = "、".join(
                    f"{name}×{count}" for name, count in sorted(info.get("splits", {}).items())
                )
                lines.append(
                    f"| `{info['file']}` | {info['rows']} | {splits} | "
                    f"{', '.join(info.get('versions', []))} |"
                )
            lines.append("")
            if result["failures"]:
                lines += ["### 问题明细", ""]
                lines += [f"- {problem}" for problem in result["failures"][:20]]
                lines.append("")

    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], report_dir: Path, suite: str) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"{stamp}-{suite}.json"
    md_path = report_dir / f"{stamp}-{suite}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="travelassistant evaluation runner")
    parser.add_argument(
        "--suite",
        default="l0",
        choices=["l0", "trajectory", "dataset", "all"],
        help="which layer to evaluate",
    )
    parser.add_argument(
        "--mode",
        default="deterministic",
        choices=["deterministic", "live"],
        help="deterministic = scripted model + stubbed externals; live = real model and tools",
    )
    parser.add_argument("--repeat", type=int, default=1, help="repeats per case, for pass^k")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--only", default="", help="comma-separated scenario ids to run")
    parser.add_argument("--no-trace", action="store_true", help="skip writing trajectories to JSONL")
    parser.add_argument("--verbose", action="store_true", help="keep INFO logs on the console")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not args.verbose:
        quiet_logger()

    sha = git_sha()
    results: list[dict[str, Any]] = []

    if args.suite in ("l0", "all"):
        results.append(await run_l0_suite())

    if args.suite in ("trajectory", "all"):
        only = [item for item in (args.only or "").split(",") if item]
        scenarios = load_trajectory_scenarios(only or None)
        results.append(
            await run_trajectory_suite(
                scenarios,
                mode=args.mode,
                repeat=args.repeat,
                trace_path=None if args.no_trace else TRACE_PATH,
            )
        )

    if args.suite in ("dataset", "all"):
        results.append(await run_dataset_suite())

    report = build_report(results, mode=args.mode, repeat=args.repeat, sha=sha)
    report_dir = Path(args.report_dir)
    json_path, md_path = write_reports(report, report_dir, args.suite)

    # --- console summary ------------------------------------------------
    print(f"\n=== 评测汇总（{args.suite} / {args.mode}）===")
    hard_failures = 0
    for result in results:
        if result["suite"] == "l0":
            print(
                f"L0  通过 {result['passed']}/{result['total']}"
                f"｜硬失败 {result['failed']}｜已知缺口 {result['known_gaps']}"
            )
            hard_failures += result["failed"]
            for check in result["checks"]:
                if not check["passed"]:
                    flag = "已知缺口" if check["known_gap"] else "硬失败"
                    print(f"    [{flag}] {check['check_id']}: {check['detail'][:140]}")
        elif result["suite"] == "trajectory":
            print(
                f"L2  pass@1 {result['pass_at_1']}/{result['total']}"
                f"｜pass^{result['repeat']} {result['pass_pow_k']}/{result['total']}"
                f"｜回归失败 {result['regressions']}"
                f"｜已知缺口 {result['known_gaps']}"
                f"｜负向对照 {result['negative_controls']}"
            )
            hard_failures += result["regressions"]
            for case in result["cases"]:
                if case["pass_at_1"]:
                    continue
                flag = "已知缺口" if case["known_gap"] else ("对照失效" if case["negative_control"] else "回归失败")
                print(
                    f"    [{flag}] {case['case_id']}: "
                    f"{case['attempts'][0]['summary'][:140]}"
                )
        elif result["suite"] == "dataset":
            flag = "全部合规" if result["passed"] else f"失败 {len(result['failures'])} 处"
            print(f"DS  {result['total_rows']} 行｜{flag}")
            if not result["passed"]:
                hard_failures += 1
                for problem in result["failures"][:5]:
                    print(f"    [失败] {problem}")

    print(f"\n报告：{md_path}")
    print(f"JSON：{json_path}")
    return 1 if hard_failures else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
