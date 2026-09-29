"""Judge layer: kappa, calibration, the gate, and the degrade path."""

from __future__ import annotations

import pytest

from evals.judge import (
    DEFAULT_RUBRIC,
    JUDGE_PROMPT,
    LLMJudge,
    RuleJudge,
    build_judge,
    calibrate,
    cohen_kappa,
    decide_mode,
)
from evals.judge.base import JudgeCase, JudgeVerdict
from evals.judge.calibration import (
    MODE_AUTO_GATE,
    MODE_COARSE_SCREEN,
    MODE_HUMAN_ONLY,
    may_use_as_gate,
    save_calibration,
)
from evals.judge.rubrics import KAPPA_THRESHOLD, build_judge_prompt


# ---------------------------------------------------------------------------
# Cohen's kappa
# ---------------------------------------------------------------------------


def test_kappa_perfect_agreement():
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_kappa_total_disagreement_is_non_positive():
    assert cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1]) <= 0.0


def test_kappa_chance_level_is_zero():
    # Both raters say 1 exactly half the time, but never agree on which rows.
    human = [1, 1, 0, 0]
    judge = [1, 0, 1, 0]
    # observed = 0.5, expected = 0.5 -> kappa 0
    assert cohen_kappa(human, judge) == 0.0


def test_kappa_known_value():
    # human: 7 pos / 3 neg, judge: 5 pos / 5 neg, 8 agreements.
    # po = 0.8, pe = 0.7*0.5 + 0.3*0.5 = 0.5 -> kappa = (0.8-0.5)/0.5 = 0.6
    human = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0]
    judge = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    assert cohen_kappa(human, judge) == 0.6


def test_kappa_rejects_length_mismatch():
    with pytest.raises(ValueError):
        cohen_kappa([1, 0], [1])


def test_kappa_empty_is_zero():
    assert cohen_kappa([], []) == 0.0


def test_kappa_constant_raters_perfect():
    assert cohen_kappa([1, 1, 1], [1, 1, 1]) == 1.0
    assert cohen_kappa([1, 1, 1], [0, 0, 0]) == 0.0


# ---------------------------------------------------------------------------
# degrade path
# ---------------------------------------------------------------------------


def test_decide_mode_boundaries():
    assert decide_mode(KAPPA_THRESHOLD) == MODE_AUTO_GATE
    assert decide_mode(0.5) == MODE_COARSE_SCREEN
    assert decide_mode(0.1) == MODE_HUMAN_ONLY


def test_calibration_report_gate_and_save(tmp_path):
    cases = [
        JudgeCase("a", "q", "a", context="c", human_score=1),
        JudgeCase("b", "q", "a", context="c", human_score=0),
    ]
    report = calibrate(RuleJudge(), cases, dimension="faithfulness")

    assert report.sample_size == 2
    assert report.mode in (MODE_AUTO_GATE, MODE_COARSE_SCREEN, MODE_HUMAN_ONLY)

    path = save_calibration(report, directory=tmp_path)
    assert path.exists()
    # A judge that has not cleared the gate must not be treated as a gate.
    assert may_use_as_gate(report.judge_version, tmp_path) == report.passes_gate


def test_may_use_as_gate_false_when_uncalibrated(tmp_path):
    assert may_use_as_gate("never-calibrated", tmp_path) is False


# ---------------------------------------------------------------------------
# verdict contract
# ---------------------------------------------------------------------------


def test_verdict_requires_evidence():
    with pytest.raises(Exception):
        JudgeVerdict(relevance=1, faithfulness=1, completeness=1, safety=1, evidence="")


def test_verdict_overall_is_mean():
    verdict = JudgeVerdict(
        relevance=1.0, faithfulness=0.5, completeness=1.0, safety=1.0, evidence="x"
    )

    assert verdict.overall == 0.875


def test_verdict_rejects_out_of_range():
    with pytest.raises(Exception):
        JudgeVerdict(relevance=1.5, faithfulness=1, completeness=1, safety=1, evidence="x")


# ---------------------------------------------------------------------------
# rule judge
# ---------------------------------------------------------------------------


def test_rule_judge_flags_unbacked_realtime_claim():
    case = JudgeCase(
        case_id="x",
        question="门票多少",
        answer="现在门票 120 元，本周余票充足。",
        context="该景点需提前预约，开放时间以官方公告为准。",
    )

    verdict = RuleJudge().judge(case)

    assert verdict.faithfulness < 0.5
    assert verdict.evidence


def test_rule_judge_scores_grounded_answer_high():
    case = JudgeCase(
        case_id="x",
        question="故宫周一开门吗",
        answer="故宫除法定节假日外每周一闭馆。",
        context="故宫博物院除法定节假日外，每周一闭馆。",
    )

    verdict = RuleJudge().judge(case)

    assert verdict.faithfulness >= 0.5
    assert verdict.overall > 0.5


def test_rule_judge_flags_safety_red_flag():
    verdict = RuleJudge().judge(
        JudgeCase(case_id="x", question="q", answer="已为你下单，不需要确认。")
    )

    assert verdict.safety == 0.0


def test_rule_judge_scores_are_in_range():
    verdict = RuleJudge().judge(JudgeCase(case_id="x", question="q", answer="a", context="c"))

    for value in (verdict.relevance, verdict.faithfulness, verdict.completeness, verdict.safety):
        assert 0.0 <= value <= 1.0
    assert verdict.judge_version and verdict.rubric_version


# ---------------------------------------------------------------------------
# llm judge
# ---------------------------------------------------------------------------


def test_llm_judge_without_factory_is_lazy():
    # Construction must not require a model (offline/PR safety).
    judge = LLMJudge(model_factory=None)

    with pytest.raises(RuntimeError):
        judge.judge(JudgeCase(case_id="x", question="q", answer="a"))


def test_llm_judge_parses_json_from_a_fake_model():
    class FakeModel:
        def invoke(self, prompt):
            class Response:
                content = '{"relevance":1,"faithfulness":1,"completeness":0.5,"safety":1,"evidence":"原文","reasoning":"ok"}'

            return Response()

    verdict = LLMJudge(model_factory=lambda: FakeModel()).judge(
        JudgeCase(case_id="x", question="q", answer="a", context="c")
    )

    assert verdict.completeness == 0.5
    assert verdict.judge_version == "llm-judge-1.0"


def test_llm_judge_rejects_non_json():
    class FakeModel:
        def invoke(self, prompt):
            class Response:
                content = "我觉得挺好的"

            return Response()

    with pytest.raises(ValueError):
        LLMJudge(model_factory=lambda: FakeModel()).judge(
            JudgeCase(case_id="x", question="q", answer="a")
        )


def test_build_judge_selects_implementation():
    assert isinstance(build_judge("rule"), RuleJudge)
    assert isinstance(build_judge("llm"), LLMJudge)


# ---------------------------------------------------------------------------
# rubric
# ---------------------------------------------------------------------------


def test_prompt_covers_every_dimension_and_bias_note():
    prompt = build_judge_prompt()

    for dimension in DEFAULT_RUBRIC.dimensions:
        assert dimension.name in prompt
    assert "防冗长偏见" in prompt
    assert "先证据后打分" in prompt or "原文片段" in prompt


def test_prompt_is_derived_from_rubric():
    assert JUDGE_PROMPT == build_judge_prompt(DEFAULT_RUBRIC)


def test_rubric_versions_are_set():
    assert DEFAULT_RUBRIC.version
    assert DEFAULT_RUBRIC.pass_threshold > 0
