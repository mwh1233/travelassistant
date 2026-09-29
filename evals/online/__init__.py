"""W5 · Online feedback loop.

The chain, end to end::

    线上 trace
      → 脱敏 (app.observability.redaction)
      → 抽样 (sampling.SamplingPolicy)
      → 逐轮轻量分类 (evals.turns.classify_conversation)
      → 低置信 / 高风险进人工复核队列 (queue.ReviewQueue)
      → 人工确认「正确行为应该是什么」
      → 晋升为版本化数据集用例 (scripts/promote_reviewed.py)
      → 跑回归 → CI 抓住

The load-bearing rule: **an item cannot be approved — and therefore cannot be
promoted — without a reviewer and a stated correct behaviour.** That is the
"人工复核不可跳过" requirement from D09 §3, enforced in code rather than in a
process document.
"""

from evals.online.drift import (
    DRIFT_VERSION,
    DriftReport,
    distribution_drift,
    label_distribution,
    population_stability_index,
    total_variation_distance,
)
from evals.online.queue import (
    REVIEW_QUEUE_VERSION,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewError,
    ReviewItem,
    ReviewQueue,
    build_review_item,
)
from evals.online.promote import (
    FEEDBACK_DATASET,
    FEEDBACK_SPLIT,
    PROMOTE_VERSION,
    PromotionError,
    build_feedback_case,
    contract_failures,
    load_feedback_cases,
    promote_queue,
)
from evals.online.sampling import (
    DEFAULT_SALT,
    SAMPLING_VERSION,
    SamplingPolicy,
    sample_decision,
    sample_records,
)

__all__ = [
    "ReviewQueue",
    "ReviewItem",
    "ReviewError",
    "build_review_item",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "REVIEW_QUEUE_VERSION",
    "build_feedback_case",
    "contract_failures",
    "load_feedback_cases",
    "promote_queue",
    "PromotionError",
    "FEEDBACK_DATASET",
    "FEEDBACK_SPLIT",
    "PROMOTE_VERSION",
    "SamplingPolicy",
    "sample_decision",
    "sample_records",
    "SAMPLING_VERSION",
    "DEFAULT_SALT",
    "label_distribution",
    "distribution_drift",
    "total_variation_distance",
    "population_stability_index",
    "DriftReport",
    "DRIFT_VERSION",
]
