"""W4 · Judge layer: rubric-driven scoring with a mandatory kappa calibration.

Public surface:

- :data:`evals.judge.rubrics.DEFAULT_RUBRIC` / ``RUBRIC_VERSION``
- :class:`evals.judge.base.RuleJudge` / :class:`evals.judge.base.LLMJudge`
- :func:`evals.judge.calibration.calibrate` / :func:`may_use_as_gate`

The invariant to remember: **a judge score is not a gate until ``may_use_as_gate``
returns True** (D09 §5.2, ``kappa ≥ 0.6``).
"""

from evals.judge.base import (
    Judge,
    JudgeCase,
    JudgeVerdict,
    LLMJudge,
    RuleJudge,
    build_judge,
    mean_overall,
)
from evals.judge.calibration import (
    CalibrationReport,
    calibrate,
    cohen_kappa,
    decide_mode,
    load_calibration,
    may_use_as_gate,
    save_calibration,
)
from evals.judge.rubrics import (
    DEFAULT_RUBRIC,
    JUDGE_PROMPT,
    KAPPA_THRESHOLD,
    RUBRIC_VERSION,
    Rubric,
    build_judge_prompt,
)

__all__ = [
    "Judge",
    "JudgeCase",
    "JudgeVerdict",
    "RuleJudge",
    "LLMJudge",
    "build_judge",
    "mean_overall",
    "DEFAULT_RUBRIC",
    "RUBRIC_VERSION",
    "JUDGE_PROMPT",
    "KAPPA_THRESHOLD",
    "Rubric",
    "build_judge_prompt",
    "CalibrationReport",
    "calibrate",
    "cohen_kappa",
    "decide_mode",
    "save_calibration",
    "load_calibration",
    "may_use_as_gate",
]
