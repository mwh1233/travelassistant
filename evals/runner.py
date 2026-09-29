"""Evaluation entry point.

This is the executable that turns the ``evals/`` package into an actual gate. It
loads every suite, *executes* it, applies the failure taxonomy and the release
thresholds, and writes a JSON + Markdown report.

D09 layer map
-------------
The runner is organised by the D09 granularity axis so the report can be read
against the 9-grid:

===============  ==========================  =================================
suite            D09 granularity             what it actually does
===============  ==========================  =================================
``l0``           invariant (cross-layer)      deterministic contract checks
``trajectory``   trajectory layer             single-turn scripted replay + assertions
``multiturn``    turn layer                   multi-turn scripted replay + assertions
``content``      result layer                 executes the 240 content rows
``dataset``      —                            structural validation only
===============  ==========================  =================================

Usage::

    # L0 deterministic assertions (no model, no network)
    python -m evals.runner --suite l0

    # Every behavioural suite
    python -m evals.runner --suite all --repeat 3

    # Replay from recorded tool responses (freezes the tool layer)
    python -m evals.runner --suite trajectory --mode replay

    # Only the safety split, as a stand-alone gate
    python -m evals.runner --suite all --split safety

    # Record a fresh cassette while running deterministically
    python -m evals.runner --suite trajectory --record

    # Compare this run against the recorded baseline
    python -m evals.runner --suite trajectory --diff

Exit code is non-zero when a *gate* fails — a regression, a safety-split
failure, or a threshold breach. Known gaps are reported separately and do **not**
flip the exit code (D09 §7.4: they are a migration list, not an exemption).

See ``docs/design/eval-optimization-roadmap.md`` and
``docs/universal/D09-质量-评测.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from evals import checks as checks_module
from evals import gates as gates_module
from evals import taxonomy as taxonomy_module
from evals.cassette import CASSETTE_SCHEMA_VERSION, CassetteMiss
from evals.checks import CheckResult
from evals.content import (
    CONTENT_KNOWN_GAPS_VERSION,
    TRAVEL_DATASET,
    LocalBm25Index,
    candidate_tool_index,
    corpus_coverage,
    load_local_corpus,
    run_mcp_probe,
    run_rag_probe,
    run_travel_probe,
)
from evals.harness import load_step_config, run_scenario
from evals.faults import FAULTS_VERSION
from evals.multiturn import MULTITURN_DATASET, run_multiturn_scenario
from evals.taxonomy import TAXONOMY_VERSION
from evals.trajectory_checks import evaluate_scenario
from evals.turns import TURN_CLASSIFIER_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = PROJECT_ROOT / "evals" / "datasets"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "evals" / "reports"
TRACE_PATH = PROJECT_ROOT / "evals" / "traces" / "trajectory.jsonl"
CASSETTE_DIR = PROJECT_ROOT / "evals" / "cassettes"

#: Splits where a single failure blocks the release regardless of the average.
SAFETY_SPLIT = "safety"

SUITES = ("l0", "trajectory", "multiturn", "content", "judge", "dataset", "feedback", "faults")
MODES = ("deterministic", "replay", "live")


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


def load_dataset(name: str, only: list[str] | None = None, split: list[str] | None = None) -> list[dict[str, Any]]:
    rows = list(iter_jsonl(DATASET_DIR / name))
    if only:
        wanted = set(only)
        rows = [row for row in rows if row.get("id") in wanted]
    if split:
        wanted_splits = set(split)
        rows = [row for row in rows if str(row.get("split")) in wanted_splits]
    return rows


def dataset_version(name: str) -> str:
    versions = {str(row.get("version")) for row in iter_jsonl(DATASET_DIR / name)}
    versions.discard("None")
    return ",".join(sorted(versions)) or "unknown"


def dataset_version_at(path: Path) -> str:
    """Version string for a dataset read from an explicit path.

    Needed because the feedback dataset can live outside ``DATASET_DIR`` (a CI
    job may point at a staged file), and a missing file must degrade to
    ``unknown`` rather than crashing the run.
    """

    if not path.exists():
        return "unknown"
    versions = {str(row.get("version")) for row in iter_jsonl(path)}
    versions.discard("None")
    return ",".join(sorted(versions)) or "unknown"


def quiet_logger() -> None:
    """Keep the report readable by dropping INFO logs from the console."""

    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except Exception:
        pass


def cassette_mode_for(mode: str, record: bool) -> str:
    """Map the CLI mode onto a cassette mode.

    - ``live`` never touches cassettes.
    - ``record`` forces ``record`` even in deterministic mode.
    - ``replay`` reads only.
    """

    if mode == "live":
        return "off"
    if mode == "replay":
        return "replay"
    return "record" if record else "off"


def uniform_case(
    *,
    case_id: str,
    split: str | None,
    passed: bool,
    checks: list[Any],
    summary: str,
    failed_checks: list[str] | None = None,
    known_gap: bool = False,
    known_gap_ref: str = "",
    known_gap_reason: str = "",
    negative_control: bool = False,
    expect_failures: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    latency_ms: float = 0.0,
    gate: bool = True,
    dataset: str | None = None,
    executed: bool = True,
) -> dict[str, Any]:
    """One case in the single shape every suite and the gates agree on."""

    check_dicts = [
        check.to_dict() if hasattr(check, "to_dict") else dict(check) for check in checks
    ]
    if failed_checks is None:
        failed_checks = sorted(
            {
                (check.get("check_id") if isinstance(check, dict) else check.check_id)
                for check in checks
                if not (check.get("passed") if isinstance(check, dict) else check.passed)
            }
        )
    return {
        "case_id": case_id,
        "dataset": dataset,
        "split": split,
        "executed": executed,
        "pass_at_1": bool(passed),
        "pass_pow_k": bool(passed),
        "known_gap": bool(known_gap),
        "known_gap_ref": known_gap_ref,
        "known_gap_reason": known_gap_reason,
        "negative_control": bool(negative_control),
        "expect_failures": sorted(set(expect_failures or [])),
        "summary": summary,
        "failed_checks": list(failed_checks),
        "checks": check_dicts,
        "metadata": metadata or {},
        "cost": cost or {"llm_calls": 0, "tokens": 0},
        "latency_ms": float(latency_ms or 0.0),
        "gate": bool(gate),
        "taxonomy": taxonomy_module.categorise(list(failed_checks)),
    }


def find_workdir() -> Path:
    return PROJECT_ROOT


# ---------------------------------------------------------------------------
# L0 suite
# ---------------------------------------------------------------------------


async def run_l0_suite() -> dict[str, Any]:
    results = await checks_module.run_l0_checks()
    cases = [
        uniform_case(
            case_id=result.check_id,
            split="l0",
            passed=result.passed,
            checks=[result],
            summary=result.detail or result.title,
            known_gap=bool(result.known_gap),
            known_gap_ref=result.known_gap_ref,
            gate=True,
            dataset="l0",
        )
        for result in results
    ]
    return {
        "suite": "l0",
        "granularity": "invariant",
        "cases": cases,
        "checks": [result.to_dict() for result in results],
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if not result.passed and not result.known_gap),
        "known_gaps": sum(1 for result in results if not result.passed and result.known_gap),
    }


# ---------------------------------------------------------------------------
# trajectory suite
# ---------------------------------------------------------------------------


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


def replay_miss_case(*, case_id: str, split: str | None, error: str, dataset: str) -> dict[str, Any]:
    """A stale cassette is a failure, not a crash.

    Replay is only useful if a miss is loud (``cassette.py``); but it must be
    loud *about the case*, so the rest of the suite still runs and the gate
    reports exactly which recording went stale.
    """

    check = CheckResult(
        check_id="L2.replay_miss",
        layer="L2",
        title="Replay 录制命中",
        passed=False,
        detail=f"重放缺少录制（录制可能已失效）：{error}",
    )
    return uniform_case(
        case_id=case_id,
        split=split,
        passed=False,
        checks=[check],
        summary=f"Replay miss: {error}",
        failed_checks=["L2.replay_miss"],
        gate=True,
        dataset=dataset,
    )


async def run_trajectory_suite(
    scenarios: list[dict[str, Any]],
    *,
    mode: str,
    repeat: int,
    trace_path: Path | None = None,
    dataset_version_value: str | None = None,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
) -> dict[str, Any]:
    from app.agents.handoffs.travel_agent import get_llm
    from app.observability.trace import write_jsonl

    candidates = await candidate_tool_map()
    model_factory = None if mode == "deterministic" else (None if mode == "replay" else get_llm)

    cases: list[dict[str, Any]] = []

    for scenario in scenarios:
        attempts: list[dict[str, Any]] = []
        miss: CassetteMiss | None = None
        for attempt in range(max(1, repeat)):
            try:
                outcome = await run_scenario(
                    scenario,
                    mode=mode,
                    model_factory=model_factory,
                    dataset_version=dataset_version_value,
                    dataset_split=str(scenario.get("split")),
                    cassette_mode=cassette_mode,
                    cassette_dir=cassette_dir,
                    code_sha=code_sha,
                )
            except CassetteMiss as exc:
                miss = exc
                break
            verdict = evaluate_scenario(scenario, outcome, candidates)
            verdict["attempt"] = attempt + 1
            verdict["mode"] = mode
            verdict["description"] = scenario.get("description", "")
            verdict["split"] = scenario.get("split")
            verdict["tools_called"] = outcome.tool_names
            verdict["final_step"] = outcome.final_state.get("current_step")
            verdict["llm_calls"] = outcome.record.get("cost", {}).get("llm_calls", 0)
            verdict["error"] = outcome.error
            verdict["latency_ms"] = outcome.latency_ms
            verdict["cassette"] = outcome.cassette
            attempts.append(verdict)

            if trace_path is not None:
                write_jsonl(outcome.record, trace_path)

        if miss is not None:
            cases.append(
                replay_miss_case(
                    case_id=str(scenario.get("id")),
                    split=scenario.get("split"),
                    error=str(miss),
                    dataset="trajectory_scenarios.jsonl",
                )
            )
            continue

        first = attempts[0]
        cost = first.get("cost") or {}
        case = uniform_case(
            case_id=scenario["id"],
            split=scenario.get("split"),
            passed=first["passed"],
            checks=first["checks"],
            summary=first["summary"],
            failed_checks=first["failed_checks"],
            known_gap=first["known_gap"],
            known_gap_ref=first.get("known_gap_ref", ""),
            negative_control=first["negative_control"],
            expect_failures=first.get("expect_failures", []),
            metadata={
                "description": scenario.get("description", ""),
                "tools_called": first["tools_called"],
                "final_step": first["final_step"],
                "difficulty": (scenario.get("metadata") or {}).get("difficulty"),
                "risk_level": (scenario.get("metadata") or {}).get("risk_level"),
                "cassette": first.get("cassette", {}),
            },
            cost={"llm_calls": first.get("llm_calls", 0), "tokens": 0},
            latency_ms=first.get("latency_ms", 0.0),
            gate=True,
            dataset="trajectory_scenarios.jsonl",
        )
        case["pass_pow_k"] = all(item["passed"] for item in attempts) if not first["negative_control"] else first["passed"]
        case["pass_at_1"] = first["passed"]
        case["passed"] = first["passed"]
        case["attempts"] = attempts
        cases.append(case)

    return {
        "suite": "trajectory",
        "granularity": "trajectory",
        "mode": mode,
        "repeat": repeat,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": sum(1 for case in cases if case["negative_control"]),
        "regressions": sum(
            1
            for case in cases
            if not case["pass_at_1"] and not case["known_gap"] and not case["negative_control"]
        ),
    }


# ---------------------------------------------------------------------------
# multiturn suite
# ---------------------------------------------------------------------------


async def run_multiturn_suite(
    scenarios: list[dict[str, Any]],
    *,
    mode: str,
    repeat: int,
    dataset_version_value: str | None = None,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    for scenario in scenarios:
        passes: list[bool] = []
        last = None
        miss: CassetteMiss | None = None
        for _ in range(max(1, repeat)):
            try:
                outcome = await run_multiturn_scenario(
                    scenario,
                    mode=mode,
                    dataset_version=dataset_version_value,
                    code_sha=code_sha,
                    cassette_mode=cassette_mode,
                    cassette_dir=cassette_dir,
                )
            except CassetteMiss as exc:
                miss = exc
                break
            last = outcome
            passes.append(outcome.passed)

        if miss is not None:
            cases.append(
                replay_miss_case(
                    case_id=str(scenario.get("id")),
                    split=scenario.get("split"),
                    error=str(miss),
                    dataset=MULTITURN_DATASET,
                )
            )
            continue

        assert last is not None
        case = uniform_case(
            case_id=last.case_id,
            split=last.split,
            passed=passes[0],
            checks=last.checks,
            summary=last.summary,
            failed_checks=last.failed_checks,
            negative_control=last.negative_control,
            expect_failures=last.expect_failures,
            metadata={
                "description": scenario.get("description", ""),
                "turns": len(last.turns),
                "turn_layer": last.turn_layer,
                "difficulty": (scenario.get("metadata") or {}).get("difficulty"),
                "risk_level": (scenario.get("metadata") or {}).get("risk_level"),
                "simulator": (scenario.get("simulator") or {}).get("kind", "scripted"),
            },
            cost={"llm_calls": (last.trace or {}).get("cost", {}).get("llm_calls", 0), "tokens": 0},
            latency_ms=float((last.trace or {}).get("cost", {}).get("latency_ms", 0.0) or 0.0),
            gate=True,
            dataset=MULTITURN_DATASET,
        )
        case["pass_pow_k"] = all(passes)
        case["pass_at_1"] = passes[0]
        case["passed"] = passes[0]
        cases.append(case)

    return {
        "suite": "multiturn",
        "granularity": "turn",
        "mode": mode,
        "repeat": repeat,
        "classifier_version": TURN_CLASSIFIER_VERSION,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": sum(1 for case in cases if case["negative_control"]),
        "regressions": sum(
            1
            for case in cases
            if not case["pass_at_1"] and not case["known_gap"] and not case["negative_control"]
        ),
    }


# ---------------------------------------------------------------------------
# content suite (result layer: actually executes the 240 rows)
# ---------------------------------------------------------------------------


def content_case_from_outcome(outcome: Any, *, gate: bool) -> dict[str, Any]:
    """Map a ``ContentOutcome`` onto the uniform case shape.

    The probes themselves decide what is a hard failure and what is a documented
    gap (via ``CheckResult.known_gap``), so the runner only has to translate.
    """

    if outcome.passed:
        summary = "全部通过"
    elif outcome.known_gap:
        summary = "已知缺口: " + ", ".join(sorted({c.check_id for c in outcome.gap_checks}))
    else:
        summary = "失败项: " + ", ".join(outcome.hard_failed_checks)

    return uniform_case(
        case_id=outcome.case_id,
        split=outcome.split,
        passed=outcome.passed,
        checks=outcome.checks,
        summary=summary,
        failed_checks=outcome.hard_failed_checks,
        known_gap=outcome.known_gap,
        known_gap_ref=outcome.known_gap_ref,
        known_gap_reason=outcome.known_gap_reason,
        metadata=outcome.metadata,
        cost={
            "llm_calls": (outcome.trace or {}).get("cost", {}).get("llm_calls", 0),
            "tokens": (
                (outcome.trace or {}).get("cost", {}).get("input_tokens", 0)
                + (outcome.trace or {}).get("cost", {}).get("output_tokens", 0)
            ),
        },
        latency_ms=outcome.latency_ms,
        gate=gate,
        dataset=outcome.dataset,
    )


async def run_content_suite(
    *,
    travel_rows: list[dict[str, Any]],
    mcp_rows: list[dict[str, Any]],
    rag_rows: list[dict[str, Any]],
    mode: str,
    repeat: int,
    dataset_version_value: str,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
    course: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Execute the content rows and separate 执行量 from 校验量.

    ``travel`` rows are genuinely driven through the state machine (behavioural);
    ``mcp`` / ``rag`` rows are capability probes (no agent execution). The
    distinction is recorded in ``probe`` so the report never conflates them.
    """

    index = await candidate_tool_index()
    rag_index = LocalBm25Index(load_local_corpus())
    corpus = load_local_corpus()

    cases: list[dict[str, Any]] = []
    not_executed: list[dict[str, Any]] = []

    # --- travel: behavioural execution ----------------------------------
    for row in travel_rows:
        try:
            outcome = await run_travel_probe(
                row,
                index=index,
                mode=mode,
                dataset_version=dataset_version_value,
                code_sha=code_sha,
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
            )
        except CassetteMiss as exc:
            cases.append(
                replay_miss_case(
                    case_id=str(row.get("id")),
                    split=row.get("split"),
                    error=str(exc),
                    dataset=TRAVEL_DATASET,
                )
            )
            continue
        if not outcome.executed:
            not_executed.append(
                {
                    "case_id": outcome.case_id,
                    "dataset": TRAVEL_DATASET,
                    "split": outcome.split,
                    "reason": outcome.failed_checks[0] if outcome.failed_checks else "execution_incomplete",
                    "detail": outcome.checks[0].detail if outcome.checks else "",
                }
            )
            continue

        outcome.metadata.setdefault("probe", "travel_walk")
        outcome.metadata.setdefault("executed_agent", True)
        cases.append(
            content_case_from_outcome(outcome, gate=str(outcome.split) == SAFETY_SPLIT)
        )

    # --- mcp: static capability probe -----------------------------------
    for row in mcp_rows:
        outcome = await run_mcp_probe(row, index=index)
        outcome.metadata.setdefault("executed_agent", False)
        cases.append(
            content_case_from_outcome(outcome, gate=str(outcome.split) == SAFETY_SPLIT)
        )

    # --- rag: offline retrieval probe -----------------------------------
    for row in rag_rows:
        outcome = run_rag_probe(row, index=rag_index)
        outcome.metadata.setdefault("executed_agent", False)
        cases.append(
            content_case_from_outcome(outcome, gate=str(outcome.split) == SAFETY_SPLIT)
        )

    by_dataset: dict[str, dict[str, Any]] = {}
    for case in cases:
        bucket = by_dataset.setdefault(
            case["dataset"], {"total": 0, "passed": 0, "failed": 0, "known_gap": 0, "executed_agent": case["metadata"].get("executed_agent", False)}
        )
        bucket["total"] += 1
        if case["pass_at_1"]:
            bucket["passed"] += 1
        elif case["known_gap"]:
            bucket["known_gap"] += 1
        else:
            bucket["failed"] += 1
    for bucket in by_dataset.values():
        scored = bucket["passed"] + bucket["failed"]
        bucket["success_rate"] = round(bucket["passed"] / scored, 4) if scored else None

    return {
        "suite": "content",
        "granularity": "result",
        "mode": mode,
        "repeat": repeat,
        "known_gap_table_version": CONTENT_KNOWN_GAPS_VERSION,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": 0,
        "regressions": sum(
            1 for case in cases if not case["pass_at_1"] and not case["known_gap"]
        ),
        "not_executed": not_executed,
        "by_dataset": by_dataset,
        "coverage": {
            "structural_rows": len(travel_rows) + len(mcp_rows) + len(rag_rows),
            "behavioural_rows": sum(
                1 for case in cases if case["metadata"].get("executed_agent")
            ),
            "probe_rows": sum(
                1 for case in cases if not case["metadata"].get("executed_agent")
            ),
            "not_executed_rows": len(not_executed),
            "corpus": corpus_coverage(rag_rows, corpus),
        },
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
        "granularity": "structural",
        "passed": result.passed,
        "detail": result.detail,
        "failures": result.data.get("failures", []),
        "files": files,
        "total_rows": sum(info["rows"] for info in files),
    }


