"""Regression tests for property catalog, provenance tracking, and verification guards."""

from real_estate_mvp.catalog import (
    PROVENANCE_DEMO,
    PROVENANCE_IMPORTED,
    PROVENANCE_UNVERIFIED,
    PROVENANCE_VERIFIED,
    is_verified_property,
    normalize_property_record,
)


def test_is_verified_property_requires_both_verified_and_provenance():
    assert is_verified_property(None) is False
    assert is_verified_property({}) is False
    assert is_verified_property({"name": "Test Property"}) is False

    # Missing provenance
    assert is_verified_property({"verified": True}) is False
    # Missing verified
    assert is_verified_property({"provenance": PROVENANCE_VERIFIED}) is False

    # False verification flag
    assert is_verified_property({"verified": False, "provenance": PROVENANCE_VERIFIED}) is False
    assert is_verified_property({"verified": None, "provenance": PROVENANCE_VERIFIED}) is False

    # Truthy non-bools
    assert is_verified_property({"verified": 1, "provenance": PROVENANCE_VERIFIED}) is False
    assert is_verified_property({"verified": "True", "provenance": PROVENANCE_VERIFIED}) is False

    # Unverified / demo / imported provenance with verified=True
    assert is_verified_property({"verified": True, "provenance": PROVENANCE_DEMO}) is False
    assert is_verified_property({"verified": True, "provenance": PROVENANCE_IMPORTED}) is False
    assert is_verified_property({"verified": True, "provenance": PROVENANCE_UNVERIFIED}) is False
    assert is_verified_property({"verified": True, "provenance": "other"}) is False

    # Exactly both verified is True and provenance == "verified"
    assert is_verified_property({"verified": True, "provenance": PROVENANCE_VERIFIED}) is True


def test_normalize_property_record_demo_record_is_unverified():
    raw_demo = {
        "property_id": "test_demo",
        "name": "Test Demo Property",
        "property_type": "Apartment",
        "bedrooms": 2,
        "price": "10,000,000 ETB",
    }
    normalized = normalize_property_record(raw_demo)
    assert normalized["provenance"] == PROVENANCE_DEMO
    assert normalized["verified"] is False
    assert is_verified_property(normalized) is False


def test_normalize_property_record_imported_record_is_unverified():
    raw_imported = {
        "property_id": "test_imported",
        "name": "Test Imported Property",
        "property_type": "Apartment",
        "location": "Bole",
        "imported_at": "2026-10-13T12:00:00Z",
    }
    normalized = normalize_property_record(raw_imported)
    assert normalized["provenance"] == PROVENANCE_IMPORTED
    assert normalized["verified"] is False
    assert is_verified_property(normalized) is False


def test_normalize_property_record_preserves_verified_status():
    raw_verified = {
        "property_id": "test_verified",
        "name": "Test Verified Property",
        "property_type": "Apartment",
        "location": "Bole",
        "verified": True,
        "provenance": PROVENANCE_VERIFIED,
    }
    normalized = normalize_property_record(raw_verified)
    assert normalized["provenance"] == PROVENANCE_VERIFIED
    assert normalized["verified"] is True
    assert is_verified_property(normalized) is True


def test_normalize_property_record_does_not_promote_demo_when_verified_flag_present():
    raw_demo_with_flag = {
        "property_id": "test_demo_bad",
        "name": "Demo With Flag",
        "property_type": "Apartment",
        "provenance": PROVENANCE_DEMO,
        "verified": True,
    }
    normalized = normalize_property_record(raw_demo_with_flag)
    assert normalized["provenance"] == PROVENANCE_DEMO
    assert is_verified_property(normalized) is False
