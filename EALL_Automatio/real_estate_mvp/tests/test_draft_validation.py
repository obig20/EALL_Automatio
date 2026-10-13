"""Deterministic LLM draft validation regression tests.

The fake Gemini client below returns canned model output so the
validator can be exercised without credentials.
"""

from types import SimpleNamespace

import pytest

from real_estate_mvp.draft import ESCALATION_TEXT, generate_draft
from real_estate_mvp.knowledge import get_property


def live_draft(monkeypatch, model_text: str, message: str, property_id: str):
    """Generate a live-mode draft with a fake Gemini backend."""
    from google import genai

    def fake_client(*args, **kwargs):
        return SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: SimpleNamespace(text=model_text)
            )
        )

    monkeypatch.setattr(genai, "Client", fake_client)
    return generate_draft(
        message,
        get_property(property_id),
        mock_mode=False,
        api_key="synthetic-test-key",
    )


def test_unsupported_amenity_is_rejected(monkeypatch):
    # The buyer asks an open question; the model volunteers a pool
    # claim that the Kazanchis data (elevator only) cannot support.
    result = live_draft(
        monkeypatch,
        "The Kazanchis apartment has a swimming pool.",
        "Tell me more about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert result.text == ESCALATION_TEXT
    assert "amenity" in result.reason


def test_unsupported_location_claim_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "It is located near the airport, close to the metro station.",
        "Where is the Kazanchis apartment located?",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "location" in result.reason


def test_unsupported_financing_claim_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "You can finance this with a bank mortgage at low interest.",
        "Tell me more about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "financing" in result.reason


def test_unsupported_property_feature_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "The apartment is furnished and includes a rooftop terrace.",
        "Tell me about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "amenity" in result.reason


def test_unsupported_availability_claim_is_rejected(monkeypatch):
    # Completion date is null in the demo data, so "ready to move in"
    # is an invented availability claim.
    result = live_draft(
        monkeypatch,
        "The property is ready to move in and units are still left.",
        "Tell me more about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "availability" in result.reason


def test_unsupported_legal_claim_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "The title deed is clear and ownership is freehold.",
        "Tell me more about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "legal" in result.reason


def test_unsupported_price_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "It is listed at 15,000,000 ETB.",
        "How much is the Kazanchis apartment?",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "numeric" in result.reason


def test_unsupported_numeric_value_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "The building has 12 floors.",
        "Tell me about the Kazanchis apartment.",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "numeric" in result.reason


def test_wrong_bedroom_count_is_rejected(monkeypatch):
    result = live_draft(
        monkeypatch,
        "The Kazanchis apartment has 3 bedrooms.",
        "How many bedrooms does the Kazanchis apartment have?",
        "kazanchis_apartment",
    )
    assert result.requires_escalation
    assert "bedroom" in result.reason


def test_marketing_and_advice_phrases_are_rejected(monkeypatch):
    for text in (
        "This luxury apartment is a must-see, act now!",
        "I promise you will love it. We guarantee quick approval.",
        "For legal advice on the contract, contact our bank.",
    ):
        result = live_draft(
            monkeypatch,
            text,
            "Tell me about the Bole 2 bedroom apartment.",
            "bole_apartment_2bed",
        )
        assert result.requires_escalation, text
        assert "marketing" in result.reason


def test_grounded_draft_is_accepted(monkeypatch):
    result = live_draft(
        monkeypatch,
        "It is listed at 9,200,000 ETB. The listed amenities include elevator.",
        "How much is the Kazanchis apartment?",
        "kazanchis_apartment",
    )
    assert not result.requires_escalation
    assert "9,200,000 ETB" in result.text
    assert result.model != "mock"


def test_grounded_availability_draft_is_accepted(monkeypatch):
    result = live_draft(
        monkeypatch,
        "The demo listing shows 4 demo units remaining.",
        "Is the Kazanchis apartment available?",
        "kazanchis_apartment",
    )
    assert not result.requires_escalation


def test_mock_drafts_pass_their_own_validation():
    # Mock drafts are built from verified data, so they must remain
    # grounded; the validator is applied to them as a self-check.
    result = generate_draft(
        "How much is the Bole 2 bedroom apartment?",
        get_property("bole_apartment_2bed"),
    )
    assert not result.requires_escalation
    assert result.text == "It is listed at 12,000,000 ETB."


def test_mock_availability_and_payment_drafts_stay_grounded():
    availability = generate_draft(
        "Is the Summit villa available?",
        get_property("summit_villa"),
    )
    assert not availability.requires_escalation
    payment = generate_draft(
        "What is the payment plan for the Summit villa?",
        get_property("summit_villa"),
    )
    assert not payment.requires_escalation


def test_configured_gemini_settings_reach_the_live_api_call(
    monkeypatch, tmp_path
):
    """Configured model, temperature, token limit, and timeout must
    reach the actual genai.Client / generate_content call."""
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    from real_estate_mvp import bot as bot_module
    from real_estate_mvp.bot import prepare_inquiry
    from real_estate_mvp.config import Settings
    from real_estate_mvp.storage import JSONLStore

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["generate_content"] = kwargs
            return SimpleNamespace(text="It is listed at 9,200,000 ETB.")

    def fake_client(*args, **kwargs):
        captured["client"] = kwargs
        return SimpleNamespace(models=FakeModels())

    from google import genai

    monkeypatch.setattr(genai, "Client", fake_client)

    settings = Settings(
        telegram_bot_token="",
        founder_chat_id="",
        gemini_api_key="synthetic-test-key",
        mock_mode=False,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=tmp_path,
        properties_file=Path(__file__).parents[1] / "data" / "properties.json",
        gemini_model="gemini-test-model",
        gemini_temperature=0.1,
        gemini_max_output_tokens=512,
        gemini_timeout_seconds=17,
    )
    store = JSONLStore(tmp_path)
    inquiry = asyncio.run(
        prepare_inquiry(
            store,
            settings,
            telegram_user_id=1,
            telegram_message_id=1,
            message="How much is the Kazanchis apartment?",
            telegram_chat_id=1,
        )
    )

    assert inquiry["status"] == "awaiting_review"
    assert inquiry["draft"] == "It is listed at 9,200,000 ETB."
    assert captured["client"]["api_key"] == "synthetic-test-key"
    assert captured["client"]["http_options"].timeout == 17
    assert captured["generate_content"]["model"] == "gemini-test-model"
    config = captured["generate_content"]["config"]
    assert config.temperature == 0.1
    assert config.max_output_tokens == 512


def test_fractional_timeout_is_coerced_to_whole_seconds(monkeypatch):
    """genai.HttpOptions requires an integer timeout; a fractional
    value must not break live draft generation."""
    from google import genai

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text="It is listed at 9,200,000 ETB.")

    def fake_client(*args, **kwargs):
        captured["client"] = kwargs
        return SimpleNamespace(models=FakeModels())

    monkeypatch.setattr(genai, "Client", fake_client)
    result = generate_draft(
        "How much is the Kazanchis apartment?",
        get_property("kazanchis_apartment"),
        mock_mode=False,
        api_key="synthetic-test-key",
        timeout_seconds=17.5,
    )
    assert not result.requires_escalation
    assert captured["client"]["http_options"].timeout == 17