# ---------------------------------------------------------------------------
# feedback suite (W5): promoted online-review cases, re-run in CI
# ---------------------------------------------------------------------------


async def run_feedback_suite(
    *,
    mode: str,
    repeat: int,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
    split_filter: list[str] | None = None,
    only: list[str] | None = None,
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    """Re-run cases promoted out of the online review queue.

    Two things are checked here, and they are different on purpose:

    1. **契约** — every promoted row still carries its provenance (reviewer,
       correct behaviour, source trace). A row that lost it is a *failure*: the
       entire point of the queue is that a human signed off.
    2. **行为** — rows a reviewer upgraded to a runnable kind
       (``runnable_kind == "agent_planning"`` with an ``input``) are driven
       through the real state machine, so the corrected behaviour is actually
       asserted rather than merely recorded.

    A contract-only row is reported as ``not_executed`` — never as a pass. That
    keeps the coverage number honest: prose is a *record* of a fix, not a test
    of one.
    """

    from evals.content import run_travel_probe
    from evals.online.promote import FEEDBACK_DATASET, contract_failures, load_feedback_cases

    dataset_path = dataset_path or (DATASET_DIR / FEEDBACK_DATASET)
    rows = load_feedback_cases(dataset_path)
    if only:
        wanted = set(only)
        rows = [row for row in rows if str(row.get("id")) in wanted]
    if split_filter:
        wanted_splits = set(split_filter)
        rows = [row for row in rows if str(row.get("split")) in wanted_splits]

    index = await candidate_tool_index()
    cases: list[dict[str, Any]] = []
    not_executed: list[dict[str, Any]] = []
    contract_violations = 0

    for row in rows:
        case_id = str(row.get("id"))
        violations = contract_failures(row)
        if violations:
            contract_violations += 1
            cases.append(
                uniform_case(
                    case_id=case_id,
                    split=row.get("split"),
                    passed=False,
                    checks=[
                        {
                            "check_id": check_id,
                            "title": "晋升用例必须保留人工复核provenance",
                            "passed": False,
                            "detail": f"{case_id} 缺失 provenance 字段",
                            "known_gap": False,
                        }
                        for check_id in violations
                    ],
                    summary="晋升用例 provenance 缺失（人工复核被绕过）",
                    failed_checks=violations,
                    gate=True,
                    dataset=FEEDBACK_DATASET,
                )
            )
            continue

        runnable = str(row.get("type")) == "agent_planning" and bool(row.get("input"))
        if not runnable:
            not_executed.append(
                {
                    "case_id": case_id,
                    "dataset": FEEDBACK_DATASET,
                    "split": row.get("split"),
                    "reason": "contract_only",
                    "detail": "只记录了「正确行为」文本，无可执行的 input；不参与通过率",
                }
            )
            continue

        try:
            outcome = await run_travel_probe(
                row,
                index=index,
                mode=mode,
                dataset_version=dataset_version_at(dataset_path),
                code_sha=code_sha,
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
            )
        except CassetteMiss as exc:
            cases.append(
                replay_miss_case(
                    case_id=case_id,
                    split=row.get("split"),
                    error=str(exc),
                    dataset=FEEDBACK_DATASET,
                )
            )
            continue

        if not outcome.executed:
            not_executed.append(
                {
                    "case_id": case_id,
                    "dataset": FEEDBACK_DATASET,
                    "split": row.get("split"),
                    "reason": outcome.failed_checks[0] if outcome.failed_checks else "execution_incomplete",
                    "detail": outcome.checks[0].detail if outcome.checks else "",
                }
            )
            continue

        outcome.metadata.setdefault("probe", "feedback_regression")
        outcome.metadata.setdefault("executed_agent", True)
        outcome.metadata.setdefault("origin_kind", "online_review")
        case = content_case_from_outcome(outcome, gate=True)
        case["origin"] = row.get("origin", {})
        case["reviewer"] = row.get("reviewer", "")
        cases.append(case)

    scored = [case for case in cases if not case.get("known_gap")]
    return {
        "suite": "feedback",
        "granularity": "result",
        "mode": mode,
        "repeat": repeat,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": 0,
        "regressions": sum(
            1 for case in cases if not case["pass_at_1"] and not case["known_gap"]
        ),
        "not_executed": not_executed,
        "contract_violations": contract_violations,
        "scored": len(scored),
        "coverage": {
            "promoted_rows": len(rows),
            "behavioural_rows": sum(
                1 for case in cases if case["metadata"].get("executed_agent")
            ),
            "contract_only_rows": len(not_executed),
        },
    }


# ---------------------------------------------------------------------------
# fault-injection suite (W6): capability boundary under broken dependencies
# ---------------------------------------------------------------------------

FAULT_DATASET = "fault_scenarios.jsonl"


async def run_fault_suite(
    scenarios: list[dict[str, Any]],
    *,
    mode: str,
    repeat: int,
    dataset_version_value: str | None = None,
    cassette_mode: str = "off",
    cassette_dir: Path | None = None,
    code_sha: str = "",
) -> dict[str, Any]:
    """Push the agent against broken dependencies and grade its degradation.

    The happy path is already covered by L2/L1. What this suite adds is the
    D09 §6 question a passing suite cannot answer: *where* does the agent stop
    being correct — and does it stop honestly, or does it invent an itinerary
    around the hole?
    """

    from evals.faults import (
        FaultExpectation,
        FaultInjector,
        evaluate_fault_scenario,
        parse_faults,
    )
    from evals.harness import run_scenario

    model_factory = None if mode in ("deterministic", "replay") else None
    if mode == "live":
        from app.agents.handoffs.travel_agent import get_llm

        model_factory = get_llm

    cases: list[dict[str, Any]] = []

    for scenario in scenarios:
        specs = parse_faults(scenario.get("faults") or [])
        expectation = FaultExpectation.from_dict(scenario.get("expectations"))
        injector = FaultInjector(specs)

        try:
            outcome = await run_scenario(
                scenario,
                mode=mode,
                model_factory=model_factory,
                dataset_version=dataset_version_value,
                dataset_split=str(scenario.get("split")),
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=code_sha,
                injector=injector,
            )
        except CassetteMiss as exc:
            cases.append(
                replay_miss_case(
                    case_id=str(scenario.get("id")),
                    split=scenario.get("split"),
                    error=str(exc),
                    dataset=FAULT_DATASET,
                )
            )
            continue

        verdict = evaluate_fault_scenario(
            case_id=str(scenario.get("id")),
            specs=specs,
            injector=injector,
            record=outcome.record,
            final_state=outcome.final_state,
            tool_names=outcome.tool_names,
            expectation=expectation,
            negative_control=bool(scenario.get("negative_control")),
            expect_failures=list(scenario.get("expect_failures") or []),
        )

        cases.append(
            uniform_case(
                case_id=verdict["case_id"],
                split=scenario.get("split"),
                passed=verdict["passed"],
                checks=verdict["checks"],
                summary=verdict["summary"],
                failed_checks=verdict["failed_checks"],
                negative_control=verdict["negative_control"],
                expect_failures=verdict["expect_failures"],
                gate=True,
                metadata={
                    "description": scenario.get("description", ""),
                    "fault_kinds": [spec.kind for spec in specs],
                    "fault_fired": verdict["faults"].get("fired", 0),
                    "final_step": outcome.final_state.get("current_step"),
                    "difficulty": (scenario.get("metadata") or {}).get("difficulty"),
                    "risk_level": (scenario.get("metadata") or {}).get("risk_level"),
                    "executed_agent": True,
                },
                cost={
                    "llm_calls": outcome.record.get("cost", {}).get("llm_calls", 0),
                    "tokens": 0,
                },
                latency_ms=outcome.latency_ms,
                dataset=FAULT_DATASET,
            )
        )

    return {
        "suite": "faults",
        "granularity": "trajectory",
        "mode": mode,
        "repeat": repeat,
        "faults_version": FAULTS_VERSION,
        "cases": cases,
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case["pass_at_1"]),
        "pass_pow_k": sum(1 for case in cases if case["pass_pow_k"]),
        "known_gaps": sum(1 for case in cases if case["known_gap"] and not case["pass_at_1"]),
        "negative_controls": sum(1 for case in cases if case["negative_control"]),
        "regressions": sum(
            1
            for case in cases
            if not case["pass_at_1"] and not case["known_gap"] and not case["negative_control"]
        ),
        "faults_fired": sum(int((case.get("metadata") or {}).get("fault_fired") or 0) for case in cases),
    }


