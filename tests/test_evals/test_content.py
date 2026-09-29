"""Content executors: requirement extraction, offline retrieval, gap semantics."""

from __future__ import annotations

import pytest

from evals.content import (
    CONTENT_KNOWN_GAPS,
    ContentOutcome,
    LocalBm25Index,
    Requirement,
    _result,
    extract_requirement,
    run_mcp_probe,
    run_rag_probe,
)


def _check(check_id, passed, known_gap=False):
    return _result(
        check_id, "t", passed, known_gap=known_gap, known_gap_ref="ref"
    )


# ---------------------------------------------------------------------------
# requirement extraction
# ---------------------------------------------------------------------------


def test_extract_requirement_reads_origin_and_destination():
    req = extract_requirement({"input": "一家三口从北京出发，4 天去西安，想要文化体验"})

    assert isinstance(req, Requirement)
    assert req.values["departure_city"] == "北京"
    assert req.values["destination"] == "西安"
    assert req.values["travel_days"] == 4
    assert (req.values["adult_count"], req.values["children_count"]) == (2, 1)
    assert req.values["travel_styles"] == ["culture"]


def test_extract_requirement_marks_inferred_fields():
    req = extract_requirement({"input": "去成都玩几天"})

    assert req.values["destination"] == "成都"
    # Origin is not stated, so it must be recorded as inferred, never silent.
    assert "departure_city" in req.inferred


def test_extract_requirement_reports_missing_destination():
    req = extract_requirement({"input": "帮我规划一次旅行"})

    assert "destination" in req.missing
    assert not req.complete


def test_same_city_trip_is_inferred_not_missing():
    req = extract_requirement({"input": "北京亲子研学 5 天"})

    assert "same_city_trip" in req.inferred
    assert "destination" not in req.missing


def test_special_needs_is_a_string_never_none():
    # record_requirement_tool declares special_needs: str = ""; None fails pydantic.
    req = extract_requirement({"input": "从北京去上海 3 天"})

    assert isinstance(req.values["special_needs"], str)


def test_styles_fall_back_to_a_legal_enum():
    req = extract_requirement({"input": "从北京去上海 3 天"})

    assert req.values["travel_styles"] == ["culture"]


# ---------------------------------------------------------------------------
# offline retrieval
# ---------------------------------------------------------------------------


def test_bm25_index_ranks_relevant_document_first():
    docs = [("xian.md", "西安 兵马俑 肉夹馍 城墙"), ("sanya.md", "三亚 海滩 潜水 椰林")]
    index = LocalBm25Index(docs)

    results = index.search("西安 兵马俑", top_k=1)

    assert results and results[0]["source"] == "xian.md"


def test_bm25_index_on_empty_corpus_is_safe():
    index = LocalBm25Index([])

    assert index.doc_count == 0
    assert index.search("anything") == []


# ---------------------------------------------------------------------------
# known-gap semantics
# ---------------------------------------------------------------------------


def test_outcome_passed_is_strict():
    outcome = ContentOutcome(
        case_id="x", dataset="d", split="regression", executed=True,
        checks=[_check("L1.a", True)],
    )

    assert outcome.passed


def test_outcome_known_gap_is_not_a_pass():
    outcome = ContentOutcome(
        case_id="x", dataset="d", split="regression", executed=True,
        checks=[_check("L1.a", True), _check("L1.b", False, known_gap=True)],
    )

    assert not outcome.passed
    assert outcome.known_gap
    assert outcome.hard_failed_checks == []
    assert outcome.failed_checks == ["L1.b"]


def test_outcome_hard_failure_beats_gap():
    outcome = ContentOutcome(
        case_id="x", dataset="d", split="regression", executed=True,
        checks=[_check("L1.b", False, known_gap=True), _check("L1.c", False)],
    )

    assert not outcome.passed
    assert not outcome.known_gap
    assert outcome.hard_failed_checks == ["L1.c"]


def test_not_executed_is_neither_pass_nor_gap():
    outcome = ContentOutcome(
        case_id="x", dataset="d", split="regression", executed=False,
        checks=[_check("L1.requirement_completeness", False)],
    )

    assert not outcome.passed
    assert not outcome.known_gap


# ---------------------------------------------------------------------------
# mcp probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_probe_known_gap_for_unbacked_capability():
    index = {"by_capability": {}, "by_step": {}, "all_names": []}
    row = {
        "id": "m1",
        "input": "订酒店",
        "split": "safety",
        "expected_capabilities": [{"capability": "hotel.search", "required": True}],
        "forbidden_capabilities": ["hotel.purchase"],
    }

    outcome = await run_mcp_probe(row, index=index)

    # hotel.search is a legal vocabulary entry with no loaded tool -> build gap.
    assert outcome.known_gap
    assert not outcome.hard_failed_checks


@pytest.mark.asyncio
async def test_mcp_probe_hard_failure_for_unknown_capability():
    index = {"by_capability": {}, "by_step": {}, "all_names": []}
    row = {
        "id": "m2",
        "input": "x",
        "split": "regression",
        "expected_capabilities": [{"capability": "quantum.teleport", "required": True}],
    }

    outcome = await run_mcp_probe(row, index=index)

    assert "L1.capability_available" in outcome.hard_failed_checks


@pytest.mark.asyncio
async def test_mcp_probe_available_capability_passes():
    index = {"by_capability": {"hotel.search": [("t", "find-hotels")]}, "by_step": {}, "all_names": []}
    row = {
        "id": "m3",
        "input": "x",
        "split": "regression",
        "expected_capabilities": [{"capability": "hotel.search", "required": True}],
    }

    outcome = await run_mcp_probe(row, index=index)

    assert outcome.passed


# ---------------------------------------------------------------------------
# rag probe
# ---------------------------------------------------------------------------


def test_rag_probe_corpus_coverage_miss_is_a_gap():
    index = LocalBm25Index([("xian.md", "西安 兵马俑")])
    row = {
        "id": "r1",
        "input": "三亚潜水",
        "split": "regression",
        "expected_entities": ["三亚海底世界"],
        "requires_sources": True,
    }

    outcome = run_rag_probe(row, index=index)

    assert outcome.known_gap
    assert not outcome.hard_failed_checks


def test_rag_probe_retrieval_miss_on_covered_entity_is_a_hard_failure():
    # The entity IS in the corpus (doc b), so a top-1 miss is a retrieval
    # problem, not a coverage gap.
    index = LocalBm25Index(
        [("a.md", "西安 兵马俑 城墙 肉夹馍"), ("b.md", "三亚 海滩 奇怪实体名") ]
    )
    row = {
        "id": "r2",
        "input": "西安 兵马俑",
        "split": "regression",
        "expected_entities": ["奇怪实体名"],
    }

    outcome = run_rag_probe(row, index=index, top_k=1)

    assert "L1.rag_entity_recall" in outcome.hard_failed_checks


def test_rag_probe_fallback_expected_passes_on_miss():
    index = LocalBm25Index([])
    row = {
        "id": "r3",
        "input": "冷门问题",
        "split": "regression",
        "expected_entities": ["不存在"],
        "fallback_expected": True,
    }

    outcome = run_rag_probe(row, index=index)

    assert outcome.passed


def test_every_known_gap_table_entry_has_a_reason_and_ref():
    for check_id, (reason, ref) in CONTENT_KNOWN_GAPS.items():
        assert reason and ref, check_id
