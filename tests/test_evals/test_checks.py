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
    assert sum(info["rows"] for info in result.data["files"].values()) == 250


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
