"""Telegram inbound deduplication regression tests."""

from pathlib import Path

import pytest

from real_estate_mvp.bot import prepare_inquiry
from real_estate_mvp.config import Settings
from real_estate_mvp.storage import DuplicateInquiryError, JSONLStore


def mock_settings(data_dir) -> Settings:
    return Settings(
        telegram_bot_token="",
        founder_chat_id="",
        gemini_api_key="",
        mock_mode=True,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=data_dir,
        properties_file=Path(__file__).parents[1] / "data" / "properties.json",
    )


def test_first_delivery_creates_inquiry(tmp_path):
    store = JSONLStore(tmp_path)
    inquiry = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="How much is the Bole 2 bedroom apartment?",
        telegram_chat_id=123,
    )
    assert inquiry["inquiry_id"] == "inq_000001"
    assert store.get_inquiry("inq_000001") is not None


def test_duplicate_delivery_is_rejected_with_existing_record(tmp_path):
    store = JSONLStore(tmp_path)
    original = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="How much is the Bole 2 bedroom apartment?",
        telegram_chat_id=123,
    )
    with pytest.raises(DuplicateInquiryError) as excinfo:
        store.create_inquiry(
            telegram_user_id=123,
            telegram_message_id=42,
            message="How much is the Bole 2 bedroom apartment?",
            telegram_chat_id=123,
        )
    assert excinfo.value.inquiry["inquiry_id"] == original["inquiry_id"]
    assert len(store.list_inquiries()) == 1


def test_duplicate_after_restart_is_detected(tmp_path):
    first = JSONLStore(tmp_path)
    first.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="How much is the Bole 2 bedroom apartment?",
        telegram_chat_id=123,
    )
    # A new store instance simulates a process restart: dedup state is
    # read back from durable storage, not memory.
    restarted = JSONLStore(tmp_path)
    with pytest.raises(DuplicateInquiryError):
        restarted.create_inquiry(
            telegram_user_id=123,
            telegram_message_id=42,
            message="How much is the Bole 2 bedroom apartment?",
            telegram_chat_id=123,
        )


def test_duplicate_while_pending_does_not_redraft(tmp_path):
    import asyncio

    async def scenario() -> None:
        store = JSONLStore(tmp_path)
        settings = mock_settings(tmp_path)
        first = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=123,
            telegram_message_id=42,
            message="How much is the Bole 2 bedroom apartment?",
            telegram_chat_id=123,
        )
        assert first["status"] == "awaiting_review"
        events_before = len(store.list_events())
        redelivered = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=123,
            telegram_message_id=42,
            message="How much is the Bole 2 bedroom apartment?",
            telegram_chat_id=123,
        )
        assert redelivered["inquiry_id"] == first["inquiry_id"]
        # No second record, no second draft, no second review cycle.
        assert len(store.list_inquiries()) == 1
        assert redelivered["draft"] == first["draft"]
        events = [event["event"] for event in store.list_events()]
        assert events.count("INQUIRY_RECEIVED") == 1
        assert events.count("DRAFT_CREATED") == 1
        assert events.count("DUPLICATE_DELIVERY") == 1
        assert len(store.list_events()) == events_before + 1

    asyncio.run(scenario())


def test_distinct_messages_from_same_buyer_are_not_duplicates(tmp_path):
    import asyncio

    async def scenario() -> None:
        store = JSONLStore(tmp_path)
        settings = mock_settings(tmp_path)
        first = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=123,
            telegram_message_id=42,
            message="How much is the Bole 2 bedroom apartment?",
            telegram_chat_id=123,
        )
        second = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=123,
            telegram_message_id=43,
            message="Where is the Summit villa?",
            telegram_chat_id=123,
        )
        assert second["inquiry_id"] != first["inquiry_id"]
        assert len(store.list_inquiries()) == 2

    asyncio.run(scenario())


def test_same_message_id_in_different_chats_is_not_a_duplicate(tmp_path):
    store = JSONLStore(tmp_path)
    store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="Message in chat one",
        telegram_chat_id=111,
    )
    second = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=42,
        message="Message in chat two",
        telegram_chat_id=222,
    )
    assert second["inquiry_id"] == "inq_000002"


def test_anonymous_records_are_not_deduplicated(tmp_path):
    store = JSONLStore(tmp_path)
    store.create_inquiry(telegram_user_id=None, telegram_message_id=None, message="Hi")
    store.create_inquiry(telegram_user_id=None, telegram_message_id=None, message="Hi")
    assert len(store.list_inquiries()) == 2


def test_legacy_records_dedup_on_user_id(tmp_path):
    store = JSONLStore(tmp_path)
    store.create_inquiry(
        telegram_user_id=123, telegram_message_id=7, message="Legacy record"
    )
    with pytest.raises(DuplicateInquiryError):
        store.create_inquiry(
            telegram_user_id=123, telegram_message_id=7, message="Legacy record"
        )
