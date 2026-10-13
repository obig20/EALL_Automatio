import unittest

from real_estate_mvp.draft import ESCALATION_TEXT
from real_estate_mvp.queue import ReviewQueue, format_review_message
from real_estate_mvp.storage import JSONLStore


class ReviewQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self.tempdir = tempfile.TemporaryDirectory()
        self.store = JSONLStore(self.tempdir.name)
        self.queue = ReviewQueue(self.store, founder_chat_id="777")
        self.inquiry = self.store.create_inquiry(
            telegram_user_id="123",
            telegram_message_id="42",
            message="How much is the Bole 2 bedroom apartment?",
        )
        self.store.update_inquiry(
            self.inquiry["inquiry_id"],
            status="awaiting_review",
            draft="It is listed at 12,000,000 ETB.",
            matched_properties=["bole_apartment_2bed"],
            property_context=[
                {
                    "name": "Bole 2 Bedroom Apartment",
                    "property_type": "Apartment",
                    "bedrooms": 2,
                    "price": "12,000,000 ETB",
                    "location": "Bole, near Atlas",
                    "payment_plan": "30% downpayment, balance over 24 months",
                    "availability": "3 demo units remaining",
                    "amenities": [],
                }
            ],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    async def test_only_founder_can_approve_and_duplicate_click_does_not_resend(self):
        sent = []

        async def send_message(chat_id, text):
            sent.append((chat_id, text))

        unauthorized = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=888,
            chat_id=888,
            send_message=send_message,
        )
        self.assertEqual(unauthorized["status"], "unauthorized")
        self.assertEqual(sent, [])

        approved = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        )
        duplicate = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        )
        self.assertEqual(approved["status"], "sent")
        self.assertEqual(duplicate["status"], "already_sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.store.get_inquiry(self.inquiry["inquiry_id"])["status"], "sent")
        event_names = [event["event"] for event in self.store.list_events()]
        self.assertIn("UNAUTHORIZED_ACTION", event_names)
        self.assertIn("SEND_SUCCESS", event_names)

    async def test_rejection_never_sends_to_buyer(self):
        rejected = await self.queue.reject(
            self.inquiry["inquiry_id"], actor_user_id=777, chat_id=777
        )
        self.assertEqual(rejected["status"], "rejected")
        sent = []
        result = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=lambda *args: sent.append(args),
        )
        self.assertEqual(result["status"], "not_approvable")
        self.assertEqual(sent, [])
        self.assertEqual(self.store.get_inquiry(self.inquiry["inquiry_id"])["status"], "rejected")

    async def test_escalated_draft_cannot_be_approved_but_manual_send_can(self):
        self.store.update_inquiry(
            self.inquiry["inquiry_id"],
            status="escalated",
            draft=ESCALATION_TEXT,
            requires_escalation=True,
        )
        sent = []

        async def send_message(chat_id, text):
            sent.append((chat_id, text))

        automatic = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        )
        self.assertEqual(automatic["status"], "not_approvable")
        manual = await self.queue.manual_send(
            self.inquiry["inquiry_id"],
            "I will confirm this detail with the team.",
            actor_user_id=777,
            chat_id=777,
            send_message=send_message,
        )
        self.assertEqual(manual["status"], "sent")
        self.assertEqual(sent[0][1], "I will confirm this detail with the team.")

    async def test_transient_failure_retries_and_persists_attempts(self):
        sends = 0

        async def flaky_send(chat_id, text):
            nonlocal sends
            sends += 1
            if sends < 2:
                raise ConnectionError("offline")

        from real_estate_mvp import queue as queue_module

        original = queue_module.with_transient_retry_async

        async def fast_retry(operation, **kwargs):
            return await original(
                operation, attempts=3, wait_min_seconds=0, wait_max_seconds=0
            )

        queue_module.with_transient_retry_async = fast_retry
        try:
            result = await self.queue.approve_and_send(
                self.inquiry["inquiry_id"],
                actor_user_id=777,
                chat_id=777,
                send_message=flaky_send,
            )
        finally:
            queue_module.with_transient_retry_async = original
        self.assertEqual(result["status"], "sent")
        self.assertEqual(sends, 2)
        self.assertEqual(result["inquiry"]["send_attempts"], 2)
        self.assertIn(
            "SEND_RETRY", [event["event"] for event in self.store.list_events()]
        )

    async def test_failed_send_is_persisted_and_not_resent_on_duplicate_click(self):
        calls = 0

        async def fail_send(chat_id, text):
            nonlocal calls
            calls += 1
            raise ValueError("permanent failure")

        result = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=fail_send,
        )
        second = await self.queue.approve_and_send(
            self.inquiry["inquiry_id"],
            actor_user_id=777,
            chat_id=777,
            send_message=fail_send,
        )
        self.assertEqual(result["status"], "send_failed")
        self.assertEqual(second["status"], "not_approvable")
        self.assertEqual(calls, 1)
        self.assertEqual(self.store.get_pending_inquiries()[0]["status"], "send_failed")
        retry = await self.queue.manual_send(
            self.inquiry["inquiry_id"],
            "Retry message",
            actor_user_id=777,
            chat_id=777,
            send_message=fail_send,
        )
        self.assertEqual(retry["status"], "resolve_required")
        self.assertEqual(calls, 1)

    async def test_manual_send_authorization(self):
        result = await self.queue.manual_send(
            self.inquiry["inquiry_id"],
            "Unauthorized text",
            actor_user_id=55,
            chat_id=55,
            send_message=lambda *_: None,
        )
        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(self.store.get_inquiry(self.inquiry["inquiry_id"])["status"], "awaiting_review")

    async def test_ambiguous_send_requires_founder_resolution(self):
        inquiry_id = self.inquiry["inquiry_id"]
        self.store.update_inquiry(inquiry_id, status="sending", send_attempts=1)
        unauthorized = await self.queue.resolve_ambiguous_send(
            inquiry_id,
            "not_delivered",
            actor_user_id=123,
            chat_id=123,
        )
        self.assertEqual(unauthorized["status"], "unauthorized")
        self.assertEqual(self.store.get_inquiry(inquiry_id)["status"], "sending")

        resolved = await self.queue.resolve_ambiguous_send(
            inquiry_id,
            "delivered",
            actor_user_id=777,
            chat_id=777,
        )
        self.assertEqual(resolved["status"], "sent")
        self.assertEqual(
            resolved["inquiry"]["send_resolution"], "founder_confirmed_delivered"
        )
        duplicate_sends = []
        result = await self.queue.approve_and_send(
            inquiry_id,
            actor_user_id=777,
            chat_id=777,
            send_message=lambda *args: duplicate_sends.append(args),
        )
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(duplicate_sends, [])

    async def test_confirmed_nondelivery_can_only_retry_as_manual_send(self):
        inquiry_id = self.inquiry["inquiry_id"]
        self.store.update_inquiry(inquiry_id, status="sending", send_attempts=1)
        resolved = await self.queue.resolve_ambiguous_send(
            inquiry_id,
            "not_delivered",
            actor_user_id=777,
            chat_id=777,
        )
        self.assertEqual(resolved["status"], "send_failed")

        sent = []
        result = await self.queue.manual_send(
            inquiry_id,
            "Founder-reviewed response",
            actor_user_id=777,
            chat_id=777,
            send_message=lambda chat_id, text: sent.append((chat_id, text)),
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(sent, [(123, "Founder-reviewed response")])


def test_review_message_marks_sample_facts_and_escalation():
    # Reuse a minimal in-memory-looking record to keep this formatting test isolated.
    message = format_review_message(
        {
            "inquiry_id": "inq_000001",
            "telegram_user_id": "123",
            "message": "Does it have a pool?",
            "property_context": [{"name": "Bole Demo", "amenities": []}],
            "draft": ESCALATION_TEXT,
            "requires_escalation": True,
            "status": "escalated",
            "escalation_reason": "Unknown Property Parameter",
        }
    )
    assert "DEMO DATA" in message
    assert "ESCALATION REQUIRED" in message
    assert ESCALATION_TEXT in message