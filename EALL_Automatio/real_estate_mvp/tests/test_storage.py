import json

import pytest

from real_estate_mvp.storage import JSONLStore, StorageError


def test_create_retrieve_update_and_event_log(tmp_path):
    store = JSONLStore(tmp_path)
    inquiry = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="Synthetic test question",
        received_at="2026-10-02T12:30:00+03:00",
    )

    assert inquiry["inquiry_id"] == "inq_000001"
    assert store.get_inquiry("inq_000001")["telegram_user_id"] == "123"
    updated = store.update_inquiry("inq_000001", status="awaiting_review", draft="Draft")
    assert updated["draft"] == "Draft"
    assert store.get_pending_inquiries()[0]["status"] == "awaiting_review"

    event = store.append_event("DRAFT_CREATED", "inq_000001", model="mock")
    assert event["event"] == "DRAFT_CREATED"
    assert store.list_events()[0]["inquiry_id"] == "inq_000001"


def test_state_survives_new_store_instance(tmp_path):
    first = JSONLStore(tmp_path)
    first.create_inquiry(
        telegram_user_id=7,
        telegram_message_id=8,
        message="Persisted message",
    )
    recovered = JSONLStore(tmp_path)

    assert recovered.get_inquiry("inq_000001")["message"] == "Persisted message"


def test_duplicate_id_and_invalid_state_fail_safely(tmp_path):
    store = JSONLStore(tmp_path)
    store.create_inquiry(telegram_user_id=1, telegram_message_id=1, message="Hi")

    with pytest.raises(StorageError, match="already exists"):
        store.create_inquiry(
            telegram_user_id=1, telegram_message_id=1, message="Again",
            inquiry_id="inq_000001",
        )
    with pytest.raises(StorageError, match="Invalid inquiry status"):
        store.update_inquiry("inq_000001", status="unknown")


def test_malformed_jsonl_reports_file_and_line(tmp_path):
    store = JSONLStore(tmp_path)
    store.inquiries_path.write_text('{"inquiry_id": "ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(StorageError, match=r"inquiries.jsonl at line 2"):
        store.list_inquiries()


def test_jsonl_records_are_valid_json(tmp_path):
    store = JSONLStore(tmp_path)
    store.create_inquiry(telegram_user_id=None, telegram_message_id=None, message="Hello")
    record = json.loads(store.inquiries_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["status"] == "received"