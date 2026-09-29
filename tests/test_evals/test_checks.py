"""Pytest wrapper around the L0 deterministic assertion set.

Every check in ``evals.checks`` is parametrised as its own test so a failure
points at a single axis instead of an opaque aggregate.
"""

import json

import pytest

from evals import checks
from app.mcp_core.registry import (
    INTERNAL_TOOL_CAPABILITIES,
    KNOWN_TOOL_VOCABULARY,
    resolve_tool_id,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "check_func",
    checks.CHECK_FUNCTIONS,
    ids=[func.__name__ for func in checks.CHECK_FUNCTIONS],
)
async def test_l0_check(check_func):
    result = await check_func()

    assert result.passed, f"{result.check_id} failed: {result.detail}"


def test_capability_vocabulary_is_unified():
    """Phase 2 closed the P0-2 gap: the datasets must stay inside the registry."""

    result = checks.check_capability_vocabulary()

    assert result.passed, result.detail
    assert result.known_gap is False
    assert result.data["checked_values"] > 500, "词表检查似乎没有真的读到数据集"


def test_capability_vocabulary_catches_hallucinated_values(tmp_path):
    """Negative control: a made-up capability must fail the gate.

    Without this, ``check_capability_vocabulary`` could pass vacuously (e.g. by
    iterating zero files) and nobody would notice.
    """

    (tmp_path / "bogus.jsonl").write_text(
        json.dumps(
            {"id": "bogus_001", "expected_tools": ["pay.now", "internal.step.rollback"]},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = checks.check_capability_vocabulary(dataset_dir=tmp_path)

    assert not result.passed
    assert "pay.now" in result.detail


def test_capability_vocabulary_accepts_resolvable_tool_names(tmp_path):
    """A concrete tool name is legal as long as it resolves to a real id."""

    (tmp_path / "ok.jsonl").write_text(
        json.dumps({"id": "ok_001", "expected_tools": ["maps_direction_driving"]}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    result = checks.check_capability_vocabulary(dataset_dir=tmp_path)

    assert result.passed, result.detail


def test_dataset_schema_is_clean():
    result = checks.check_dataset_schema()

    assert result.passed, result.detail
    # Tripwire: 120 travel + 60 mcp + 60 rag + 10 trajectory + 8 multiturn.
    # Bump deliberately when a dataset is genuinely extended.
    assert sum(info["rows"] for info in result.data["files"].values()) == 258


def test_dataset_schema_accepts_turn_based_scenarios(tmp_path):
    """A multi-turn row carries ``turns`` instead of a flat ``input``."""

    row = {
        "id": "mt_ok",
        "type": "multiturn",
        "split": "regression",
        "version": checks.DATASET_VERSION,
        "initial_state": {"current_step": "requirement_collection"},
        "metadata": {"difficulty": "medium", "multi_turn": True},
        "turns": [{"input": "第一轮"}, {"input": "第二轮"}],
    }
    (tmp_path / "mt.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = checks.check_dataset_schema(dataset_dir=tmp_path)

    assert result.passed, result.detail


def test_dataset_schema_rejects_empty_turn_input(tmp_path):
    """Negative control: a turn with no input must be rejected."""

    row = {
        "id": "mt_bad",
        "type": "multiturn",
        "split": "regression",
        "version": checks.DATASET_VERSION,
        "initial_state": {"current_step": "requirement_collection"},
        "metadata": {"difficulty": "medium", "multi_turn": True},
        "turns": [{"input": "第一轮"}, {"input": ""}],
    }
    (tmp_path / "mt.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = checks.check_dataset_schema(dataset_dir=tmp_path)

    assert not result.passed
    assert "turns[1] input 为空" in result.detail


def test_dataset_schema_catches_missing_fields(tmp_path):
    """Negative control: a row without split/version/metadata must be rejected."""

    (tmp_path / "bogus.jsonl").write_text(
        json.dumps({"id": "bogus_001", "type": "x", "input": "hi"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = checks.check_dataset_schema(dataset_dir=tmp_path)

    assert not result.passed
    assert "split" in result.detail


def test_dataset_schema_rejects_unflagged_illegal_step(tmp_path):
    """An illegal starting step is only legal when explicitly flagged."""

    row = {
        "id": "bogus_001",
        "type": "x",
        "split": "regression",
        "version": checks.DATASET_VERSION,
        "input": "hi",
        "initial_state": {"current_step": "not_a_step"},
        "metadata": {"difficulty": "easy", "multi_turn": False},
    }
    (tmp_path / "bogus.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    assert not checks.check_dataset_schema(dataset_dir=tmp_path).passed

    row["metadata"]["deliberately_illegal_initial_step"] = True
    (tmp_path / "bogus.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    assert checks.check_dataset_schema(dataset_dir=tmp_path).passed


def test_feedback_schema_absent_is_not_a_failure(tmp_path):
    """No promoted cases yet is a valid state, not a broken dataset."""

    result = checks.check_feedback_schema(dataset_dir=tmp_path)

    assert result.passed
    assert result.data["rows"] == 0


def test_feedback_schema_accepts_promoted_row(tmp_path):
    row = {
        "id": "fb_rv_1",
        "type": "feedback",
        "split": "regression",
        "version": "1.0",
        "source_trace_id": "t-1",
        "reviewer": "alice",
        "correct_behaviour": "必须先确认再下单",
        "origin": {"kind": "online_review", "item_id": "rv_1"},
    }
    (tmp_path / "regression_feedback.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = checks.check_feedback_schema(dataset_dir=tmp_path)

    assert result.passed, result.detail


def test_feedback_schema_catches_missing_reviewer(tmp_path):
    """Negative control: a promoted row with no reviewer is rejected."""

    row = {
        "id": "fb_rv_1",
        "type": "feedback",
        "split": "regression",
        "version": "1.0",
        "source_trace_id": "t-1",
        "correct_behaviour": "必须先确认",
        "origin": {"kind": "online_review"},
    }
    (tmp_path / "regression_feedback.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = checks.check_feedback_schema(dataset_dir=tmp_path)

    assert not result.passed
    assert "reviewer" in result.detail


def test_feedback_schema_rejects_wrong_split(tmp_path):
    row = {
        "id": "fb_rv_1",
        "type": "feedback",
        "split": "smoke",
        "version": "1.0",
        "source_trace_id": "t-1",
        "reviewer": "alice",
        "correct_behaviour": "x",
        "origin": {"kind": "online_review"},
    }
    (tmp_path / "regression_feedback.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    assert not checks.check_feedback_schema(dataset_dir=tmp_path).passed


def test_feedback_dataset_is_not_treated_as_a_scenario(tmp_path):
    """Promoted rows must not be fed to the scenario schema check."""

    row = {
        "id": "fb_rv_1",
        "type": "feedback",
        "split": "regression",
        "version": "1.0",
        "source_trace_id": "t-1",
        "reviewer": "alice",
        "correct_behaviour": "x",
        "origin": {"kind": "online_review"},
    }
    (tmp_path / "regression_feedback.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    assert checks.check_dataset_schema(dataset_dir=tmp_path).passed


def test_tool_vocabulary_has_no_unknown_entries():
    """The vocabulary itself must contain no ``unknown.*`` placeholder."""

    assert KNOWN_TOOL_VOCABULARY
    assert not [value for value in KNOWN_TOOL_VOCABULARY if value.startswith("unknown.")]


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("select_transport_tool", "internal.transport.select"),
        ("go_back_to_food", "internal.step.rollback"),
        ("maps_around_search", "map.poi"),
        ("maps_direction_walking", "map.route"),
        ("query_transport_options", "internal.transport.query"),
    ],
)
def test_resolve_tool_id(tool_name, expected):
    assert resolve_tool_id(tool_name) == expected


def test_internal_tool_ids_are_all_namespaced():
    assert all(value.startswith("internal.") for value in INTERNAL_TOOL_CAPABILITIES.values())
