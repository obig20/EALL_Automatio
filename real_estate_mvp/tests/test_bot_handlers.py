"""Bot handler regression tests: rate limiting, dedup, founder authorization."""

import asyncio
from types import SimpleNamespace

from real_estate_mvp.bot import buyer_text
from real_estate_mvp.config import Settings
from real_estate_mvp.queue import ReviewQueue
from real_estate_mvp.ratelimit import RateLimiter
from real_estate_mvp.storage import JSONLStore


def mock_settings(data_dir, **overrides) -> Settings:
    values = dict(
        telegram_bot_token="",
        founder_chat_id="777",
        gemini_api_key="",
        mock_mode=True,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=data_dir,
        properties_file=None,
        rate_limit_per_hour=10,
        rate_limit_per_day=50,
    )
    values.update(overrides)
    if values["properties_file"] is None:
        from pathlib import Path

        values["properties_file"] = Path(__file__).parents[1] / "data" / "properties.json"
    return Settings(**values)


def _fake_update(chat_id, user_id, message_id, text):
    message = SimpleNamespace(
        text=text,
        message_id=message_id,
        date=None,
        replies=[],
    )

    async def reply_text(reply, **kwargs):
        message.replies.append(reply)

    message.reply_text = reply_text
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _fake_context(store, settings, rate_limiter=None):
    async def fake_send_message(**kwargs):
        return SimpleNamespace(message_id=900)

    async def fake_edit_message_text(**kwargs):
        return None

    return SimpleNamespace(
        application=SimpleNamespace(
            bot=SimpleNamespace(
                send_message=fake_send_message,
                edit_message_text=fake_edit_message_text,
            ),
            bot_data={
                "settings": settings,
                "store": store,
                "rate_limiter": rate_limiter,
            },
        )
    )


async def _test_rate_limited_buyer_is_answered_but_not_persisted(tmp_path):
    store = JSONLStore(tmp_path)
    settings = mock_settings(tmp_path)
    limiter = RateLimiter(max_per_hour=1, max_per_day=50)
    update = _fake_update(123, 123, 1, "How much is the Bole 2 bedroom apartment?")

    await buyer_text(update, _fake_context(store, settings, limiter))
    assert len(store.list_inquiries()) == 1

    # The same sender is now over the hourly limit.
    second = _fake_update(123, 123, 2, "And the Summit villa?")
    await buyer_text(second, _fake_context(store, settings, limiter))
    assert len(store.list_inquiries()) == 1
    assert any("many messages" in reply for reply in second.effective_message.replies)
    events = [event["event"] for event in store.list_events()]
    assert "RATE_LIMITED" in events


async def _test_distinct_senders_are_not_rate_limited_together(tmp_path):
    store = JSONLStore(tmp_path)
    settings = mock_settings(tmp_path)
    limiter = RateLimiter(max_per_hour=1, max_per_day=50)

    first = _fake_update(111, 111, 1, "First buyer")
    second = _fake_update(222, 222, 1, "Second buyer")
    await buyer_text(first, _fake_context(store, settings, limiter))
    await buyer_text(second, _fake_context(store, settings, limiter))
    assert len(store.list_inquiries()) == 2


async def _test_duplicate_delivery_in_handler(tmp_path):
    store = JSONLStore(tmp_path)
    settings = mock_settings(tmp_path)
    limiter = RateLimiter(max_per_hour=10, max_per_day=50)
    update = _fake_update(123, 123, 42, "How much is the Bole 2 bedroom apartment?")

    await buyer_text(update, _fake_context(store, settings, limiter))
    assert len(store.list_inquiries()) == 1
    assert store.list_inquiries()[0]["status"] == "awaiting_review"

    # Telegram redelivers the same chat + message ID.
    redelivery = _fake_update(123, 123, 42, "How much is the Bole 2 bedroom apartment?")
    await buyer_text(redelivery, _fake_context(store, settings, limiter))
    inquiries = store.list_inquiries()
    assert len(inquiries) == 1
    events = [event["event"] for event in store.list_events()]
    assert events.count("INQUIRY_RECEIVED") == 1
    assert events.count("DRAFT_CREATED") == 1
    assert events.count("DUPLICATE_DELIVERY") == 1


def test_rate_limited_buyer_is_answered_but_not_persisted(tmp_path):
    asyncio.run(_test_rate_limited_buyer_is_answered_but_not_persisted(tmp_path))


def test_distinct_senders_are_not_rate_limited_together(tmp_path):
    asyncio.run(_test_distinct_senders_are_not_rate_limited_together(tmp_path))


def test_duplicate_delivery_in_handler(tmp_path):
    asyncio.run(_test_duplicate_delivery_in_handler(tmp_path))


def test_founder_actions_reject_group_chats(tmp_path):
    store = JSONLStore(tmp_path)
    inquiry = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=1,
        message="How much is the Bole 2 bedroom apartment?",
    )
    store.update_inquiry(
        inquiry["inquiry_id"],
        status="awaiting_review",
        draft="It is listed at 12,000,000 ETB.",
    )
    queue = ReviewQueue(store, founder_chat_id="777")
    sent = []

    async def send_message(chat_id, text):
        sent.append(text)

    async def run_all():
        group_chat = -100123456789
        approve = await queue.approve_and_send(
            inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=group_chat,
            send_message=send_message,
        )
        reject = await queue.reject(
            inquiry["inquiry_id"], actor_user_id=777, chat_id=group_chat
        )
        manual = await queue.manual_send(
            inquiry["inquiry_id"],
            "Manual reply",
            actor_user_id=777,
            chat_id=group_chat,
            send_message=send_message,
        )
        resolve = await queue.resolve_ambiguous_send(
            inquiry["inquiry_id"],
            "delivered",
            actor_user_id=777,
            chat_id=group_chat,
        )
        return approve, reject, manual, resolve

    approve, reject, manual, resolve = asyncio.run(run_all())
    assert approve["status"] == "unauthorized"
    assert reject["status"] == "unauthorized"
    assert manual["status"] == "unauthorized"
    assert resolve["status"] == "unauthorized"
    assert sent == []


def test_founder_actions_reject_wrong_user_in_private_chat(tmp_path):
    store = JSONLStore(tmp_path)
    inquiry = store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=1,
        message="How much is the Bole 2 bedroom apartment?",
    )
    store.update_inquiry(
        inquiry["inquiry_id"],
        status="awaiting_review",
        draft="It is listed at 12,000,000 ETB.",
    )
    queue = ReviewQueue(store, founder_chat_id="777")

    async def run_all():
        # Correct private chat, but a different user ID.
        approve = await queue.approve_and_send(
            inquiry["inquiry_id"],
            actor_user_id=888,
            chat_id=777,
            send_message=lambda chat_id, text: None,
        )
        reject = await queue.reject(
            inquiry["inquiry_id"], actor_user_id=888, chat_id=777
        )
        return approve, reject

    approve, reject = asyncio.run(run_all())
    assert approve["status"] == "unauthorized"
    assert reject["status"] == "unauthorized"


def test_unauthorized_actions_are_logged_without_sensitive_data(tmp_path):
    store = JSONLStore(tmp_path)
    queue = ReviewQueue(store, founder_chat_id="777")

    async def run_all():
        await queue.reject(
            "inq_000001", actor_user_id=888, chat_id=888
        )

    asyncio.run(run_all())
    events = store.list_events()
    assert events[0]["event"] == "UNAUTHORIZED_ACTION"
    assert events[0]["action"] == "reject"
    # The log entry must not contain the buyer message or draft text.
    assert "message" not in events[0]
    assert "draft" not in events[0]
