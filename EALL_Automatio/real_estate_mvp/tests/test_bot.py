import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from real_estate_mvp.bot import prepare_inquiry, recover_pending_inquiries
from real_estate_mvp.config import Settings
from real_estate_mvp.storage import JSONLStore


def mock_settings(data_dir: Path) -> Settings:
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


class InquiryPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_question_is_persisted_for_human_review_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            inquiry = await prepare_inquiry(
                store,
                mock_settings(Path(directory)),
                telegram_user_id="123",
                telegram_message_id="42",
                message="How much is the Bole 2 bedroom apartment?",
                received_at="2026-10-02T12:30:00+03:00",
            )

            self.assertEqual(inquiry["inquiry_id"], "inq_000001")
            self.assertEqual(inquiry["status"], "awaiting_review")
            self.assertEqual(inquiry["matched_properties"], ["bole_apartment_2bed"])
            self.assertIn("12,000,000 ETB", inquiry["draft"])
            self.assertIsNone(inquiry["sent_at"])
            self.assertEqual(store.get_pending_inquiries()[0]["inquiry_id"], "inq_000001")
            events = [event["event"] for event in store.list_events()]
            self.assertIn("INQUIRY_RECEIVED", events)
            self.assertIn("AWAITING_REVIEW", events)
            self.assertNotIn("SEND_SUCCESS", events)

    async def test_unknown_amenity_escalates_and_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            inquiry = await prepare_inquiry(
                store,
                mock_settings(Path(directory)),
                telegram_user_id="123",
                telegram_message_id="43",
                message="Does the Bole 2 bedroom apartment have a swimming pool?",
            )
            self.assertEqual(inquiry["status"], "escalated")
            self.assertTrue(inquiry["requires_escalation"])
            self.assertEqual(inquiry["draft"], "[ESCALATE: Unknown Property Parameter]")

    async def test_empty_and_very_long_input_do_not_crash_or_autosend(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            with self.assertRaises(ValueError):
                await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="44",
                    message="",
                )
            inquiry = await prepare_inquiry(
                store,
                mock_settings(Path(directory)),
                telegram_user_id="123",
                telegram_message_id="45",
                message="Bole apartment " + ("question " * 500),
            )
            self.assertEqual(inquiry["status"], "draft_failed")
            self.assertIsNone(inquiry["sent_at"])

    async def test_restart_preserves_review_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            inquiry = await prepare_inquiry(
                store,
                mock_settings(Path(directory)),
                telegram_user_id="123",
                telegram_message_id="46",
                message="Where is the Summit villa?",
            )
            restarted = JSONLStore(directory)
            self.assertEqual(
                restarted.get_inquiry(inquiry["inquiry_id"])["status"],
                "awaiting_review",
            )
            self.assertEqual(len(restarted.get_pending_inquiries()), 1)

    async def test_recovery_resumes_draft_and_never_sends_to_buyer(self):
        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edited = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return SimpleNamespace(message_id=900)

            async def edit_message_text(self, **kwargs):
                self.edited.append(kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            interrupted = store.create_inquiry(
                telegram_user_id="123",
                telegram_message_id="47",
                message="How much is the Bole 2 bedroom apartment?",
            )
            store.append_event("INQUIRY_RECEIVED", interrupted["inquiry_id"])
            fake_bot = FakeBot()
            app = SimpleNamespace(
                bot=fake_bot,
                bot_data={
                    "settings": replace(
                        mock_settings(Path(directory)), founder_chat_id="777"
                    ),
                    "store": store,
                },
            )

            await recover_pending_inquiries(app)

            recovered = store.get_inquiry(interrupted["inquiry_id"])
            self.assertEqual(recovered["status"], "awaiting_review")
            self.assertEqual(recovered["review_message_id"], 900)
            self.assertEqual(len(fake_bot.sent), 1)
            self.assertEqual(fake_bot.sent[0]["chat_id"], 777)
            self.assertNotEqual(fake_bot.sent[0]["chat_id"], 123)

    async def test_recovery_refreshes_ambiguous_send_without_retrying_buyer_send(self):
        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edited = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return SimpleNamespace(message_id=901)

            async def edit_message_text(self, **kwargs):
                self.edited.append(kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            interrupted = store.create_inquiry(
                telegram_user_id="123",
                telegram_message_id="48",
                message="How much is the Bole 2 bedroom apartment?",
            )
            store.update_inquiry(
                interrupted["inquiry_id"],
                status="sending",
                draft="Founder-approved draft",
                review_message_id=55,
            )
            fake_bot = FakeBot()
            app = SimpleNamespace(
                bot=fake_bot,
                bot_data={
                    "settings": replace(
                        mock_settings(Path(directory)), founder_chat_id="777"
                    ),
                    "store": store,
                },
            )

            await recover_pending_inquiries(app)

            self.assertEqual(len(fake_bot.edited), 1)
            self.assertEqual(fake_bot.edited[0]["chat_id"], 777)
            self.assertEqual(fake_bot.edited[0]["message_id"], 55)
            self.assertEqual(fake_bot.sent, [])
            self.assertEqual(store.get_inquiry(interrupted["inquiry_id"])["status"], "sending")