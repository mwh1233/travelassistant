"""Redaction: identifiers stripped, opaque ids hashed, nothing silently kept."""

from __future__ import annotations

from app.observability.redaction import (
    REDACTION_VERSION,
    RedactionReport,
    redact,
    redact_record,
    redact_text,
)


def test_redacts_email():
    assert "alice@example.com" not in redact_text("联系 alice@example.com 谢谢")
    assert "[EMAIL]" in redact_text("联系 alice@example.com 谢谢")


def test_redacts_phone():
    text = redact_text("我的手机是 13800138000")

    assert "13800138000" not in text
    assert "[PHONE]" in text


def test_redacts_id_card():
    text = redact_text("身份证 11010119900307123X")

    assert "11010119900307123X" not in text
    assert "[ID]" in text


def test_redacts_bank_card():
    text = redact_text("卡号 6222021234567890")

    assert "6222021234567890" not in text


def test_redacts_api_key_shapes():
    text = redact_text("key sk-abcdefghijklmnop1234")

    assert "sk-abcdefghijklmnop1234" not in text
    assert "[SECRET]" in text


def test_redacts_credential_assignment():
    text = redact_text("token=abcd1234efgh")

    assert "abcd1234efgh" not in text


def test_redacts_url_credentials():
    text = redact_text("https://user:pass@example.com/path")

    assert "user:pass" not in text
    assert "[REDACTED]@" in text


def test_hashes_opaque_ids():
    redacted, report = redact({"user_id": "u-12345", "input": "你好"})

    assert redacted["user_id"].startswith("id:")
    assert redacted["user_id"] != "u-12345"
    assert "u-12345" not in redacted["user_id"]
    assert report.hashed_ids == 1


def test_hash_is_stable_and_non_invertible():
    first, _ = redact({"user_id": "u-1"})
    second, _ = redact({"user_id": "u-1"})

    assert first["user_id"] == second["user_id"]


def test_empty_id_is_left_alone():
    redacted, report = redact({"user_id": "", "session_id": None})

    assert redacted["user_id"] == ""
    assert redacted["session_id"] is None
    assert report.hashed_ids == 0


def test_walks_nested_structures():
    record = {"turns": [{"text": "邮件 a@b.com"}, {"text": "卡片 6222021234567890"}]}

    redacted, report = redact(record)

    assert "a@b.com" not in redacted["turns"][0]["text"]
    assert "6222021234567890" not in redacted["turns"][1]["text"]
    assert report.counts.get("email") == 1


def test_errors_are_scanned_too():
    record = {"errors": ["ValueError: bad input for 13800138000"]}

    redacted, report = redact(record)

    assert "13800138000" not in redacted["errors"][0]
    assert report.counts.get("phone") == 1


def test_report_records_version_and_total():
    _, report = redact({"input": "a@b.com 13800138000"})

    assert report.version == REDACTION_VERSION
    assert report.total >= 2
    assert report.to_dict()["retention_days"] == 30


def test_report_accumulates_across_calls():
    report = RedactionReport()
    redact_record({"input": "a@b.com"}, report)
    redact_record({"input": "c@d.com"}, report)

    assert report.counts.get("email") == 2


def test_passthrough_leaves_non_text_fields_untouched():
    # A numeric field that is not an identifier must not be rewritten.
    redacted, _ = redact({"cost": 1234, "travel_days": 4})

    assert redacted["cost"] == 1234
    assert redacted["travel_days"] == 4


def test_original_record_not_mutated():
    record = {"input": "a@b.com"}
    redact(record)

    assert record["input"] == "a@b.com"
