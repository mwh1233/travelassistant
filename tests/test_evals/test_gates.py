"""Release gates: thresholds, slicing, baseline diff and holdout isolation."""

from __future__ import annotations

from pathlib import Path

from evals import gates


def _case(case_id: str, split: str, passed: bool, **extra):
    case = {
        "case_id": case_id,
        "split": split,
        "pass_at_1": passed,
        "pass_pow_k": passed,
        "known_gap": extra.pop("known_gap", False),
    }
    case.update(extra)
    return case


def test_percentile_nearest_rank():
    assert gates.percentile([], 50) == 0.0
    assert gates.percentile([10.0], 95) == 10.0
    assert gates.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert gates.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0


def test_slice_report_by_split_and_metadata():
    cases = [
        _case("a", "regression", True, metadata={"difficulty": "hard"}),
        _case("b", "regression", False, metadata={"difficulty": "hard"}),
        _case("c", "smoke", True, metadata={"difficulty": "easy"}),
    ]

    by_split = gates.slice_report(cases, "split")
    assert by_split["regression"]["success_rate"] == 0.5
    assert by_split["smoke"]["success_rate"] == 1.0

    by_difficulty = gates.slice_report(cases, "metadata.difficulty")
    assert by_difficulty["hard"]["total"] == 2


def test_success_gate_flags_a_split_below_threshold():
    cases = [_case(f"r{i}", "regression", i < 5) for i in range(10)]  # 50%

    outcomes = gates.evaluate_gates(cases, thresholds=gates.Thresholds())
    by_id = {outcome.gate_id: outcome for outcome in outcomes}

    assert not by_id["G1.success_rate"].passed


def test_safety_gate_is_zero_tolerance():
    cases = [_case("s1", "safety", True), _case("s2", "safety", False)]

    outcomes = gates.evaluate_gates(cases, thresholds=gates.Thresholds())
    by_id = {outcome.gate_id: outcome for outcome in outcomes}

    assert not by_id["G2.safety_zero_tolerance"].passed
    assert "s2" in by_id["G2.safety_zero_tolerance"].detail


def test_safety_known_gap_does_not_break_the_gate():
    cases = [_case("s1", "safety", False, known_gap=True)]

    outcomes = gates.evaluate_gates(cases, thresholds=gates.Thresholds())
    by_id = {outcome.gate_id: outcome for outcome in outcomes}

    assert by_id["G2.safety_zero_tolerance"].passed


def _gate(outcomes, gate_id):
    return next(outcome for outcome in outcomes if outcome.gate_id == gate_id)


def test_cost_budget_scales_with_turns():
    thresholds = gates.Thresholds()
    single = _case("x", "regression", True, cost={"llm_calls": 5}, metadata={"turns": 1})
    multi = _case("y", "regression", True, cost={"llm_calls": 16}, metadata={"turns": 8})

    assert gates.call_budget(single, thresholds) == thresholds.max_llm_calls_per_case
    assert gates.call_budget(multi, thresholds) == 8 * thresholds.max_llm_calls_per_turn
    # 16 calls over 8 turns is fine; over 1 turn it is not.
    assert _gate(gates.evaluate_gates([multi]), "G5.cost").passed
    assert not _gate(
        gates.evaluate_gates(
            [_case("z", "regression", True, cost={"llm_calls": 16}, metadata={"turns": 1})]
        ),
        "G5.cost",
    ).passed


def test_stability_gate_only_runs_with_repeat():
    cases = [_case(f"c{i}", "regression", True, pass_pow_k=i % 2 == 0) for i in range(4)]

    without = {o.gate_id for o in gates.evaluate_gates(cases, repeat=1)}
    with_repeat = {o.gate_id for o in gates.evaluate_gates(cases, repeat=2)}

    assert "G3.stability" not in without
    assert "G3.stability" in with_repeat


def test_baseline_round_trip_and_regression(tmp_path: Path):
    suite = "unit"
    summary = {"by_split": {"regression": {"success_rate": 0.9}}}

    gates.save_baseline(suite, summary, baseline_dir=tmp_path, meta={"git_sha": "abc"})
    loaded = gates.load_baseline(suite, baseline_dir=tmp_path)
    assert loaded is not None and loaded["meta"]["git_sha"] == "abc"

    dropped = {"by_split": {"regression": {"success_rate": 0.80}}}  # -10pt
    outcome = gates.compare_against_baseline(dropped, loaded)

    assert not outcome.passed
    assert outcome.data["worst_drop_pt"] == 10.0


def test_baseline_absent_is_not_a_failure():
    outcome = gates.compare_against_baseline({"by_split": {}}, None)

    assert outcome.passed


def test_holdout_violations_detects_leak(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "prompt.py").write_text("example: hh_secret_001", encoding="utf-8")

    violations = gates.holdout_violations(["hh_secret_001"], root=tmp_path)

    assert violations and "hh_secret_001" in violations[0]
    assert gates.holdout_violations([], root=tmp_path) == []
