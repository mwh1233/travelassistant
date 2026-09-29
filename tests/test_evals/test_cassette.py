"""Cassettes: the storage that makes Replay mode trustworthy."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.cassette import (
    Cassette,
    CassetteMiss,
    args_hash,
    entry_key,
    load_cassette,
)


def test_args_hash_is_order_independent():
    a = args_hash({"city": "西安", "days": 4})
    b = args_hash({"days": 4, "city": "西安"})

    assert a == b
    assert len(a) == 12


def test_args_hash_distinguishes_values():
    assert args_hash({"city": "西安"}) != args_hash({"city": "成都"})


def test_entry_key_format():
    key = entry_key("query_transport_options", {"origin_city": "北京"})

    assert key.startswith("query_transport_options|")


def test_record_then_lookup_hits(tmp_path: Path):
    cassette = Cassette(case_id="c1", mode="record", directory=tmp_path)
    cassette.record("tool_a", {"x": 1}, "result-a")

    assert cassette.lookup("tool_a", {"x": 1}) == "result-a"
    assert cassette.hits == 1


def test_lookup_miss_is_recorded_not_faked(tmp_path: Path):
    cassette = Cassette(case_id="c1", mode="replay", directory=tmp_path)

    assert cassette.lookup("tool_a", {"x": 1}) is None
    assert cassette.misses  # a miss is observable, never silently defaulted


def test_save_and_reload_round_trip(tmp_path: Path):
    writer = Cassette(case_id="c1", mode="record", directory=tmp_path, git_sha="abc123")
    writer.record("tool_a", {"x": 1}, {"nested": ["值"]})
    saved = writer.save()

    assert saved is not None and saved.exists()

    reader = load_cassette("c1", mode="replay", directory=tmp_path)
    assert reader.lookup("tool_a", {"x": 1}) == {"nested": ["值"]}
    assert reader.entries


def test_off_mode_neither_reads_nor_writes(tmp_path: Path):
    cassette = Cassette(case_id="c1", mode="off", directory=tmp_path)
    cassette.record("tool_a", {"x": 1}, "y")

    assert cassette.entries == {}
    assert cassette.save() is None


def test_summary_reports_staleness(tmp_path: Path):
    cassette = Cassette(case_id="c1", mode="replay", directory=tmp_path)
    cassette.lookup("missing_tool", {})

    summary = cassette.summary()

    assert summary["mode"] == "replay"
    assert summary["misses"] == 1
    assert summary["miss_sample"]


def test_cassette_miss_is_a_runtime_error():
    # The harness raises this; it must remain an exception, not a return value.
    assert issubclass(CassetteMiss, RuntimeError)
    with pytest.raises(CassetteMiss):
        raise CassetteMiss("stale")
