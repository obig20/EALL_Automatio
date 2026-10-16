import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_mvp.bot import prepare_inquiry, recover_pending_inquiries
from real_estate_mvp.config import Settings
from real_estate_mvp.draft import DraftGenerationError
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
    def assert_failure_diagnostic(
        self,
        store: JSONLStore,
        inquiry: dict,
        *,
        category: str,
        stage: str,
    ) -> dict:
        self.assertEqual(inquiry["status"], "draft_failed")
        self.assertIsNone(inquiry["draft"])
        self.assertIsNone(inquiry.get("sent_at"))
        self.assertEqual(inquiry["draft_error_category"], category)
        self.assertEqual(inquiry["draft_error_stage"], stage)
        event = next(
            event
            for event in store.list_events()
            if event["event"] == "DRAFT_FAILED"
            and event["inquiry_id"] == inquiry["inquiry_id"]
        )
        self.assertEqual(event["error_category"], category)
        self.assertEqual(event["stage"], stage)
        self.assertEqual(
            set(event),
            {
                "timestamp",
                "event",
                "inquiry_id",
                "error",
                "error_category",
                "stage",
                "cause_class",
                "upstream_status",
                "message_length",
                "mock_mode",
            },
        )
        self.assertNotIn(inquiry["message"], str(event))
        self.assertNotIn(
            "SEND_SUCCESS",
            [
                record["event"]
                for record in store.list_events()
                if record["inquiry_id"] == inquiry["inquiry_id"]
            ],
        )
        return event

    async def test_known_question_is_persisted_for_human_review_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            # Demo properties escalate to protect buyers from synthetic data
            demo_inquiry = await prepare_inquiry(
                store,
                mock_settings(Path(directory)),
                telegram_user_id="123",
                telegram_message_id="42",
                message="How much is the Bole 2 bedroom apartment?",
                received_at="2026-10-02T12:30:00+03:00",
            )
            self.assertEqual(demo_inquiry["inquiry_id"], "inq_000001")
            self.assertEqual(demo_inquiry["status"], "escalated")
            self.assertTrue(demo_inquiry["requires_escalation"])
            self.assertEqual(demo_inquiry["draft"], "[ESCALATE: Unknown Property Parameter]")
            self.assertIsNone(demo_inquiry["sent_at"])
            events = [event["event"] for event in store.list_events()]
            self.assertIn("INQUIRY_RECEIVED", events)
            self.assertIn("ESCALATION", events)
            self.assertNotIn("SEND_SUCCESS", events)

            # Verified properties produce grounded drafts awaiting founder review
            with patch(
                "real_estate_mvp.bot.get_property_context",
                lambda pid: {
                    "name": "Bole 2 Bedroom Apartment",
                    "property_type": "Apartment",
                    "bedrooms": 2,
                    "price": "12,000,000 ETB",
                    "location": "Bole, near Atlas",
                    "payment_plan": "30% downpayment, balance over 24 months",
                    "availability": "3 units remaining",
                    "amenities": [],
                    "verified": True,
                    "provenance": "verified",
                },
            ):
                verified_inquiry = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="124",
                    telegram_message_id="43",
                    message="How much is the Bole 2 bedroom apartment?",
                    received_at="2026-10-02T12:31:00+03:00",
                )
                self.assertEqual(verified_inquiry["status"], "awaiting_review")
                self.assertIn("12,000,000 ETB", verified_inquiry["draft"])
                self.assertFalse(verified_inquiry["requires_escalation"])
                self.assertIsNone(verified_inquiry["sent_at"])
                new_events = [event["event"] for event in store.list_events()]
                self.assertIn("AWAITING_REVIEW", new_events)

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
            event = self.assert_failure_diagnostic(
                store,
                inquiry,
                category="INPUT_TOO_LONG",
                stage="draft_input_validation",
            )
            self.assertEqual(event["error"], "DraftGenerationError")
            self.assertGreater(event["message_length"], 4000)
            self.assertTrue(event["mock_mode"])
            self.assertIsNone(inquiry["sent_at"])

    async def test_property_context_failure_records_safe_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            private_detail = "private context failure detail"
            with patch(
                "real_estate_mvp.bot.get_property_context",
                side_effect=RuntimeError(private_detail),
            ):
                inquiry = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="49",
                    message="How much is the Bole 2 bedroom apartment?",
                )

            event = self.assert_failure_diagnostic(
                store,
                inquiry,
                category="PROPERTY_CONTEXT_FAILED",
                stage="property_context",
            )
            self.assertEqual(event["error"], "RuntimeError")
            self.assertNotIn(private_detail, str(event))

    async def test_generation_failure_records_error_and_cause_classes_only(self):
        class ProviderFailure(Exception):
            status_code = 429

        def fail_generation(*args, **kwargs):
            try:
                raise ProviderFailure("secret-key and private buyer details")
            except ProviderFailure as cause:
                raise DraftGenerationError(
                    "safe wrapper text",
                    category="DRAFT_GENERATION_FAILED",
                ) from cause

        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            with patch(
                "real_estate_mvp.bot.generate_draft",
                side_effect=fail_generation,
            ):
                inquiry = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="50",
                    message="How much is the Bole 2 bedroom apartment?",
                )

            event = self.assert_failure_diagnostic(
                store,
                inquiry,
                category="DRAFT_GENERATION_FAILED",
                stage="draft_generation",
            )
            self.assertEqual(event["error"], "DraftGenerationError")
            self.assertEqual(event["cause_class"], "ProviderFailure")
            self.assertEqual(event["upstream_status"], 429)
            self.assertNotIn("secret-key", str(event))
            self.assertNotIn("private buyer details", str(event))
            self.assertEqual(inquiry["draft_error_cause_class"], "ProviderFailure")

    async def test_empty_model_response_category_is_persisted(self):
        def empty_response(*args, **kwargs):
            raise DraftGenerationError(
                "Gemini returned an empty response",
                category="MODEL_EMPTY_RESPONSE",
            )

        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            with patch(
                "real_estate_mvp.bot.generate_draft",
                side_effect=empty_response,
            ):
                inquiry = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="51",
                    message="How much is the Bole 2 bedroom apartment?",
                )

            event = self.assert_failure_diagnostic(
                store,
                inquiry,
                category="MODEL_EMPTY_RESPONSE",
                stage="draft_generation",
            )
            self.assertEqual(event["error"], "DraftGenerationError")

    async def test_pipeline_failure_and_persistence_failure_are_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JSONLStore(directory)
            with patch(
                "real_estate_mvp.bot.find_relevant_properties",
                side_effect=LookupError("private matcher detail"),
            ):
                unexpected = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="52",
                    message="How much is the Bole 2 bedroom apartment?",
                )
            event = self.assert_failure_diagnostic(
                store,
                unexpected,
                category="UNEXPECTED_PIPELINE_ERROR",
                stage="property_matching",
            )
            self.assertEqual(event["error"], "LookupError")
            self.assertNotIn("private matcher detail", str(event))

            original_append = store.append_event
            should_fail = True

            def fail_draft_started_once(event_name, inquiry_id=None, **details):
                nonlocal should_fail
                if event_name == "DRAFT_STARTED" and should_fail:
                    should_fail = False
                    raise OSError("private persistence path")
                return original_append(event_name, inquiry_id, **details)

            with patch.object(store, "append_event", side_effect=fail_draft_started_once):
                persistence_failure = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="53",
                    message="How much is the Bole 2 bedroom apartment?",
                )
            event = self.assert_failure_diagnostic(
                store,
                persistence_failure,
                category="PERSISTENCE_FAILED",
                stage="draft_start_persistence",
            )
            self.assertEqual(event["error"], "OSError")
            self.assertNotIn("private persistence path", str(event))

            should_fail_result_event = True

            def fail_draft_created_once(event_name, inquiry_id=None, **details):
                nonlocal should_fail_result_event
                if event_name in {"DRAFT_CREATED", "ESCALATION"} and should_fail_result_event:
                    should_fail_result_event = False
                    raise OSError("private result persistence detail")
                return original_append(event_name, inquiry_id, **details)

            with patch.object(
                store,
                "append_event",
                side_effect=fail_draft_created_once,
            ):
                result_persistence_failure = await prepare_inquiry(
                    store,
                    mock_settings(Path(directory)),
                    telegram_user_id="123",
                    telegram_message_id="54",
                    message="How much is the Bole 2 bedroom apartment?",
                )
            event = self.assert_failure_diagnostic(
                store,
                result_persistence_failure,
                category="PERSISTENCE_FAILED",
                stage="draft_result_persistence",
            )
            self.assertEqual(event["error"], "OSError")
            self.assertNotIn("private result persistence detail", str(event))

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
                "escalated",
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
            self.assertEqual(recovered["status"], "escalated")
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

    def test_review_keyboard_omits_approve_for_unverified_records(self):
        from real_estate_mvp.bot import _prepare_review_keyboard

        unverified_inquiry = {
            "inquiry_id": "inq_000001",
            "status": "awaiting_review",
            "requires_escalation": False,
            "property_context": [{"name": "Demo", "provenance": "demo", "verified": False}],
        }
        keyboard = _prepare_review_keyboard(unverified_inquiry)
        buttons = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertNotIn("Approve & Send", buttons)
        self.assertIn("Edit Manually", buttons)
        self.assertIn("Reject", buttons)

        verified_inquiry = {
            "inquiry_id": "inq_000002",
            "status": "awaiting_review",
            "requires_escalation": False,
            "property_context": [{"name": "Verified", "provenance": "verified", "verified": True}],
        }
        keyboard_verified = _prepare_review_keyboard(verified_inquiry)
        buttons_v = [btn.text for row in keyboard_verified.inline_keyboard for btn in row]
        self.assertIn("Approve & Send", buttons_v)