# ---------------------------------------------------------------------------
# judge suite (W4)
# ---------------------------------------------------------------------------


async def run_judge_suite(*, judge_kind: str = "rule") -> dict[str, Any]:
    """Calibrate the judge against the gold set and gate on Cohen's kappa.

    The suite exists so the rubric itself is regression-tested: edit a rubric and
    the kappa moves, and this gate says whether the judge is still fit to score
    automatically (D09 §5.2).
    """

    from evals.judge import build_judge, calibrate
    from evals.judge.calibration import load_gold
    from evals.judge.rubrics import KAPPA_THRESHOLD

    gold = load_gold()
    if not gold:
        check = CheckResult(
            check_id="L4.judge_kappa",
            layer="L4",
            title="Judge 与人工盲评一致性",
            passed=False,
            detail="缺少 gold 数据集，无法校准",
        )
        return {
            "suite": "judge",
            "granularity": "capability",
            "cases": [
                uniform_case(
                    case_id="L4.judge_kappa",
                    split="judge",
                    passed=False,
                    checks=[check],
                    summary="缺少 gold 数据集",
                    dataset="judge_gold.jsonl",
                )
            ],
            "calibration": None,
            "total": 1,
            "pass_at_1": 0,
            "pass_pow_k": 0,
            "known_gaps": 0,
            "negative_controls": 0,
            "regressions": 1,
        }

    model_factory = None
    if judge_kind == "llm":
        try:
            from app.agents.handoffs.travel_agent import get_llm

            model_factory = get_llm
        except Exception:
            model_factory = None

    judge = build_judge(judge_kind, model_factory=model_factory)
    report = calibrate(judge, gold, dimension="faithfulness")

    check = CheckResult(
        check_id="L4.judge_kappa",
        layer="L4",
        title="Judge 与人工盲评一致性（Cohen's kappa）",
        passed=report.passes_gate,
        detail=report.summary(),
        data=report.to_dict(),
    )
    case = uniform_case(
        case_id="L4.judge_kappa",
        split="judge",
        passed=report.passes_gate,
        checks=[check],
        summary=report.summary(),
        failed_checks=[] if report.passes_gate else ["L4.judge_kappa"],
        metadata={
            "judge_version": report.judge_version,
            "rubric_version": report.rubric_version,
            "kappa": report.kappa,
            "mode": report.mode,
        },
        dataset="judge_gold.jsonl",
    )
    return {
        "suite": "judge",
        "granularity": "capability",
        "cases": [case],
        "calibration": report.to_dict(),
        "kappa_threshold": KAPPA_THRESHOLD,
        "total": 1,
        "pass_at_1": int(report.passes_gate),
        "pass_pow_k": int(report.passes_gate),
        "known_gaps": 0,
        "negative_controls": 0,
        "regressions": 0 if report.passes_gate else 1,
    }


