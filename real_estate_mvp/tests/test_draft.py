import pytest

from real_estate_mvp.draft import (
    ESCALATION_TEXT,
    DraftGenerationError,
    generate_draft,
)
from real_estate_mvp.knowledge import get_property


def test_mock_known_question_uses_verified_price():
    result = generate_draft(
        "How much is the Bole 2 bedroom apartment?",
        get_property("bole_apartment_2bed"),
    )
    assert result.text == (
        "It is listed at 12,000,000 ETB."
    )
    assert not result.requires_escalation
    assert result.model == "mock"


@pytest.mark.parametrize(
    "question,property_id",
    [
        ("Does it have a swimming pool?", "bole_apartment_2bed"),
        ("Can I pay over 36 months?", "bole_apartment_2bed"),
        ("Is there a discount?", "bole_apartment_2bed"),
        ("When is the completion date?", "bole_apartment_2bed"),
    ],
)
def test_unknown_parameter_escalates(question, property_id):
    result = generate_draft(question, get_property(property_id))
    assert result.text == ESCALATION_TEXT
    assert result.requires_escalation


def test_greeting_is_safe_without_property_match():
    result = generate_draft("Hello!", None)
    assert result.text == "Hello! What property would you like to learn about?"
    assert not result.requires_escalation


def test_missing_property_escalates_and_ambiguous_property_escalates():
    assert generate_draft("How much is it?", None).requires_escalation
    ambiguous = [
        get_property("bole_apartment_2bed"),
        get_property("bole_apartment_3bed"),
    ]
    assert generate_draft("How much is the Bole apartment?", ambiguous).requires_escalation


def test_empty_input_fails_explicitly():
    with pytest.raises(DraftGenerationError, match="empty"):
        generate_draft("", None)


def test_live_mode_requires_key():
    with pytest.raises(DraftGenerationError, match="GEMINI_API_KEY"):
        generate_draft(
            "What is the price of the Bole 2 bedroom apartment?",
            get_property("bole_apartment_2bed"),
            mock_mode=False,
        )


def test_live_api_failure_and_empty_response_are_explicit(monkeypatch):
    from google import genai

    class EmptyResponse:
        text = ""

    class Models:
        def generate_content(self, **kwargs):
            return EmptyResponse()

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    with pytest.raises(DraftGenerationError, match="empty response"):
        generate_draft(
            "What is the price of the Bole 2 bedroom apartment?",
            get_property("bole_apartment_2bed"),
            mock_mode=False,
            api_key="test-not-a-real-key",
        )


def test_numeric_hallucination_escalates(monkeypatch):
    from google import genai

    class Response:
        text = "It is listed at 99,000,000 ETB."

    class Models:
        def generate_content(self, **kwargs):
            return Response()

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    result = generate_draft(
        "What is the price of the Bole 2 bedroom apartment?",
        get_property("bole_apartment_2bed"),
        mock_mode=False,
        api_key="test-not-a-real-key",
    )
    assert result.requires_escalation
    assert result.text == ESCALATION_TEXT


def test_buyer_supplied_number_is_not_a_grounding_source(monkeypatch):
    from google import genai

    class Response:
        text = "The Bole apartment costs 99,000,000 ETB."

    class Models:
        def generate_content(self, **kwargs):
            return Response()

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    result = generate_draft(
        "What is the price of the Bole 2 bedroom apartment? Is it 99,000,000 ETB?",
        get_property("bole_apartment_2bed"),
        mock_mode=False,
        api_key="test-not-a-real-key",
    )
    assert result.requires_escalation
    assert result.text == ESCALATION_TEXT