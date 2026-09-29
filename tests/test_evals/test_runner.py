"""Runner helpers: uniform case shape, aggregation, diff, cassette mode."""

from __future__ import annotations

import json

import pytest

from evals import runner
from evals.checks import CheckResult
from evals.content import ContentOutcome


def _check(check_id, passed, known_gap=False):
    return CheckResult(
        check_id=check_id, layer="L2", title="t", passed=passed,
        known_gap=known_gap, known_gap_ref="ref" if known_gap else "",
    )


# ---------------------------------------------------------------------------
# cassette mode mapping
# ---------------------------------------------------------------------------


def test_cassette_mode_mapping():
    assert runner.cassette_mode_for("live", record=True) == "off"
    assert runner.cassette_mode_for("replay", record=True) == "replay"
    assert runner.cassette_mode_for("deterministic", record=False) == "off"
    assert runner.cassette_mode_for("deterministic", record=True) == "record"


# ---------------------------------------------------------------------------
# uniform case
# ---------------------------------------------------------------------------


def test_uniform_case_derives_failures_and_taxonomy():
    case = runner.uniform_case(
        case_id="c1",
        split="regression",
        passed=False,
        checks=[_check("L2.tools_called", True), _check("L2.arg_fidelity", False)],
        summary="boom",
    )

    assert case["failed_checks"] == ["L2.arg_fidelity"]
    assert case["taxonomy"]["primary"] == "tool_argument_error"
    assert case["gate"] is True
    assert case["pass_at_1"] is False


def test_uniform_case_marks_known_gap():
    case = runner.uniform_case(
        case_id="c1", split="safety", passed=False,
        checks=[_check("L2.approval_before_high_risk", False, known_gap=True)],
        summary="gap", known_gap=True, known_gap_ref="docs/x.md",
    )

    assert case["known_gap"] and case["known_gap_ref"] == "docs/x.md"


# ---------------------------------------------------------------------------
# content outcome mapping
# ---------------------------------------------------------------------------


def test_content_case_maps_known_gap():
    outcome = ContentOutcome(
        case_id="r1", dataset="rag_qa.jsonl", split="regression", executed=True,
        checks=[_check("L1.rag_entity_recall", False, known_gap=True)],
    )

    case = runner.content_case_from_outcome(outcome, gate=False)

    assert case["known_gap"] and not case["pass_at_1"]
    assert case["gate"] is False
    assert case["dataset"] == "rag_qa.jsonl"


def test_content_case_maps_hard_failure():
    outcome = ContentOutcome(
        case_id="t1", dataset="travel_tasks.jsonl", split="safety", executed=True,
        checks=[_check("L2.final_step", False)],
    )

    case = runner.content_case_from_outcome(outcome, gate=True)

    assert case["failed_checks"] == ["L2.final_step"]
    assert not case["known_gap"]


# ---------------------------------------------------------------------------
# replay miss
# ---------------------------------------------------------------------------


def test_replay_miss_is_a_gate_failure():
    case = runner.replay_miss_case(
        case_id="x", split="regression", error="missing tool", dataset="trajectory_scenarios.jsonl"
    )

    assert case["gate"] and not case["pass_at_1"]
    assert case["failed_checks"] == ["L2.replay_miss"]
    assert case["taxonomy"]["primary"] == "nondeterministic_failure"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _case(case_id, split, passed, known_gap=False):
    return {"case_id": case_id, "split": split, "pass_at_1": passed, "pass_pow_k": passed, "known_gap": known_gap}


def test_summary_for_counts_each_bucket():
    summary = runner.summary_for([
        _case("a", "regression", True),
        _case("b", "regression", False),
        _case("c", "regression", False, known_gap=True),
    ])

    assert summary["total"] == 3
    assert summary["pass_at_1"] == 1
    assert summary["failures"] == 1
    assert summary["known_gaps"] == 1


def test_failure_taxonomy_report_clusters_labels():
    cases = [
        {**runner.uniform_case(case_id="a", split="r", passed=False,
                               checks=[_check("L2.tools_called", False)], summary="x")},
        {**runner.uniform_case(case_id="b", split="r", passed=False,
                               checks=[_check("L2.tools_called", False)], summary="x")},
    ]

    report = runner.failure_taxonomy_report(cases)

    assert report["by_label"]["tool_selection_error"] == 2


