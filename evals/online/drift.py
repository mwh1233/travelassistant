"""Drift checks for the online classifier and the label mix.

Two questions worth asking continuously (D09 §4.3 / §6):

1. **Is the classifier still doing what it did?** A rule table or a small model
   silently stops matching after a wording shift. ``label_distribution`` +
   ``distribution_drift`` catch that.
2. **Did the population of failures change shape?** Even with a stable
   classifier, a spike in one label means something new is happening in
   production, which is exactly the signal worth feeding back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

DRIFT_VERSION = "1.0"


def label_distribution(labels: Iterable[str]) -> dict[str, float]:
    """Normalised frequency of each label."""

    counts: dict[str, int] = {}
    total = 0
    for label in labels:
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
        total += 1
    if not total:
        return {}
    return {label: round(count / total, 6) for label, count in sorted(counts.items())}


def total_variation_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """L1/2 distance between two distributions; 0 = identical, 1 = disjoint."""

    keys = set(a) | set(b)
    return round(sum(abs(a.get(key, 0.0) - b.get(key, 0.0)) for key in keys) / 2.0, 6)


@dataclass
class DriftReport:
    metric: str
    value: float
    threshold: float
    drifted: bool
    baseline: dict[str, float] = field(default_factory=dict)
    current: dict[str, float] = field(default_factory=dict)
    version: str = DRIFT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "drifted": self.drifted,
            "baseline": self.baseline,
            "current": self.current,
        }


def distribution_drift(
    baseline: Iterable[str] | dict[str, float],
    current: Iterable[str] | dict[str, float],
    *,
    threshold: float = 0.2,
) -> DriftReport:
    """Compare two label populations and decide whether the shift is real."""

    base = baseline if isinstance(baseline, dict) else label_distribution(baseline)
    curr = current if isinstance(current, dict) else label_distribution(current)
    distance = total_variation_distance(base, curr)
    return DriftReport(
        metric="total_variation_distance",
        value=distance,
        threshold=threshold,
        drifted=distance > threshold,
        baseline=base,
        current=curr,
    )


def population_stability_index(
    baseline: dict[str, float],
    current: dict[str, float],
    *,
    epsilon: float = 1e-4,
) -> float:
    """PSI over the same label space. >0.2 is the conventional "shifted" bar."""

    keys = set(baseline) | set(current)
    psi = 0.0
    for key in keys:
        expected = max(baseline.get(key, 0.0), epsilon)
        actual = max(current.get(key, 0.0), epsilon)
        psi += (actual - expected) * math.log(actual / expected)
    return round(psi, 6)
