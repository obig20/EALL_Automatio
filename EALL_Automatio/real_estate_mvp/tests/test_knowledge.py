import json
from pathlib import Path

from real_estate_mvp.catalog import PROVENANCE_DEMO, is_verified_property
from real_estate_mvp.knowledge import (
    find_relevant_properties,
    get_all_properties,
    get_property,
    get_property_context,
    search_properties,
)


def test_lookup_and_demo_data_are_explicitly_marked():
    bole = get_property("bole_apartment_2bed")
    assert bole["bedrooms"] == 2
    assert bole["provenance"] == PROVENANCE_DEMO
    assert bole.get("verified") is False
    assert is_verified_property(bole) is False
    assert get_property("not_a_property") is None
    raw = json.loads(
        (Path(__file__).parents[1] / "data" / "properties.json").read_text(
            encoding="utf-8"
        )
    )
    assert "DEMO DATA ONLY" in raw["_notice"]["data_classification"]


def test_search_and_context():
    results = search_properties("Bole 2 Bedroom")
    assert [result["property_id"] for result in results] == ["bole_apartment_2bed"]
    assert get_property_context("bole_apartment_2bed")["price"] == "12,000,000 ETB"


def test_deterministic_property_matching():
    assert find_relevant_properties("How much is the Bole 2 bedroom apartment?") == [
        "bole_apartment_2bed"
    ]
    assert find_relevant_properties("Tell me about the Summit villa") == ["summit_villa"]
    assert find_relevant_properties("Hi there") == []
    assert find_relevant_properties("") == []


def test_villa_request_rejects_apartment_records():
    """A villa request must never be satisfied by an apartment record."""
    assert find_relevant_properties("three bedroom villa") == []
    assert find_relevant_properties("3 bedroom villa") == []
    assert find_relevant_properties(
        "I'm looking for a three-bedroom villa in an area you haven't listed"
    ) == []
    # The only villa in the catalog is 4 bedrooms, so a 3-bedroom villa
    # request has no compatible listing even though a 3-bedroom apartment
    # exists.
    assert find_relevant_properties("I want a villa") == ["summit_villa"]


def test_compatible_property_type_matches():
    """Specific type nouns constrain the catalog to a compatible group."""
    assert find_relevant_properties("3 bedroom apartment") == ["bole_apartment_3bed"]
    assert find_relevant_properties("apartment in Bole") == [
        "bole_apartment_2bed",
        "bole_apartment_3bed",
    ]
    assert find_relevant_properties("Bole 2 bedroom apartment") == [
        "bole_apartment_2bed"
    ]


def test_ambiguous_type_returns_multiple_candidates():
    """When several compatible listings match, all of them are returned."""
    results = find_relevant_properties("apartment")
    assert set(results) == {
        "bole_apartment_2bed",
        "bole_apartment_3bed",
        "kazanchis_apartment",
    }


def test_generic_house_word_does_not_constrain_type():
    """"House" is loose enough to mean "property", so Bole apartments still match."""
    assert find_relevant_properties("How much is the house in Bole?") == [
        "bole_apartment_2bed",
        "bole_apartment_3bed",
    ]