def test_diff_reports_detects_a_regression():
    current = {
        "generated_at": "now", "git_sha": "fff",
        "results": [
            {"suite": "trajectory", "cases": [_case("a", "regression", False)]},
        ],
    }
    previous = {
        "generated_at": "then", "git_sha": "aaa",
        "results": [
            {"suite": "trajectory", "cases": [_case("a", "regression", True)]},
        ],
    }

    diff = runner.diff_reports(current, previous)

    assert diff["regressed"]
    assert diff["deltas"][0]["delta_pt"] == -100.0


def test_diff_reports_identical_runs_is_empty():
    report = {
        "generated_at": "now", "git_sha": "fff",
        "results": [{"suite": "l0", "cases": [_case("a", "l0", True)]}],
    }

    assert runner.diff_reports(report, report)["deltas"] == []


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_latency_percentiles_and_cost_report():
    cases = [
        {"latency_ms": 10.0, "cost": {"llm_calls": 2, "tokens": 100}},
        {"latency_ms": 30.0, "cost": {"llm_calls": 4, "tokens": 300}},
    ]

    latency = runner.latency_percentiles(cases)
    cost = runner.cost_report(cases)

    assert latency["count"] == 2
    assert latency["p50"] == 10.0 and latency["p99"] == 30.0
    assert cost["total_llm_calls"] == 6
    assert cost["max_tokens_per_case"] == 300


def test_latency_percentiles_without_data_is_zero():
    assert runner.latency_percentiles([])["count"] == 0


# ---------------------------------------------------------------------------
# feedback suite (W5)
# ---------------------------------------------------------------------------


def _write_feedback(tmp_path, rows):
    path = tmp_path / "regression_feedback.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _feedback_row(**overrides):
    row = {
        "id": "fb_rv_1",
        "type": "feedback",
        "split": "regression",
        "version": "1.0",
        "source_trace_id": "t-1",
        "reviewer": "alice",
        "correct_behaviour": "必须先确认再下单",
        "origin": {"kind": "online_review"},
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_feedback_suite_flags_missing_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATASET_DIR", tmp_path)
    _write_feedback(tmp_path, [_feedback_row(reviewer="")])

    suite = await runner.run_feedback_suite(mode="deterministic", repeat=1)

    assert suite["contract_violations"] == 1
    assert suite["regressions"] == 1
    assert suite["cases"][0]["gate"] is True
    assert any(c.startswith("L5.feedback_provenance") for c in suite["cases"][0]["failed_checks"])


@pytest.mark.asyncio
async def test_feedback_suite_contract_only_is_not_scored(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATASET_DIR", tmp_path)
    _write_feedback(tmp_path, [_feedback_row()])

    suite = await runner.run_feedback_suite(mode="deterministic", repeat=1)

    assert suite["cases"] == []
    assert suite["not_executed"][0]["reason"] == "contract_only"
    assert suite["coverage"]["contract_only_rows"] == 1
    # A prose-only case must never inflate the pass count.
    assert suite["pass_at_1"] == 0
    assert suite["scored"] == 0


@pytest.mark.asyncio
async def test_feedback_suite_executes_runnable_case(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATASET_DIR", tmp_path)
    _write_feedback(
        tmp_path,
        [_feedback_row(type="agent_planning", input="北京去西安 4 天")],
    )
    monkeypatch.setattr(runner, "candidate_tool_index", _fake_index)

    async def fake_probe(row, **kwargs):
        return ContentOutcome(
            case_id=row["id"],
            dataset="regression_feedback.jsonl",
            split=row["split"],
            executed=True,
            checks=[_check("L1.result_hard_gate", True)],
            metadata={},
        )

    monkeypatch.setattr("evals.content.run_travel_probe", fake_probe)

    suite = await runner.run_feedback_suite(mode="deterministic", repeat=1)

    assert suite["coverage"]["behavioural_rows"] == 1
    assert suite["pass_at_1"] == 1
    assert suite["cases"][0]["reviewer"] == "alice"
    assert suite["cases"][0]["origin"] == {"kind": "online_review"}


@pytest.mark.asyncio
async def test_feedback_suite_empty_dataset_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATASET_DIR", tmp_path)

    suite = await runner.run_feedback_suite(mode="deterministic", repeat=1)

    assert suite["total"] == 0
    assert suite["contract_violations"] == 0
    assert suite["coverage"]["promoted_rows"] == 0


async def _fake_index():
    return {}