# ---------------------------------------------------------------------------
# aggregation / gates
# ---------------------------------------------------------------------------


def collect_cases(results: list[dict[str, Any]], *, gate_only: bool = True) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for result in results:
        for case in result.get("cases") or []:
            if gate_only and not case.get("gate", True):
                continue
            collected.append(case)
    return collected


def summary_for(cases: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [case for case in cases if not case.get("known_gap")]
    return {
        "total": len(cases),
        "pass_at_1": sum(1 for case in cases if case.get("pass_at_1")),
        "pass_pow_k": sum(1 for case in cases if case.get("pass_pow_k")),
        "known_gaps": sum(1 for case in cases if case.get("known_gap") and not case.get("pass_at_1")),
        "failures": sum(
            1
            for case in cases
            if not case.get("pass_at_1") and not case.get("known_gap") and not case.get("negative_control")
        ),
        "scored": len(scored),
        "by_split": gates_module.slice_report(cases, "split"),
    }


def capability_profile(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Result-layer capability slice: pass rate by task shape, not one总分."""

    return {
        "by_dataset": gates_module.slice_report(cases, "dataset"),
        "by_difficulty": gates_module.slice_report(cases, "metadata.difficulty"),
        "by_risk": gates_module.slice_report(cases, "metadata.risk_level"),
        "by_split": gates_module.slice_report(cases, "split"),
    }


def failure_taxonomy_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Cluster failures by taxonomy label (D09 §4.5 — the point of labelling)."""

    counts: dict[str, int] = {}
    categories: dict[str, int] = {}
    for case in cases:
        if case.get("pass_at_1") or case.get("known_gap"):
            continue
        label = (case.get("taxonomy") or {}).get("primary")
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
        category = (case.get("taxonomy") or {}).get("category") or "other"
        categories[category] = categories.get(category, 0) + 1
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "by_label": dict(sorted(counts.items(), key=lambda item: -item[1])),
        "by_category": dict(sorted(categories.items(), key=lambda item: -item[1])),
    }


def latency_percentiles(cases: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(case.get("latency_ms") or 0.0) for case in cases if case.get("latency_ms")]
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        "count": len(values),
        "p50": round(gates_module.percentile(values, 50), 2),
        "p95": round(gates_module.percentile(values, 95), 2),
        "p99": round(gates_module.percentile(values, 99), 2),
    }


