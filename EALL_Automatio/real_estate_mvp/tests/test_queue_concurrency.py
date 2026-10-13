"""Review queue concurrency regression tests."""

import asyncio
import time

from real_estate_mvp.queue import ReviewQueue
from real_estate_mvp.storage import JSONLStore


def _queue_with_inquiry(tmp_path, inquiry_id: str, draft: str):
    store = JSONLStore(tmp_path)
    store.create_inquiry(
        telegram_user_id=123,
        telegram_message_id=1,
        message="Synthetic",
    )
    store.update_inquiry(
        inquiry_id,
        status="awaiting_review",
        draft=draft,
    )
    return ReviewQueue(store, founder_chat_id="777"), store


async def _test_unrelated_inquiries_do_not_block_each_other(tmp_path):
    queue, _ = _queue_with_inquiry(tmp_path, "inq_000001", "Draft A")
    store = queue.store
    store.create_inquiry(telegram_user_id=124, telegram_message_id=2, message="B")
    store.update_inquiry("inq_000002", status="awaiting_review", draft="Draft B")

    slow_started = asyncio.Event()

    async def slow_send(chat_id, text):
        slow_started.set()
        await asyncio.sleep(0.4)

    async def fast_send(chat_id, text):
        return None

    task_a = asyncio.create_task(
        queue.approve_and_send(
            "inq_000001",
            actor_user_id=777,
            chat_id=777,
            send_message=slow_send,
        )
    )
    await slow_started.wait()
    started = time.monotonic()
    result_b = await queue.approve_and_send(
        "inq_000002",
        actor_user_id=777,
        chat_id=777,
        send_message=fast_send,
    )
    elapsed_b = time.monotonic() - started
    await task_a

    assert result_b["status"] == "sent"
    # The unrelated approval completed while inquiry A's send was
    # still in progress (well under the 0.4s slow send duration).
    assert elapsed_b < 0.3


async def _test_concurrent_same_inquiry_approval_sends_once(tmp_path):
    queue, _ = _queue_with_inquiry(tmp_path, "inq_000001", "Draft")
    sends = []

    async def send_message(chat_id, text):
        await asyncio.sleep(0.05)
        sends.append((chat_id, text))

    results = await asyncio.gather(
        queue.approve_and_send(
            "inq_000001",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        ),
        queue.approve_and_send(
            "inq_000001",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        ),
        queue.approve_and_send(
            "inq_000001",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        ),
    )
    statuses = sorted(result["status"] for result in results)
    assert statuses == ["already_sent", "already_sent", "sent"]
    assert len(sends) == 1


async def _test_concurrent_manual_send_and_approval_send_once(tmp_path):
    queue, _ = _queue_with_inquiry(tmp_path, "inq_000001", "Draft")
    sends = []

    async def send_message(chat_id, text):
        await asyncio.sleep(0.05)
        sends.append(text)

    results = await asyncio.gather(
        queue.approve_and_send(
            "inq_000001",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        ),
        queue.manual_send(
            "inq_000001",
            "Manual reply",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        ),
    )
    # Whichever operation acquires the per-inquiry lock first delivers
    # exactly once; the other is blocked by the in-progress send.
    assert sorted(result["status"] for result in results) in (
        ["already_sent_or_sending", "sent"],
        ["already_in_progress", "sent"],
    )
    assert len(sends) == 1


def test_unrelated_inquiries_do_not_block_each_other(tmp_path):
    asyncio.run(_test_unrelated_inquiries_do_not_block_each_other(tmp_path))


def test_concurrent_same_inquiry_approval_sends_once(tmp_path):
    asyncio.run(_test_concurrent_same_inquiry_approval_sends_once(tmp_path))


def test_concurrent_manual_send_and_approval_send_once(tmp_path):
    asyncio.run(_test_concurrent_manual_send_and_approval_send_once(tmp_path))
