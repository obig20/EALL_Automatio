import json
from pathlib import Path

from real_estate_mvp.knowledge import (
    find_relevant_properties,
    get_all_properties,
    get_property,
    get_property_context,
    search_properties,
)


def test_lookup_and_demo_data_are_explicitly_marked():
    assert get_property("bole_apartment_2bed")["bedrooms"] == 2
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