def cost_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [int((case.get("cost") or {}).get("llm_calls") or 0) for case in cases]
    tokens = [int((case.get("cost") or {}).get("tokens") or 0) for case in cases]
    return {
        "total_llm_calls": sum(calls),
        "max_llm_calls_per_case": max(calls) if calls else 0,
        "total_tokens": sum(tokens),
        "max_tokens_per_case": max(tokens) if tokens else 0,
    }


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def diff_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Report where two runs disagree, cell by cell (D09 §6 一次只改一个变量).

    Compares the *slices*, not a single total, so a movement is always attributed
    to a grid cell such as ``结果层 × regression``.
    """

    deltas: list[dict[str, Any]] = []

    def _split_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for result in report.get("results") or []:
            suite = result.get("suite")
            if not suite:
                continue
            cases = result.get("cases") or []
            out[suite] = gates_module.slice_report(cases, "split") if cases else {}
        return out

    cur = _split_map(current)
    prev = _split_map(previous)

    for suite in sorted(set(cur) | set(prev)):
        cur_splits = cur.get(suite) or {}
        prev_splits = prev.get(suite) or {}
        for split in sorted(set(cur_splits) | set(prev_splits)):
            cur_rate = (cur_splits.get(split) or {}).get("success_rate")
            prev_rate = (prev_splits.get(split) or {}).get("success_rate")
            if cur_rate is None or prev_rate is None:
                continue
            delta = round(cur_rate - prev_rate, 4)
            if delta != 0:
                deltas.append(
                    {
                        "suite": suite,
                        "split": split,
                        "previous": prev_rate,
                        "current": cur_rate,
                        "delta_pt": round(delta * 100, 2),
                    }
                )

    return {
        "previous_generated_at": previous.get("generated_at"),
        "previous_git_sha": previous.get("git_sha"),
        "deltas": deltas,
        "regressed": [item for item in deltas if item["delta_pt"] < 0],
        "improved": [item for item in deltas if item["delta_pt"] > 0],
    }


def _find_previous_report(explicit: str | None, report_dir: Path) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    candidates = sorted(report_dir.glob("*.json"))
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def build_report(
    results: list[dict[str, Any]],
    *,
    mode: str,
    repeat: int,
    sha: str,
    thresholds: gates_module.Thresholds,
    gates: list[gates_module.GateOutcome],
    coverage: dict[str, Any],
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "report_schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
        "mode": mode,
        "repeat": repeat,
        "thresholds": {
            "version": thresholds.version,
            "provisional": thresholds.provisional,
        },
        "taxonomy_version": TAXONOMY_VERSION,
        "turn_classifier_version": TURN_CLASSIFIER_VERSION,
        "cassette_schema_version": CASSETTE_SCHEMA_VERSION,
        "known_gap_table_version": CONTENT_KNOWN_GAPS_VERSION,
        "meta": extra_meta or {},
        "results": results,
        "gates": [gate.to_dict() for gate in gates],
        "coverage": coverage,
    }


def _failed_checks_text(case: dict[str, Any], limit: int = 3) -> str:
    if case["pass_at_1"]:
        return ""
    details = [
        f"{check['check_id']}: {check['detail']}"
        for check in case.get("checks", [])
        if not check.get("passed") and not check.get("known_gap")
    ]
    if not details and case.get("known_gap"):
        details = [
            f"{check['check_id']}（已知缺口）"
            for check in case.get("checks", [])
            if not check.get("passed")
        ]
    return "<br>".join(details[:limit]) or case.get("summary", "")


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Agent 评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 代码版本：`{report['git_sha']}`",
        f"- 执行模式：`{report['mode']}`（repeat={report['repeat']}）",
        f"- 阈值版本：`{report['thresholds']['version']}`"
        + ("（未标定）" if report["thresholds"]["provisional"] else "（已标定）"),
        f"- 失败标签版本：`{report['taxonomy_version']}`｜轮次分类器：`{report['turn_classifier_version']}`"
        f"｜Cassette：`{report['cassette_schema_version']}`",
    ]
    judge_meta = (report.get("meta") or {}).get("judge")
    if judge_meta:
        lines.append(
            f"- Judge：`{judge_meta.get('judge_version')}`｜rubric `{judge_meta.get('rubric_version')}`"
            f"｜kappa {judge_meta.get('kappa')}｜用法 `{judge_meta.get('mode')}`"
        )
    lines.append("")

    coverage = report.get("coverage") or {}
    if coverage:
        lines += [
            "## 覆盖度",
            "",
            f"- 结构校验样本：**{coverage.get('structural_rows', 0)}** 行（数据集合规）",
            f"- 行为执行样本：**{coverage.get('behavioural_rows', 0)}** 行（真的跑过 Agent）",
            f"- 能力探针样本：**{coverage.get('probe_rows', 0)}** 行（静态能力/检索探针）",
            f"- 无法执行样本：**{coverage.get('not_executed_rows', 0)}** 行（需求不可还原等）",
            "",
        ]
        corpus = coverage.get("corpus") or {}
        if corpus:
            lines += [
                f"- RAG 语料覆盖：{corpus.get('covered', 0)}/{corpus.get('rows', 0)}"
                f"（{corpus.get('coverage_rate', 0):.0%}，语料文档 {corpus.get('corpus_docs', 0)} 篇）",
                "",
            ]

    gates = report.get("gates") or []
    if gates:
        lines += ["## 发布门禁", "", "| 门禁 | 结果 | 说明 |", "|---|---|---|"]
        for gate in gates:
            flag = "✅" if gate["passed"] else "❌"
            lines.append(f"| `{gate['gate_id']}` | {flag} | {gate['detail']} |")
        lines.append("")

    taxonomy = report.get("failure_taxonomy") or {}
    if taxonomy.get("by_label"):
        lines += [
            "## 失败标签分布",
            "",
            "、".join(f"`{label}`×{count}" for label, count in taxonomy["by_label"].items()),
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

        elif suite in ("trajectory", "multiturn"):
            title = "L2 · 轨迹断言" if suite == "trajectory" else "L3 · 轮次断言"
            lines += [
                f"## {title}",
                "",
                (
                    f"pass@1 {result['pass_at_1']}/{result['total']}，"
                    f"pass^{result['repeat']} {result['pass_pow_k']}/{result['total']}，"
                    f"回归失败 {result['regressions']}，已知缺口 {result['known_gaps']}，"
                    f"负向对照 {result['negative_controls']}"
                ),
                "",
                "| 用例 | split | pass@1 | 失败项 |",
                "|---|---|---|---|",
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
                    f"| `{case['case_id']}` | {case['split']} | {flag} | {_failed_checks_text(case)} |"
                )
            lines.append("")

        elif suite == "content":
            lines += [
                "## L1 · 结果层执行（内容用例）",
                "",
                (
                    f"执行 {result['total']} 例，通过 {result['pass_at_1']}，"
                    f"已知缺口 {result['known_gaps']}，回归失败 {result['regressions']}，"
                    f"未执行 {len(result.get('not_executed') or [])}"
                ),
                "",
                "| 数据集 | 执行方式 | 通过 | 已知缺口 | 失败 | 成功率 |",
                "|---|---|---|---|---|---|",
            ]
            for dataset, bucket in sorted(result["by_dataset"].items()):
                kind = "行为执行" if bucket.get("executed_agent") else "探针"
                rate = bucket.get("success_rate")
                rate_text = f"{rate:.0%}" if rate is not None else "—"
                lines.append(
                    f"| `{dataset}` | {kind} | {bucket['passed']} | {bucket['known_gap']} | "
                    f"{bucket['failed']} | {rate_text} |"
                )
            lines.append("")

            gaps = [case for case in result["cases"] if case["known_gap"] and not case["pass_at_1"]]
            if gaps:
                lines += ["### 已知缺口（不计入回归门禁）", ""]
                buckets: dict[tuple[str, str], int] = {}
                for case in gaps:
                    key = (case.get("known_gap_reason", ""), case.get("known_gap_ref", ""))
                    buckets[key] = buckets.get(key, 0) + 1
                for (reason, ref), count in sorted(buckets.items(), key=lambda item: -item[1]):
                    lines.append(f"- {reason}（{ref}）｜涉及 {count} 例")
                lines.append("")

            if result.get("not_executed"):
                lines += ["### 未执行样本（不计入分母）", ""]
                for item in result["not_executed"][:20]:
                    lines.append(
                        f"- `{item['case_id']}`（{item['split']}）：{item['reason']}｜{item['detail'][:120]}"
                    )
                if len(result["not_executed"]) > 20:
                    lines.append(f"- …共 {len(result['not_executed'])} 例")
                lines.append("")

        elif suite == "faults":
            lines += [
                "## L6 · 故障注入（能力边界）",
                "",
                (
                    f"场景 {result['total']}｜通过 {result['pass_at_1']}｜"
                    f"注入故障 {result['faults_fired']} 次｜回归失败 {result['regressions']}"
                ),
                "",
                "| 用例 | 故障类型 | 触发 | 最终步 | 结果 | 失败项 |",
                "|---|---|---|---|---|---|",
            ]
            for case in result["cases"]:
                meta = case.get("metadata") or {}
                kinds = "、".join(meta.get("fault_kinds") or []) or "—"
                flag = "✅" if case["pass_at_1"] else ("⚠️ 已知缺口" if case["known_gap"] else "❌")
                lines.append(
                    f"| `{case['case_id']}` | {kinds} | {meta.get('fault_fired', 0)} | "
                    f"{meta.get('final_step', '—')} | {flag} | {_failed_checks_text(case)} |"
                )
            lines.append("")

        elif suite == "feedback":
            coverage = result.get("coverage") or {}
            lines += [
                "## L5 · 线上复核晋升（回归）",
                "",
                (
                    f"晋升用例 {coverage.get('promoted_rows', 0)} 行｜"
                    f"可执行（行为回归）{coverage.get('behavioural_rows', 0)} 行｜"
                    f"仅契约（不计分）{coverage.get('contract_only_rows', 0)} 行｜"
                    f"契约违规 {result.get('contract_violations', 0)}"
                ),
                "",
                (
                    f"pass@1 {result['pass_at_1']}/{result.get('scored', result['total'])}，"
                    f"回归失败 {result['regressions']}"
                ),
                "",
            ]
            if result["cases"]:
                lines += [
                    "| 晋升用例 | split | 复核人 | 结果 | 失败项 |",
                    "|---|---|---|---|---|",
                ]
                for case in result["cases"]:
                    flag = "✅" if case["pass_at_1"] else ("⚠️ 已知缺口" if case["known_gap"] else "❌")
                    lines.append(
                        f"| `{case['case_id']}` | {case['split']} | {case.get('reviewer', '—')} | "
                        f"{flag} | {_failed_checks_text(case)} |"
                    )
                lines.append("")
            else:
                lines += ["尚无晋升用例（线上闭环未产出反馈）", ""]

            if result.get("not_executed"):
                lines += ["### 仅记录「正确行为」文本、未参与计分", ""]
                for item in result["not_executed"][:20]:
                    lines.append(
                        f"- `{item['case_id']}`（{item['split']}）：{item['reason']}｜{item['detail'][:120]}"
                    )
                lines.append("")

        elif suite == "judge":
            calibration = result.get("calibration") or {}
            lines += [
                "## L4 · Judge 校准",
                "",
                (
                    f"Judge `{calibration.get('judge_version', '—')}`｜"
                    f"rubric `{calibration.get('rubric_version', '—')}`｜"
                    f"kappa **{calibration.get('kappa', 0)}**（阈值 {calibration.get('threshold', '—')}）｜"
                    f"一致率 {calibration.get('agreement', 0):.0%}｜n={calibration.get('sample_size', 0)}"
                ),
                "",
                f"用法判定：`{calibration.get('mode', '—')}`"
                "（auto_gate = 可用于自动门禁；coarse_screen = 仅粗筛；human_only = 必须回流人工）",
                "",
            ]
            if calibration.get("confusion"):
                lines += [
                    "| human\\judge | 判为通过 | 判为不通过 |",
                    "|---|---|---|",
                    f"| 通过 | {calibration['confusion'].get('human_pos_judge_pos', 0)} | "
                    f"{calibration['confusion'].get('human_pos_judge_neg', 0)} |",
                    f"| 不通过 | {calibration['confusion'].get('human_neg_judge_pos', 0)} | "
                    f"{calibration['confusion'].get('human_neg_judge_neg', 0)} |",
                    "",
                ]
            if calibration.get("disagreements"):
                lines += ["### 分歧样本（人工与 judge 不一致）", ""]
                for item in calibration["disagreements"][:10]:
                    lines.append(
                        f"- `{item['case_id']}`：人工={item['human']} judge={item['judge']}"
                        f"（score={item['judge_score']}）｜{item['evidence'][:80]}"
                    )
                lines.append("")

        elif suite == "dataset":
            headline = "✅ 全部合规" if result["passed"] else f"❌ {len(result['failures'])} 处问题"
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

    diff = report.get("diff")
    if diff:
        lines += [
            "## 版本对比（diff）",
            "",
            f"对比基线：`{diff.get('previous_git_sha')}`（{diff.get('previous_generated_at')}）",
            "",
        ]
        if not diff["deltas"]:
            lines += ["各切片无变化。", ""]
        else:
            lines += ["| 套件 | split | 之前 | 现在 | 变化 |", "|---|---|---|---|---|"]
            for item in diff["deltas"]:
                arrow = "🟢" if item["delta_pt"] > 0 else "🔴"
                lines.append(
                    f"| `{item['suite']}` | {item['split']} | {item['previous']:.0%} | "
                    f"{item['current']:.0%} | {arrow} {item['delta_pt']:+.1f}pt |"
                )
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
        default="all",
        choices=[*SUITES, "all"],
        help="which D09 layer to evaluate",
    )
    parser.add_argument(
        "--mode",
        default="deterministic",
        choices=list(MODES),
        help="deterministic = scripted model + stubbed externals; replay = recorded externals; live = real model and tools",
    )
    parser.add_argument(
        "--judge",
        default="rule",
        choices=["rule", "llm"],
        help="which judge to calibrate: rule = deterministic proxy, llm = rubric-driven model judge",
    )
    parser.add_argument("--repeat", type=int, default=1, help="repeats per case, for pass^k")
    parser.add_argument(
        "--split",
        default="",
        help="comma-separated splits to run (smoke,regression,safety,challenge)",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--only", default="", help="comma-separated case ids to run")
    parser.add_argument("--no-trace", action="store_true", help="skip writing trajectories to JSONL")
    parser.add_argument(
        "--record", action="store_true", help="record tool responses into a cassette"
    )
    parser.add_argument("--cassette-dir", default=str(CASSETTE_DIR))
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record this run as the baseline for gap comparison",
    )
    parser.add_argument(
        "--diff",
        nargs="?",
        const="__latest__",
        default=None,
        help="compare against a previous report (default: the latest in --report-dir)",
    )
    parser.add_argument("--verbose", action="store_true", help="keep INFO logs on the console")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not args.verbose:
        quiet_logger()

    sha = git_sha()
    split_filter = [item for item in (args.split or "").split(",") if item]
    only = [item for item in (args.only or "").split(",") if item]
    cassette_mode = cassette_mode_for(args.mode, args.record)
    cassette_dir = Path(args.cassette_dir)
    thresholds = gates_module.load_thresholds()

    results: list[dict[str, Any]] = []

    if args.suite in ("l0", "all"):
        results.append(await run_l0_suite())

    if args.suite in ("trajectory", "all"):
        scenarios = load_dataset("trajectory_scenarios.jsonl", only or None, split_filter or None)
        results.append(
            await run_trajectory_suite(
                scenarios,
                mode=args.mode,
                repeat=args.repeat,
                trace_path=None if args.no_trace else TRACE_PATH,
                dataset_version_value=dataset_version("trajectory_scenarios.jsonl"),
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=sha,
            )
        )

    if args.suite in ("multiturn", "all"):
        scenarios = load_dataset("multiturn_scenarios.jsonl", only or None, split_filter or None)
        results.append(
            await run_multiturn_suite(
                scenarios,
                mode=args.mode,
                repeat=args.repeat,
                dataset_version_value=dataset_version("multiturn_scenarios.jsonl"),
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=sha,
            )
        )

    if args.suite in ("content", "all"):
        results.append(
            await run_content_suite(
                travel_rows=load_dataset("travel_tasks.jsonl", only or None, split_filter or None),
                mcp_rows=load_dataset("mcp_tool_tasks.jsonl", only or None, split_filter or None),
                rag_rows=load_dataset("rag_qa.jsonl", only or None, split_filter or None),
                mode=args.mode,
                repeat=args.repeat,
                dataset_version_value=",".join(
                    dataset_version(name)
                    for name in ("travel_tasks.jsonl", "mcp_tool_tasks.jsonl", "rag_qa.jsonl")
                ),
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=sha,
            )
        )

    if args.suite in ("dataset", "all"):
        results.append(await run_dataset_suite())

    if args.suite in ("feedback", "all"):
        results.append(
            await run_feedback_suite(
                mode=args.mode,
                repeat=args.repeat,
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=sha,
                split_filter=split_filter or None,
                only=only or None,
            )
        )

    if args.suite in ("faults", "all"):
        results.append(
            await run_fault_suite(
                load_dataset(FAULT_DATASET, only or None, split_filter or None),
                mode=args.mode,
                repeat=args.repeat,
                dataset_version_value=dataset_version(FAULT_DATASET),
                cassette_mode=cassette_mode,
                cassette_dir=cassette_dir,
                code_sha=sha,
            )
        )

    if args.suite in ("judge", "all"):
        results.append(await run_judge_suite(judge_kind=args.judge))

    # --- gates over the gate-bearing cases ------------------------------
    gate_cases = collect_cases(results, gate_only=True)
    all_cases = collect_cases(results, gate_only=False)
    gate_outcomes = gates_module.evaluate_gates(
        gate_cases,
        thresholds=thresholds,
        repeat=args.repeat,
        latency_ms=[float(case.get("latency_ms") or 0.0) for case in gate_cases if case.get("latency_ms")],
    )

    summary = summary_for(gate_cases)
    baseline = gates_module.load_baseline(args.suite)
    gate_outcomes.append(
        gates_module.compare_against_baseline(summary, baseline, thresholds=thresholds)
    )

    # --- holdout governance ---------------------------------------------
    holdout_ids = [
        row["id"]
        for row in load_dataset("travel_tasks.jsonl")
        if str(row.get("split")) == gates_module.HOLDOUT_SPLIT
    ]
    leaks = gates_module.holdout_violations(holdout_ids)
    if holdout_ids:
        gate_outcomes.append(
            gates_module.GateOutcome(
                "G7.holdout_isolation",
                not leaks,
                "holdout 未被任何调优路径引用" if not leaks else f"{len(leaks)} 处泄漏",
                {"violations": leaks},
            )
        )

    # --- judge calibration gate (W4) ------------------------------------
    judge_result = next((r for r in results if r["suite"] == "judge"), None)
    if judge_result and judge_result.get("calibration"):
        calibration = judge_result["calibration"]
        gate_outcomes.append(
            gates_module.GateOutcome(
                "G8.judge_calibration",
                bool(calibration.get("passes_gate")),
                (
                    f"kappa {calibration.get('kappa')}（阈值 {calibration.get('threshold')}）"
                    f"｜用法 {calibration.get('mode')}"
                ),
                {
                    "judge_version": calibration.get("judge_version"),
                    "rubric_version": calibration.get("rubric_version"),
                    "kappa": calibration.get("kappa"),
                    "mode": calibration.get("mode"),
                },
            )
        )

    # --- coverage --------------------------------------------------------
    content_result = next((r for r in results if r["suite"] == "content"), None)
    dataset_result = next((r for r in results if r["suite"] == "dataset"), None)
    coverage = {
        "structural_rows": (dataset_result or {}).get("total_rows", 0),
        "behavioural_rows": sum(
            1 for result in results for case in (result.get("cases") or [])
            if case.get("metadata", {}).get("executed_agent")
        )
        + sum(
            len(result.get("cases") or [])
            for result in results
            if result["suite"] in ("trajectory", "multiturn")
        ),
        "probe_rows": (content_result or {}).get("coverage", {}).get("probe_rows", 0),
        "not_executed_rows": (content_result or {}).get("coverage", {}).get("not_executed_rows", 0),
        "corpus": (content_result or {}).get("coverage", {}).get("corpus", {}),
    }

    report = build_report(
        results,
        mode=args.mode,
        repeat=args.repeat,
        sha=sha,
        thresholds=thresholds,
        gates=gate_outcomes,
        coverage=coverage,
        extra_meta={
            "cassette_mode": cassette_mode,
            "capability_profile": capability_profile(all_cases),
            "failure_taxonomy": failure_taxonomy_report(gate_cases),
            "latency": latency_percentiles(gate_cases),
            "cost": cost_report(gate_cases),
            "judge": (judge_result or {}).get("calibration"),
            "dataset_versions": {
                name: dataset_version(name)
                for name in (
                    "travel_tasks.jsonl",
                    "mcp_tool_tasks.jsonl",
                    "rag_qa.jsonl",
                    "trajectory_scenarios.jsonl",
                    "multiturn_scenarios.jsonl",
                )
            },
        },
    )

    # --- diff ------------------------------------------------------------
    if args.diff is not None:
        report_dir = Path(args.report_dir)
        explicit = None if args.diff == "__latest__" else args.diff
        previous_path = _find_previous_report(explicit, report_dir)
        if previous_path:
            try:
                previous = json.loads(previous_path.read_text(encoding="utf-8"))
                report["diff"] = diff_reports(report, previous)
            except Exception:
                report["diff"] = None

    report_dir = Path(args.report_dir)
    json_path, md_path = write_reports(report, report_dir, args.suite)

    if args.update_baseline:
        gates_module.save_baseline(
            args.suite,
            summary,
            meta={"git_sha": sha, "mode": args.mode, "thresholds_version": thresholds.version},
        )

    # --- console summary ------------------------------------------------
    print(f"\n=== 评测汇总（{args.suite} / {args.mode}）===")
    hard_failures = 0
    for result in results:
        suite = result["suite"]
        if suite == "l0":
            print(
                f"L0  通过 {result['passed']}/{result['total']}"
                f"｜硬失败 {result['failed']}｜已知缺口 {result['known_gaps']}"
            )
            for check in result["checks"]:
                if not check["passed"]:
                    flag = "已知缺口" if check["known_gap"] else "硬失败"
                    print(f"    [{flag}] {check['check_id']}: {check['detail'][:140]}")
        elif suite in ("trajectory", "multiturn"):
            tag = "L2" if suite == "trajectory" else "L3"
            print(
                f"{tag}  pass@1 {result['pass_at_1']}/{result['total']}"
                f"｜pass^{result['repeat']} {result['pass_pow_k']}/{result['total']}"
                f"｜回归失败 {result['regressions']}"
                f"｜已知缺口 {result['known_gaps']}"
                f"｜负向对照 {result['negative_controls']}"
            )
            for case in result["cases"]:
                if case["pass_at_1"]:
                    continue
                flag = "已知缺口" if case["known_gap"] else ("对照失效" if case["negative_control"] else "回归失败")
                print(f"    [{flag}] {case['case_id']}: {case['summary'][:140]}")
        elif suite == "content":
            cov = result["coverage"]
            print(
                f"L1  执行 {result['total']}"
                f"｜行为 {cov['behavioural_rows']}｜探针 {cov['probe_rows']}"
                f"｜未执行 {cov['not_executed_rows']}"
                f"｜通过 {result['pass_at_1']}"
                f"｜已知缺口 {result['known_gaps']}｜失败 {result['regressions']}"
            )
            for dataset, bucket in sorted(result["by_dataset"].items()):
                rate = bucket.get("success_rate")
                print(
                    f"    {dataset}: 通过 {bucket['passed']}｜已知缺口 {bucket['known_gap']}"
                    f"｜失败 {bucket['failed']}｜成功率 {rate:.0%}"
                    if rate is not None
                    else f"    {dataset}: 通过 {bucket['passed']}"
                )
        elif suite == "faults":
            print(
                f"L6  场景 {result['total']}｜通过 {result['pass_at_1']}"
                f"｜注入故障 {result['faults_fired']} 次｜失败 {result['regressions']}"
            )
            for case in result["cases"]:
                if case["pass_at_1"]:
                    continue
                print(f"    [故障] {case['case_id']}: {case['summary'][:140]}")
        elif suite == "feedback":
            cov = result.get("coverage") or {}
            print(
                f"L5  晋升 {cov.get('promoted_rows', 0)} 行"
                f"｜行为回归 {cov.get('behavioural_rows', 0)}"
                f"｜仅契约 {cov.get('contract_only_rows', 0)}"
                f"｜契约违规 {result.get('contract_violations', 0)}"
                f"｜失败 {result['regressions']}"
            )
            for case in result["cases"]:
                if case["pass_at_1"]:
                    continue
                flag = "已知缺口" if case["known_gap"] else "回归失败"
                print(f"    [{flag}] {case['case_id']}: {case['summary'][:140]}")
        elif suite == "judge":
            calibration = result.get("calibration") or {}
            flag = "达标" if calibration.get("passes_gate") else "未达标"
            print(
                f"L4  judge={calibration.get('judge_version', '—')}"
                f"｜kappa {calibration.get('kappa', 0)}（{flag}）"
                f"｜用法 {calibration.get('mode', '—')}"
            )
        elif suite == "dataset":
            flag = "全部合规" if result["passed"] else f"失败 {len(result['failures'])} 处"
            print(f"DS  {result['total_rows']} 行｜{flag}")
            if not result["passed"]:
                for problem in result["failures"][:5]:
                    print(f"    [失败] {problem}")

    print("\n--- 发布门禁 ---")
    for gate in gate_outcomes:
        if gate.passed:
            print(f"  ✅ {gate.gate_id}: {gate.detail}")
        else:
            print(f"  ❌ {gate.gate_id}: {gate.detail}")
            hard_failures += 1

    diff = report.get("diff")
    if diff is not None:
        print("\n--- 版本对比 ---")
        if not diff["deltas"]:
            print("  各切片无变化")
        for item in diff["deltas"]:
            print(
                f"  {item['suite']}/{item['split']}: {item['previous']:.0%} → "
                f"{item['current']:.0%}（{item['delta_pt']:+.1f}pt）"
            )

    print(f"\n报告：{md_path}")
    print(f"JSON：{json_path}")
    return 1 if hard_failures else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
