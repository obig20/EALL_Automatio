import pytest

from real_estate_mvp.draft import (
    ESCALATION_TEXT,
    DraftGenerationError,
    generate_draft,
)
from real_estate_mvp.knowledge import get_property


def _verified(prop: dict | None) -> dict | None:
    if prop is None:
        return None
    record = dict(prop)
    record["verified"] = True
    record["provenance"] = "verified"
    return record


def test_mock_known_question_uses_verified_price():
    result = generate_draft(
        "How much is the Bole 2 bedroom apartment?",
        _verified(get_property("bole_apartment_2bed")),
    )
    assert result.text == (
        "It is listed at 12,000,000 ETB."
    )
    assert not result.requires_escalation
    assert result.model == "mock"


def test_demo_property_is_blocked_from_drafting():
    result = generate_draft(
        "How much is the Bole 2 bedroom apartment?",
        get_property("bole_apartment_2bed"),
    )
    assert result.text == ESCALATION_TEXT
    assert result.requires_escalation
    assert result.reason == "Unverified Property Record"


def test_unverified_and_imported_records_escalate():
    prop_imported = dict(
        get_property("bole_apartment_2bed"), verified=False, provenance="imported"
    )
    result_imported = generate_draft("How much is it?", prop_imported)
    assert result_imported.text == ESCALATION_TEXT
    assert result_imported.requires_escalation
    assert result_imported.reason == "Unverified Property Record"

    prop_unverified = dict(
        get_property("bole_apartment_2bed"), verified=False, provenance="unverified"
    )
    result_unverified = generate_draft("How much is it?", prop_unverified)
    assert result_unverified.text == ESCALATION_TEXT
    assert result_unverified.requires_escalation
    assert result_unverified.reason == "Unverified Property Record"

    prop_missing = dict(
        get_property("bole_apartment_2bed"), verified=True, provenance="demo"
    )
    result_missing = generate_draft("How much is it?", prop_missing)
    assert result_missing.text == ESCALATION_TEXT
    assert result_missing.requires_escalation
    assert result_missing.reason == "Unverified Property Record"


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
    result = generate_draft(question, _verified(get_property(property_id)))
    assert result.text == ESCALATION_TEXT
    assert result.requires_escalation


def test_greeting_is_safe_without_property_match():
    result = generate_draft("Hello!", None)
    assert result.text == "Hello! What property would you like to learn about?"
    assert not result.requires_escalation


def test_missing_property_escalates_and_ambiguous_property_lists_candidates():
    """A missing property escalates; an ambiguous match lists candidates.

    When several verified listings match, the assistant must not silently
    pick one. It returns a grounded summary of every candidate instead of
    escalating, and never asserts that any single candidate is correct.
    """
    assert generate_draft("How much is it?", None).requires_escalation
    ambiguous_demo = [
        get_property("bole_apartment_2bed"),
        get_property("bole_apartment_3bed"),
    ]
    result_demo = generate_draft("How much is the Bole apartment?", ambiguous_demo)
    assert result_demo.requires_escalation
    assert result_demo.text == ESCALATION_TEXT
    assert result_demo.reason == "Unverified Property Record"

    ambiguous_verified = [
        _verified(get_property("bole_apartment_2bed")),
        _verified(get_property("bole_apartment_3bed")),
    ]
    result = generate_draft("How much is the Bole apartment?", ambiguous_verified)
    assert not result.requires_escalation
    assert "Bole 2 Bedroom Apartment" in result.text
    assert "Bole 3 Bedroom Apartment" in result.text
    assert "12,000,000 ETB" in result.text
    assert "18,000,000 ETB" in result.text


def test_empty_input_fails_explicitly():
    with pytest.raises(DraftGenerationError, match="empty"):
        generate_draft("", None)


def test_live_mode_requires_key():
    with pytest.raises(DraftGenerationError, match="GEMINI_API_KEY"):
        generate_draft(
            "What is the price of the Bole 2 bedroom apartment?",
            _verified(get_property("bole_apartment_2bed")),
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
    with pytest.raises(DraftGenerationError, match="empty response") as raised:
        generate_draft(
            "What is the price of the Bole 2 bedroom apartment?",
            _verified(get_property("bole_apartment_2bed")),
            mock_mode=False,
            api_key="test-not-a-real-key",
        )
    assert raised.value.category == "MODEL_EMPTY_RESPONSE"
    assert raised.value.__cause__ is None


def test_live_generation_error_preserves_wrapped_cause_class(monkeypatch):
    from google import genai

    class ProviderFailure(Exception):
        status_code = 400

    class Models:
        def generate_content(self, **kwargs):
            raise ProviderFailure("private provider details")

    class Client:
        def __init__(self, **kwargs):
            self.models = Models()

    monkeypatch.setattr(genai, "Client", Client)
    with pytest.raises(DraftGenerationError) as raised:
        generate_draft(
            "How much is the Bole 2 bedroom apartment?",
            _verified(get_property("bole_apartment_2bed")),
            mock_mode=False,
            api_key="test-not-a-real-key",
        )

    assert raised.value.category == "DRAFT_GENERATION_FAILED"
    assert isinstance(raised.value.__cause__, ProviderFailure)


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
        _verified(get_property("bole_apartment_2bed")),
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
        _verified(get_property("bole_apartment_2bed")),
        mock_mode=False,
        api_key="test-not-a-real-key",
    )
    assert result.requires_escalation
    assert result.text == ESCALATION_TEXT