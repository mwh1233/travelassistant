"""Judge calibration: Cohen's kappa, the gate, and the degrade path.

D09 §5.2 sets a hard bar: **``kappa ≥ 0.6`` before a judge may be used for
automatic scoring**. Below that it is only a coarse screen and the final
decision must fall back to a human. Without this gate a plausible-looking judge
quietly becomes the definition of quality.

The calibration compares a judge's binarised scores against human labels on a
gold set. Kappa (not raw agreement) is the right statistic because agreement
alone is inflated when one class dominates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from evals.judge.base import Judge, JudgeCase
from evals.judge.rubrics import DEFAULT_RUBRIC, KAPPA_COARSE_FLOOR, KAPPA_THRESHOLD

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLD_PATH = PROJECT_ROOT / "evals" / "datasets" / "judge_gold.jsonl"
CALIBRATION_DIR = PROJECT_ROOT / "evals" / "calibrations"

#: Where the calibration verdict lands.
MODE_AUTO_GATE = "auto_gate"
MODE_COARSE_SCREEN = "coarse_screen"
MODE_HUMAN_ONLY = "human_only"


def cohen_kappa(human: list[int], judge: list[int]) -> float:
    """Cohen's kappa for two binary raters.

    Returns 1.0 for perfect agreement, 0.0 when agreement equals chance (or is
    worse). Both lists must be the same length.
    """

    if len(human) != len(judge):
        raise ValueError("human 与 judge 标签长度不一致")
    n = len(human)
    if n == 0:
        return 0.0

    observed = sum(1 for h, j in zip(human, judge) if h == j) / n
    # chance agreement
    h1 = sum(human) / n
    j1 = sum(judge) / n
    expected = h1 * j1 + (1 - h1) * (1 - j1)

    if expected >= 1.0:
        # Both raters constant: perfect agreement is real, disagreement is not.
        return 1.0 if observed >= 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 4)


def decide_mode(kappa: float) -> str:
    """Map a kappa onto the allowed usage (D09 §5.2 degrade path)."""

    if kappa >= KAPPA_THRESHOLD:
        return MODE_AUTO_GATE
    if kappa >= KAPPA_COARSE_FLOOR:
        return MODE_COARSE_SCREEN
    return MODE_HUMAN_ONLY


@dataclass
class CalibrationReport:
    judge_version: str
    rubric_version: str
    dimension: str
    sample_size: int
    kappa: float
    agreement: float
    mode: str
    threshold: float = KAPPA_THRESHOLD
    confusion: dict[str, int] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passes_gate(self) -> bool:
        return self.kappa >= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_version": self.judge_version,
            "rubric_version": self.rubric_version,
            "dimension": self.dimension,
            "sample_size": self.sample_size,
            "kappa": self.kappa,
            "agreement": self.agreement,
            "threshold": self.threshold,
            "passes_gate": self.passes_gate,
            "mode": self.mode,
            "confusion": self.confusion,
            "disagreements": self.disagreements,
        }

    def summary(self) -> str:
        verdict = "达标" if self.passes_gate else "未达标"
        return (
            f"[{self.dimension}] kappa={self.kappa:.3f}（阈值 {self.threshold}，{verdict}）"
            f"｜一致率 {self.agreement:.0%}｜n={self.sample_size}｜用法={self.mode}"
        )


def load_gold(path: Path | None = None) -> list[JudgeCase]:
    target = path or GOLD_PATH
    if not target.exists():
        return []
    cases: list[JudgeCase] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cases.append(
                JudgeCase(
                    case_id=str(row.get("id")),
                    question=str(row.get("input") or ""),
                    answer=str(row.get("answer") or ""),
                    context=str(row.get("context") or ""),
                    dimension=str(row.get("dimension") or "faithfulness"),
                    human_score=int(row["human_score"]) if row.get("human_score") is not None else None,
                    human_annotator=str(row.get("human_annotator") or ""),
                )
            )
    return cases


def calibrate(
    judge: Judge,
    cases: Iterable[JudgeCase],
    *,
    dimension: str = "faithfulness",
    threshold: float = 0.5,
) -> CalibrationReport:
    """Binarise judge scores at ``threshold`` and compare against human labels."""

    human: list[int] = []
    machine: list[int] = []
    disagreements: list[dict[str, Any]] = []

    for case in cases:
        if case.human_score is None:
            continue
        verdict = judge.judge(case)
        score = float(getattr(verdict, dimension, verdict.overall))
        predicted = 1 if score >= threshold else 0
        human.append(int(case.human_score))
        machine.append(predicted)
        if predicted != int(case.human_score):
            disagreements.append(
                {
                    "case_id": case.case_id,
                    "human": int(case.human_score),
                    "judge": predicted,
                    "judge_score": round(score, 4),
                    "evidence": verdict.evidence[:120],
                }
            )

    kappa = cohen_kappa(human, machine) if human else 0.0
    agreement = (
        round(sum(1 for h, j in zip(human, machine) if h == j) / len(human), 4) if human else 0.0
    )
    confusion = {
        "human_pos_judge_pos": sum(1 for h, j in zip(human, machine) if h == 1 and j == 1),
        "human_pos_judge_neg": sum(1 for h, j in zip(human, machine) if h == 1 and j == 0),
        "human_neg_judge_pos": sum(1 for h, j in zip(human, machine) if h == 0 and j == 1),
        "human_neg_judge_neg": sum(1 for h, j in zip(human, machine) if h == 0 and j == 0),
    }

    return CalibrationReport(
        judge_version=getattr(judge, "version", "unknown"),
        rubric_version=getattr(getattr(judge, "rubric", DEFAULT_RUBRIC), "version", "unknown"),
        dimension=dimension,
        sample_size=len(human),
        kappa=kappa,
        agreement=agreement,
        mode=decide_mode(kappa),
        threshold=threshold,
        confusion=confusion,
        disagreements=disagreements[:20],
    )


def save_calibration(report: CalibrationReport, directory: Path | None = None) -> Path:
    target_dir = directory or CALIBRATION_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{report.judge_version}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_calibration(judge_version: str, directory: Path | None = None) -> dict[str, Any] | None:
    path = (directory or CALIBRATION_DIR) / f"{judge_version}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def may_use_as_gate(judge_version: str, directory: Path | None = None) -> bool:
    """The single question a caller should ask before trusting a judge score."""

    report = load_calibration(judge_version, directory)
    return bool(report and report.get("passes_gate"))
